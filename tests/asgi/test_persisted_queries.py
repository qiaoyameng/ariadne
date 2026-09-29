import hashlib
import json
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

from ariadne import InMemoryPersistedQueryStore
from ariadne.asgi import GraphQL


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


def build_request(method="POST", payload=None, query_string=""):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    headers = []
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode("ascii")))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "method": method,
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": query_string.encode("ascii"),
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    body_sent = False

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def execute(app, **kwargs):
    response = await app.handle_request(build_request(**kwargs))
    return response.status_code, json.loads(response.body)


async def post(app, payload):
    return await execute(app, method="POST", payload=payload)


async def post_hash_only(app, query_hash, **extra):
    return await post(app, {"extensions": make_extension(query_hash), **extra})


async def get_with_extension(app, query_hash):
    query_string = urlencode({"extensions": json.dumps(make_extension(query_hash))})
    return await execute(app, method="GET", query_string=query_string)


@pytest.fixture
def store():
    return InMemoryPersistedQueryStore()


@pytest.fixture
def app(schema, store):
    return GraphQL(schema, persisted_query_store=store)


@pytest.fixture
def get_app(schema, store):
    return GraphQL(schema, persisted_query_store=store, execute_get_queries=True)


@pytest.mark.asyncio
async def test_query_is_registered_then_executed_from_cache(app):
    payload = {
        "query": "{ status }",
        "extensions": make_extension(make_hash("{ status }")),
    }
    status, body = await post(app, payload)
    assert status == 200
    assert body == {"data": {"status": True}}

    status, body = await post_hash_only(app, make_hash("{ status }"))
    assert status == 200
    assert body == {"data": {"status": True}}


@pytest.mark.asyncio
async def test_hash_only_request_for_unknown_hash_returns_not_found_error(app):
    status, body = await post_hash_only(app, make_hash("{ status }"))
    assert status == 400
    error = body["errors"][0]
    assert error["message"] == "PersistedQueryNotFound"
    assert error["extensions"]["code"] == "PersistedQueryNotFound"


@pytest.mark.asyncio
async def test_cached_query_is_executed_with_current_variables(app):
    payload = {
        "query": HELLO_QUERY,
        "variables": {"name": "Alice"},
        "operationName": "SayHello",
        "extensions": make_extension(HELLO_HASH),
    }
    status, body = await post(app, payload)
    assert body == {"data": {"hello": "Hello, Alice!"}}

    status, body = await post_hash_only(
        app, HELLO_HASH, variables={"name": "Bob"}, operationName="SayHello"
    )
    assert status == 200
    assert body == {"data": {"hello": "Hello, Bob!"}}


@pytest.mark.asyncio
async def test_cached_multi_operation_query_uses_current_operation_name(app):
    payload = {
        "query": MULTI_OPERATION_QUERY,
        "operationName": "HelloAlice",
        "extensions": make_extension(MULTI_OPERATION_HASH),
    }
    status, body = await post(app, payload)
    assert body == {"data": {"hello": "Hello, Alice!"}}

    status, body = await post_hash_only(
        app, MULTI_OPERATION_HASH, operationName="HelloBob"
    )
    assert status == 200
    assert body == {"data": {"hello": "Hello, Bob!"}}


@pytest.mark.asyncio
async def test_unsupported_persisted_query_version_returns_error(app):
    payload = {"extensions": make_extension(make_hash("{ status }"), version=2)}
    status, body = await post(app, payload)
    assert status == 400
    error = body["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryNotSupported"


@pytest.mark.asyncio
async def test_invalid_persisted_query_hash_returns_error(app):
    status, body = await post(app, {"extensions": make_extension("not-a-sha256-hash")})
    assert status == 400
    error = body["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryInvalidHash"


@pytest.mark.asyncio
async def test_query_not_matching_hash_returns_error(app):
    payload = {
        "query": "{ status }",
        "extensions": make_extension(make_hash("{ other }")),
    }
    status, body = await post(app, payload)
    assert status == 400
    error = body["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryHashMismatch"


@pytest.mark.asyncio
async def test_invalid_query_is_not_stored(app):
    invalid_query = "{ status"
    query_hash = make_hash(invalid_query)
    payload = {"query": invalid_query, "extensions": make_extension(query_hash)}
    status, body = await post(app, payload)
    assert status == 400

    status, body = await post_hash_only(app, query_hash)
    assert status == 400
    error = body["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryNotFound"


@pytest.mark.asyncio
async def test_query_failing_validation_is_not_stored(app):
    invalid_query = "{ unknownField }"
    query_hash = make_hash(invalid_query)
    payload = {"query": invalid_query, "extensions": make_extension(query_hash)}
    status, body = await post(app, payload)
    assert status == 400

    status, body = await post_hash_only(app, query_hash)
    assert status == 400
    error = body["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryNotFound"


@pytest.mark.asyncio
async def test_cached_mutation_is_not_executed_over_get(get_app):
    payload = {"query": MUTATION_QUERY, "extensions": make_extension(MUTATION_HASH)}
    status, body = await post(get_app, payload)
    assert status == 200
    assert body == {"data": {"echo": "Echo: Hello"}}

    status, body = await get_with_extension(get_app, MUTATION_HASH)
    assert status == 400
    assert "data" not in body


@pytest.mark.asyncio
async def test_cached_query_is_executed_over_get(get_app):
    payload = {
        "query": "{ status }",
        "extensions": make_extension(make_hash("{ status }")),
    }
    await post(get_app, payload)

    status, body = await get_with_extension(get_app, make_hash("{ status }"))
    assert status == 200
    assert body == {"data": {"status": True}}


@pytest.mark.asyncio
async def test_cached_query_is_revalidated_with_current_validation_rules(
    schema, store
):
    introspecting_app = GraphQL(schema, persisted_query_store=store)
    payload = {
        "query": INTROSPECTION_QUERY,
        "extensions": make_extension(INTROSPECTION_HASH),
    }
    status, body = await post(introspecting_app, payload)
    assert status == 200

    secure_app = GraphQL(schema, persisted_query_store=store, introspection=False)
    status, body = await post_hash_only(secure_app, INTROSPECTION_HASH)
    assert status == 400
    assert "data" not in body


@pytest.mark.asyncio
async def test_evicted_query_is_not_found_and_hash_is_not_remapped(schema):
    store = InMemoryPersistedQueryStore(max_size=1)
    app = GraphQL(schema, persisted_query_store=store)

    query_one = "{ status }"
    query_two = '{ hello(name: "Alice") }'
    hash_one = make_hash(query_one)
    hash_two = make_hash(query_two)

    await post(app, {"query": query_one, "extensions": make_extension(hash_one)})
    await post(app, {"query": query_two, "extensions": make_extension(hash_two)})

    status, body = await post_hash_only(app, hash_one)
    assert status == 400
    error = body["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryNotFound"

    status, body = await post_hash_only(app, hash_two)
    assert status == 200
    assert body == {"data": {"hello": "Hello, Alice!"}}


@pytest.mark.asyncio
async def test_store_get_failure_is_reported_as_graphql_error(schema):
    class GetFailingStore:
        def get(self, query_hash):
            raise RuntimeError("store get failed")

        def set(self, query_hash, query):
            pass

    app = GraphQL(schema, persisted_query_store=GetFailingStore())
    status, body = await post_hash_only(app, make_hash("{ status }"))
    assert status == 400
    error = body["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryStoreError"


@pytest.mark.asyncio
async def test_store_set_failure_is_reported_as_graphql_error(schema):
    class SetFailingStore:
        def get(self, query_hash):
            return None

        def set(self, query_hash, query):
            raise RuntimeError("store set failed")

    app = GraphQL(schema, persisted_query_store=SetFailingStore())
    payload = {
        "query": "{ status }",
        "extensions": make_extension(make_hash("{ status }")),
    }
    status, body = await post(app, payload)
    assert status == 400
    error = body["errors"][0]
    assert error["extensions"]["code"] == "PersistedQueryStoreError"


@pytest.mark.asyncio
async def test_persisted_queries_are_disabled_without_store(schema):
    app = GraphQL(schema)
    status, body = await post_hash_only(app, make_hash("{ status }"))
    assert status == 400
    error = body["errors"][0]
    assert error["message"] == "The query must be a string."
    assert error.get("extensions", {}).get("code") != "PersistedQueryNotFound"
