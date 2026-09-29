import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest
from werkzeug.test import Client as WsgiClient
from werkzeug.wrappers import Response as WsgiResponse

from ariadne import InMemoryPersistedQueryStore
from ariadne.asgi import GraphQL as AsgiGraphQL
from ariadne.persisted_queries import (
    PERSISTED_QUERY_HASH_MISMATCH,
    PERSISTED_QUERY_INVALID_HASH,
    PERSISTED_QUERY_NOT_FOUND,
    PERSISTED_QUERY_NOT_SUPPORTED,
    register_persisted_query,
    resolve_persisted_query,
)
from ariadne.wsgi import GraphQL as WsgiGraphQL

QUERY = '{ hello(name: "Bob") }'
QUERY_HASH = hashlib.sha256(QUERY.encode("utf-8")).hexdigest()

VARIABLES_QUERY = "query Hello($name: String!) { hello(name: $name) }"
VARIABLES_QUERY_HASH = hashlib.sha256(VARIABLES_QUERY.encode("utf-8")).hexdigest()

MULTI_OPERATION_QUERY = (
    "query GetStatus { status } "
    'query GetHello { hello(name: "Alice") }'
)
MULTI_OPERATION_QUERY_HASH = hashlib.sha256(
    MULTI_OPERATION_QUERY.encode("utf-8")
).hexdigest()

MUTATION = 'mutation { echo(text: "ping") }'
MUTATION_HASH = hashlib.sha256(MUTATION.encode("utf-8")).hexdigest()

STATUS_QUERY = "{ status }"
STATUS_QUERY_HASH = hashlib.sha256(STATUS_QUERY.encode("utf-8")).hexdigest()

INTROSPECTION_QUERY = "{ __schema { types { name } } }"
INTROSPECTION_QUERY_HASH = hashlib.sha256(
    INTROSPECTION_QUERY.encode("utf-8")
).hexdigest()


def hash_only_payload(sha256_hash, **extra):
    return {
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": sha256_hash}
        },
        **extra,
    }


def full_payload(query, sha256_hash=None, **extra):
    if sha256_hash is None:
        sha256_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return {
        "query": query,
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": sha256_hash}
        },
        **extra,
    }


class AsgiClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, query_string=b"", body=b"", headers=()):
        status = None
        response_body = b""

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            nonlocal status, response_body
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                response_body += message.get("body", b"")

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": "/",
            "query_string": query_string,
            "headers": list(headers),
        }
        asyncio.run(self.app(scope, receive, send))
        return status, json.loads(response_body)

    def post(self, payload):
        return self.request(
            "POST",
            body=json.dumps(payload).encode("utf-8"),
            headers=[(b"content-type", b"application/json")],
        )

    def get(self, params):
        return self.request("GET", query_string=urlencode(params).encode("utf-8"))


class WsgiClientWrapper:
    def __init__(self, app):
        self.client = WsgiClient(app, WsgiResponse)

    def post(self, payload):
        response = self.client.post("/", json=payload)
        return response.status_code, json.loads(response.get_data(as_text=True))

    def get(self, params):
        response = self.client.get(f"/?{urlencode(params)}")
        return response.status_code, json.loads(response.get_data(as_text=True))


@pytest.fixture(params=["asgi", "wsgi"])
def make_client(request, schema):
    def factory(**kwargs):
        if request.param == "asgi":
            return AsgiClient(AsgiGraphQL(schema, **kwargs))
        return WsgiClientWrapper(WsgiGraphQL(schema, **kwargs))

    return factory


@pytest.fixture
def store():
    return InMemoryPersistedQueryStore()


@pytest.fixture
def client(make_client, store):
    return make_client(persisted_query_store=store)


def error_code(result):
    return result["errors"][0]["extensions"]["code"]


# Handshake and basic execution


def test_hash_only_request_returns_not_found_error_on_cache_miss(client):
    status, result = client.post(hash_only_payload(QUERY_HASH))
    assert status == 400
    assert error_code(result) == PERSISTED_QUERY_NOT_FOUND


def test_full_query_is_registered_and_replayed_with_hash_only(client):
    status, result = client.post(full_payload(QUERY))
    assert status == 200
    assert result == {"data": {"hello": "Hello, Bob!"}}

    status, result = client.post(hash_only_payload(QUERY_HASH))
    assert status == 200
    assert result == {"data": {"hello": "Hello, Bob!"}}


def test_apq_is_disabled_by_default(make_client):
    client = make_client()

    status, result = client.post(hash_only_payload(QUERY_HASH))
    assert status == 400
    assert "extensions" not in result["errors"][0]

    # extensions are ignored and query executes normally
    status, result = client.post(full_payload(QUERY))
    assert status == 200
    assert result == {"data": {"hello": "Hello, Bob!"}}


def test_cached_query_is_executed_with_current_request_variables(client):
    client.post(full_payload(VARIABLES_QUERY, variables={"name": "Alice"}))

    status, result = client.post(
        hash_only_payload(VARIABLES_QUERY_HASH, variables={"name": "Bob"})
    )
    assert status == 200
    assert result == {"data": {"hello": "Hello, Bob!"}}


def test_cached_query_with_multiple_operations_uses_operation_name(client):
    status, result = client.post(
        full_payload(MULTI_OPERATION_QUERY, operationName="GetHello")
    )
    assert status == 200
    assert result == {"data": {"hello": "Hello, Alice!"}}

    status, result = client.post(
        hash_only_payload(MULTI_OPERATION_QUERY_HASH, operationName="GetStatus")
    )
    assert status == 200
    assert result == {"data": {"status": True}}


# GET requests


def test_get_request_executes_cached_query(make_client, store):
    client = make_client(persisted_query_store=store, execute_get_queries=True)
    client.post(full_payload(STATUS_QUERY))

    status, result = client.get(
        {
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": STATUS_QUERY_HASH}}
            )
        }
    )
    assert status == 200
    assert result == {"data": {"status": True}}


def test_get_request_cannot_execute_cached_mutation(make_client, store):
    client = make_client(persisted_query_store=store, execute_get_queries=True)

    status, result = client.post(full_payload(MUTATION))
    assert status == 200
    assert result == {"data": {"echo": "Echo: ping"}}

    status, result = client.get(
        {
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": MUTATION_HASH}}
            )
        }
    )
    assert status == 400
    assert "data" not in result


# Validation and security policies are applied on every request


def test_cached_query_is_revalidated_against_current_schema(make_client, store):
    allowing_client = make_client(persisted_query_store=store, introspection=True)
    status, _ = allowing_client.post(full_payload(INTROSPECTION_QUERY))
    assert status == 200

    forbidding_client = make_client(persisted_query_store=store, introspection=False)
    status, result = forbidding_client.post(
        hash_only_payload(INTROSPECTION_QUERY_HASH)
    )
    assert status == 400
    assert "introspection" in result["errors"][0]["message"].lower()


def test_invalid_query_is_not_stored(client):
    invalid_query = "{ hello(name: "
    invalid_hash = hashlib.sha256(invalid_query.encode("utf-8")).hexdigest()

    status, result = client.post(full_payload(invalid_query, invalid_hash))
    assert status == 400

    status, result = client.post(hash_only_payload(invalid_hash))
    assert status == 400
    assert error_code(result) == PERSISTED_QUERY_NOT_FOUND


def test_query_failing_validation_is_not_stored(client):
    invalid_query = "{ notInSchema }"
    invalid_hash = hashlib.sha256(invalid_query.encode("utf-8")).hexdigest()

    status, result = client.post(full_payload(invalid_query, invalid_hash))
    assert status == 400

    status, result = client.post(hash_only_payload(invalid_hash))
    assert status == 400
    assert error_code(result) == PERSISTED_QUERY_NOT_FOUND


# Hash and version validation


def test_unsupported_version_returns_stable_error_code(client):
    payload = full_payload(QUERY)
    payload["extensions"]["persistedQuery"]["version"] = 2

    status, result = client.post(payload)
    assert status == 400
    assert error_code(result) == PERSISTED_QUERY_NOT_SUPPORTED


def test_invalid_hash_returns_stable_error_code(client):
    status, result = client.post(hash_only_payload("not-a-hash"))
    assert status == 400
    assert error_code(result) == PERSISTED_QUERY_INVALID_HASH


def test_malformed_persisted_query_extension_returns_stable_error_code(client):
    status, result = client.post({"extensions": {"persistedQuery": "invalid"}})
    assert status == 400
    assert error_code(result) == PERSISTED_QUERY_INVALID_HASH


def test_hash_mismatch_returns_stable_error_code(client):
    other_hash = hashlib.sha256(b"{ status }").hexdigest()

    status, result = client.post(full_payload(QUERY, other_hash))
    assert status == 400
    assert error_code(result) == PERSISTED_QUERY_HASH_MISMATCH


# Store behavior


def test_store_eviction_only_affects_cache_hits(make_client):
    store = InMemoryPersistedQueryStore(max_size=2)
    client = make_client(persisted_query_store=store)

    queries = [
        '{ hello(name: "One") }',
        '{ hello(name: "Two") }',
        '{ hello(name: "Three") }',
    ]
    hashes = [hashlib.sha256(q.encode("utf-8")).hexdigest() for q in queries]

    for query in queries:
        status, _ = client.post(full_payload(query))
        assert status == 200

    # oldest entry was evicted
    status, result = client.post(hash_only_payload(hashes[0]))
    assert status == 400
    assert error_code(result) == PERSISTED_QUERY_NOT_FOUND

    # newer entries are still cached and map to correct queries
    for query, query_hash in zip(queries[1:], hashes[1:]):
        status, result = client.post(hash_only_payload(query_hash))
        assert status == 200
        name = query.split('"')[1]
        assert result == {"data": {"hello": f"Hello, {name}!"}}


class FailingGetStore(InMemoryPersistedQueryStore):
    def get(self, key):
        raise RuntimeError("store is down")


class FailingSetStore(InMemoryPersistedQueryStore):
    def set(self, key, query):
        raise RuntimeError("store is down")


def test_store_get_failure_is_reported_through_error_handling(make_client):
    client = make_client(persisted_query_store=FailingGetStore())

    status, result = client.post(hash_only_payload(QUERY_HASH))
    assert status == 400
    assert "errors" in result


def test_store_set_failure_is_reported_through_error_handling(make_client):
    client = make_client(persisted_query_store=FailingSetStore())

    status, result = client.post(full_payload(QUERY))
    assert status == 400
    assert "errors" in result


def test_concurrent_registration_of_same_hash_is_consistent(make_client, store):
    client = make_client(persisted_query_store=store)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(lambda _: client.post(full_payload(QUERY)), range(16))
        )

    assert all(status == 200 for status, _ in results)
    assert store.get(QUERY_HASH) == QUERY

    status, result = client.post(hash_only_payload(QUERY_HASH))
    assert status == 200
    assert result == {"data": {"hello": "Hello, Bob!"}}


# InMemoryPersistedQueryStore unit tests


def test_in_memory_store_requires_positive_max_size():
    with pytest.raises(ValueError):
        InMemoryPersistedQueryStore(max_size=0)


def test_in_memory_store_evicts_least_recently_used_entry():
    store = InMemoryPersistedQueryStore(max_size=2)
    store.set("a", "query-a")
    store.set("b", "query-b")

    # refresh "a" so "b" becomes the least recently used
    assert store.get("a") == "query-a"

    store.set("c", "query-c")

    assert store.get("a") == "query-a"
    assert store.get("b") is None
    assert store.get("c") == "query-c"


def test_in_memory_store_overwrite_keeps_capacity():
    store = InMemoryPersistedQueryStore(max_size=1)
    store.set("a", "query-a")
    store.set("a", "query-a-updated")

    assert store.get("a") == "query-a-updated"


# resolve_persisted_query unit tests


def test_resolve_persisted_query_passes_through_non_dict_data(store):
    data = [{"query": "{ status }"}]
    assert resolve_persisted_query(data, store) == (data, None)


def test_resolve_persisted_query_passes_through_data_without_extension(store):
    data = {"query": "{ status }"}
    assert resolve_persisted_query(data, store) == (data, None)


def test_resolve_persisted_query_passes_through_other_extensions(store):
    data = {"query": "{ status }", "extensions": {"tracing": {"version": 1}}}
    assert resolve_persisted_query(data, store) == (data, None)


def test_resolve_persisted_query_passes_through_non_string_query(store):
    data = {"query": 123, "extensions": {"persistedQuery": {
        "version": 1, "sha256Hash": QUERY_HASH
    }}}
    assert resolve_persisted_query(data, store) == (data, None)


def test_resolve_persisted_query_returns_hash_for_registration(store):
    data, pending_hash = resolve_persisted_query(full_payload(QUERY), store)
    assert data["query"] == QUERY
    assert pending_hash == QUERY_HASH


def test_register_persisted_query_stores_query(store):
    register_persisted_query(store, QUERY_HASH, QUERY)
    assert store.get(QUERY_HASH) == QUERY
