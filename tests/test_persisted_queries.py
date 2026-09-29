import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from graphql import GraphQLError

from ariadne.persisted_queries import (
    ERROR_CODE_HASH_MISMATCH,
    ERROR_CODE_INVALID_HASH,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_NOT_SUPPORTED,
    ERROR_CODE_STORE_ERROR,
    InMemoryPersistedQueryStore,
    PersistedQueryStore,
    register_persisted_query,
    resolve_persisted_query,
)


def make_hash(query):
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def make_extension(query_hash, version=1):
    return {"persistedQuery": {"version": version, "sha256Hash": query_hash}}


@pytest.fixture
def store():
    return InMemoryPersistedQueryStore()


def test_in_memory_store_is_a_persisted_query_store(store):
    assert isinstance(store, PersistedQueryStore)


def test_in_memory_store_returns_none_for_unknown_hash(store):
    assert store.get(make_hash("{ unknown }")) is None


def test_in_memory_store_returns_stored_query(store):
    query_hash = make_hash("{ status }")
    store.set(query_hash, "{ status }")
    assert store.get(query_hash) == "{ status }"


def test_in_memory_store_keeps_distinct_queries_for_distinct_hashes(store):
    hash_one = make_hash("{ status }")
    hash_two = make_hash("{ hello }")
    store.set(hash_one, "{ status }")
    store.set(hash_two, "{ hello }")
    assert store.get(hash_one) == "{ status }"
    assert store.get(hash_two) == "{ hello }"


def test_in_memory_store_evicts_least_recently_used_query():
    store = InMemoryPersistedQueryStore(max_size=2)
    hash_one = make_hash("{ one }")
    hash_two = make_hash("{ two }")
    hash_three = make_hash("{ three }")

    store.set(hash_one, "{ one }")
    store.set(hash_two, "{ two }")
    store.get(hash_one)  # mark hash_one as recently used
    store.set(hash_three, "{ three }")

    assert store.get(hash_one) == "{ one }"
    assert store.get(hash_two) is None
    assert store.get(hash_three) == "{ three }"


def test_in_memory_store_set_existing_hash_updates_query_and_recency():
    store = InMemoryPersistedQueryStore(max_size=2)
    hash_one = make_hash("{ one }")
    hash_two = make_hash("{ two }")

    store.set(hash_one, "{ one }")
    store.set(hash_two, "{ two }")
    store.set(hash_one, "{ one }")
    store.set(make_hash("{ three }"), "{ three }")

    assert store.get(hash_one) == "{ one }"
    assert store.get(hash_two) is None


def test_in_memory_store_rejects_invalid_max_size():
    with pytest.raises(ValueError):
        InMemoryPersistedQueryStore(max_size=0)


def test_in_memory_store_concurrent_registration_of_same_hash_is_consistent():
    store = InMemoryPersistedQueryStore(max_size=10)
    query = "{ status }"
    query_hash = make_hash(query)

    def register_and_read(_):
        store.set(query_hash, query)
        return store.get(query_hash)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(register_and_read, range(64)))

    assert results == [query] * 64
    assert store.get(query_hash) == query


def test_in_memory_store_concurrent_registration_respects_max_size():
    store = InMemoryPersistedQueryStore(max_size=5)
    queries = ["{ field%s }" % i for i in range(20)]

    def register(query):
        store.set(make_hash(query), query)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(register, queries))

    stored = [query for query in queries if store.get(make_hash(query)) is not None]
    assert 0 < len(stored) <= 5
    assert stored == queries[-len(stored) :]


def test_resolve_returns_none_without_persisted_query_extension(store):
    assert resolve_persisted_query(store, {"query": "{ status }"}) is None
    assert (
        resolve_persisted_query(store, {"query": "{ status }", "extensions": {}})
        is None
    )


def test_resolve_returns_registration_for_matching_query_and_hash(store):
    query = "{ status }"
    data = {"query": query, "extensions": make_extension(make_hash(query))}
    assert resolve_persisted_query(store, data) == (make_hash(query), query)


def test_resolve_accepts_uppercase_hash(store):
    query = "{ status }"
    query_hash = make_hash(query).upper()
    data = {"query": query, "extensions": make_extension(query_hash)}
    assert resolve_persisted_query(store, data) == (make_hash(query), query)


def test_resolve_raises_not_supported_for_other_version(store):
    data = {"extensions": make_extension(make_hash("{ status }"), version=2)}
    with pytest.raises(GraphQLError) as exc_info:
        resolve_persisted_query(store, data)
    assert exc_info.value.extensions["code"] == ERROR_CODE_NOT_SUPPORTED


def test_resolve_raises_invalid_hash_for_malformed_hash(store):
    data = {"extensions": make_extension("not-a-hash")}
    with pytest.raises(GraphQLError) as exc_info:
        resolve_persisted_query(store, data)
    assert exc_info.value.extensions["code"] == ERROR_CODE_INVALID_HASH


def test_resolve_raises_invalid_hash_for_malformed_extension(store):
    data = {"extensions": {"persistedQuery": "not-a-dict"}}
    with pytest.raises(GraphQLError) as exc_info:
        resolve_persisted_query(store, data)
    assert exc_info.value.extensions["code"] == ERROR_CODE_INVALID_HASH


def test_resolve_raises_hash_mismatch_for_other_query(store):
    data = {
        "query": "{ status }",
        "extensions": make_extension(make_hash("{ other }")),
    }
    with pytest.raises(GraphQLError) as exc_info:
        resolve_persisted_query(store, data)
    assert exc_info.value.extensions["code"] == ERROR_CODE_HASH_MISMATCH


def test_resolve_raises_not_found_for_unknown_hash(store):
    data = {"extensions": make_extension(make_hash("{ status }"))}
    with pytest.raises(GraphQLError) as exc_info:
        resolve_persisted_query(store, data)
    assert exc_info.value.extensions["code"] == ERROR_CODE_NOT_FOUND


def test_resolve_sets_cached_query_on_data(store):
    query = "{ status }"
    query_hash = make_hash(query)
    store.set(query_hash, query)

    data = {"extensions": make_extension(query_hash)}
    assert resolve_persisted_query(store, data) is None
    assert data["query"] == query


def test_resolve_ignores_invalid_query_type(store):
    data = {"query": 123, "extensions": make_extension(make_hash("{ status }"))}
    assert resolve_persisted_query(store, data) is None
    assert data["query"] == 123


def test_resolve_wraps_store_get_errors():
    class FailingStore:
        def get(self, query_hash):
            raise RuntimeError("get failed")

        def set(self, query_hash, query):
            pass

    data = {"extensions": make_extension(make_hash("{ status }"))}
    with pytest.raises(GraphQLError) as exc_info:
        resolve_persisted_query(FailingStore(), data)
    assert exc_info.value.extensions["code"] == ERROR_CODE_STORE_ERROR


def test_register_stores_query(store):
    query_hash = make_hash("{ status }")
    register_persisted_query(store, query_hash, "{ status }")
    assert store.get(query_hash) == "{ status }"


def test_register_wraps_store_set_errors():
    class FailingStore:
        def get(self, query_hash):
            return None

        def set(self, query_hash, query):
            raise RuntimeError("set failed")

    with pytest.raises(GraphQLError) as exc_info:
        register_persisted_query(FailingStore(), make_hash("{ status }"), "{ status }")
    assert exc_info.value.extensions["code"] == ERROR_CODE_STORE_ERROR
