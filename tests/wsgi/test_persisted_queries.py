import hashlib
import json

import pytest
from werkzeug.test import Client
from werkzeug.wrappers import Response

from ariadne import InMemoryPersistedQueryStore
from ariadne.wsgi import GraphQL


# Add TestClient to keep test similar to ASGI
class TestClient(Client):
    __test__ = False

    def __init__(self, app):
        super().__init__(app, Response)


def make_hash(query):
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def make_extension(query_hash, version=1):
    return {"persistedQuery": {"version": version, "sha256Hash": query_hash}}


HELLO_QUERY = 'query SayHello($name: String!) { hello(name: $name) }'
HELLO_HASH = make_hash(HELLO_QUERY)

MULTI_OPERATION_QUERY = """
    query HelloAlice { hello(name: "Alice") }
    query HelloBob { hello(name: "Bob") }
"""
MULTI_OPERATION_HASH = make_hash(MULTI_OPERATION_QUERY)

MUTATION_QUERY = 'mutation { echo(text: "Hello") }'
MUTATION_HASH = make_hash(MUTATION_QUERY)

INTROSPECTION_QUERY = "{ __schema { queryType { name } } }"
INTROSPECTION_HASH = make_hash(INTROSPECTION_QUERY)


@pytest.fixture
def store():
    return InMemoryPersistedQueryStore()


@pytest.fixture
def app(schema, store):
    return GraphQL(schema, persisted_query_store=store)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def get_client(schema, store):
    return TestClient(
        GraphQL(schema, persisted_query_store=store, execute_get_queries=True)
    )


def post(client, payload):
    return client.post("/graphql/", json=payload)


def post_hash_only(client, query_hash, **extra):
    return post(client, {"extensions": make_extension(query_hash), **extra})


def test_query_is_registered_then_executed_from_cache(client):
    response = post(
        client,
        {"query": "{ status }", "extensions": make_extension(make_hash("{ status }"))},
    )
    assert response.status_code == 200
    assert response.get_json() == {"data": {"status": True}}

    response = post_hash_only(client, make_hash("{ status }"))
    assert response.status_code == 200
    assert response.get_json() == {"data": {"status": True}}


def test_hash_only_request_for_unknown_hash_returns_not_found_error(client):
    response = post_hash_only(client, make_hash("{ status }"))
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["message"] == "PersistedQueryNotFound"
    assert error["extensions"]["code"] == "PersistedQueryNotFound"


def test_cached_query_is_executed_with_current_variables(client):
    payload = {
        "query": HELLO_QUERY,
        "variables": {"name": "Alice"},
        "operationName": "SayHello",
        "extensions": make_extension(HELLO_HASH),
    }
    response = post(client, payload)
    assert response.get_json() == {"data": {"hello": "Hello, Alice!"}}

    response = post_hash_only(
        client, HELLO_HASH, variables={"name": "Bob"}, operationName="SayHello"
    )
    assert response.status_code == 200
    assert response.get_json() == {"data": {"hello": "Hello, Bob!"}}


def test_cached_multi_operation_query_uses_current_operation_name(client):
    payload = {
        "query": MULTI_OPERATION_QUERY,
        "operationName": "HelloAlice",
        "extensions": make_extension(MULTI_OPERATION_HASH),
    }
    response = post(client, payload)
    assert response.get_json() == {"data": {"hello": "Hello, Alice!"}}

    response = post_hash_only(client, MULTI_OPERATION_HASH, operationName="HelloBob")
    assert response.status_code == 200
    assert response.get_json() == {"data": {"hello": "Hello, Bob!"}}


def test_unsupported_persisted_query_version_returns_error(client):
    response = post(
        client, {"extensions": make_extension(make_hash("{ status }"), version=2)}
    )
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryNotSupported"


def test_invalid_persisted_query_hash_returns_error(client):
    response = post(client, {"extensions": make_extension("not-a-sha256-hash")})
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryInvalidHash"


def test_query_not_matching_hash_returns_error(client):
    response = post(
        client,
        {"query": "{ status }", "extensions": make_extension(make_hash("{ other }"))},
    )
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryHashMismatch"


def test_invalid_query_is_not_stored(client):
    invalid_query = "{ status"
    query_hash = make_hash(invalid_query)
    response = post(
        client, {"query": invalid_query, "extensions": make_extension(query_hash)}
    )
    assert response.status_code == 400

    response = post_hash_only(client, query_hash)
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryNotFound"


def test_query_failing_validation_is_not_stored(client):
    invalid_query = "{ unknownField }"
    query_hash = make_hash(invalid_query)
    response = post(
        client, {"query": invalid_query, "extensions": make_extension(query_hash)}
    )
    assert response.status_code == 400

    response = post_hash_only(client, query_hash)
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryNotFound"


def test_cached_mutation_is_not_executed_over_get(get_client):
    response = post(
        get_client,
        {"query": MUTATION_QUERY, "extensions": make_extension(MUTATION_HASH)},
    )
    assert response.status_code == 200
    assert response.get_json() == {"data": {"echo": "Echo: Hello"}}

    response = get_client.get(
        "/graphql/",
        query_string={"extensions": json.dumps(make_extension(MUTATION_HASH))},
    )
    assert response.status_code == 400
    assert "data" not in response.get_json()


def test_cached_query_is_executed_over_get(get_client):
    post(
        get_client,
        {"query": "{ status }", "extensions": make_extension(make_hash("{ status }"))},
    )

    response = get_client.get(
        "/graphql/",
        query_string={
            "extensions": json.dumps(make_extension(make_hash("{ status }")))
        },
    )
    assert response.status_code == 200
    assert response.get_json() == {"data": {"status": True}}


def test_cached_query_is_revalidated_with_current_validation_rules(schema, store):
    introspecting_client = TestClient(GraphQL(schema, persisted_query_store=store))
    response = post(
        introspecting_client,
        {
            "query": INTROSPECTION_QUERY,
            "extensions": make_extension(INTROSPECTION_HASH),
        },
    )
    assert response.status_code == 200

    secure_client = TestClient(
        GraphQL(schema, persisted_query_store=store, introspection=False)
    )
    response = post_hash_only(secure_client, INTROSPECTION_HASH)
    assert response.status_code == 400
    assert "data" not in response.get_json()


def test_evicted_query_is_not_found_and_hash_is_not_remapped(schema):
    store = InMemoryPersistedQueryStore(max_size=1)
    client = TestClient(GraphQL(schema, persisted_query_store=store))

    query_one = "{ status }"
    query_two = '{ hello(name: "Alice") }'
    hash_one = make_hash(query_one)
    hash_two = make_hash(query_two)

    post(client, {"query": query_one, "extensions": make_extension(hash_one)})
    post(client, {"query": query_two, "extensions": make_extension(hash_two)})

    response = post_hash_only(client, hash_one)
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryNotFound"

    response = post_hash_only(client, hash_two)
    assert response.status_code == 200
    assert response.get_json() == {"data": {"hello": "Hello, Alice!"}}


def test_store_get_failure_is_reported_as_graphql_error(schema):
    class GetFailingStore:
        def get(self, query_hash):
            raise RuntimeError("store get failed")

        def set(self, query_hash, query):
            pass

    client = TestClient(GraphQL(schema, persisted_query_store=GetFailingStore()))
    response = post_hash_only(client, make_hash("{ status }"))
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryStoreError"


def test_store_set_failure_is_reported_as_graphql_error(schema):
    class SetFailingStore:
        def get(self, query_hash):
            return None

        def set(self, query_hash, query):
            raise RuntimeError("store set failed")

    client = TestClient(GraphQL(schema, persisted_query_store=SetFailingStore()))
    response = post(
        client,
        {"query": "{ status }", "extensions": make_extension(make_hash("{ status }"))},
    )
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryStoreError"


def test_persisted_queries_are_disabled_without_store(schema):
    client = TestClient(GraphQL(schema))
    response = post_hash_only(client, make_hash("{ status }"))
    assert response.status_code == 400
    error = response.get_json()["errors"][0]
    assert error["message"] == "The query must be a string."
    assert error.get("extensions", {}).get("code") != "PersistedQueryNotFound"
