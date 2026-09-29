"""Automatic Persisted Queries (APQ) support.

Implements the Apollo persisted queries protocol: clients send a SHA-256
hash of the query in `extensions.persistedQuery` instead of (or together
with) the query text. Server stores query texts in a `PersistedQueryStore`
so later requests can reference them by hash only.
"""

import hashlib
import re
from collections import OrderedDict
from threading import Lock
from typing import Any, Protocol

from graphql import GraphQLError

__all__ = [
    "InMemoryPersistedQueryStore",
    "PersistedQueryStore",
    "PERSISTED_QUERY_HASH_MISMATCH",
    "PERSISTED_QUERY_INVALID_HASH",
    "PERSISTED_QUERY_NOT_FOUND",
    "PERSISTED_QUERY_NOT_SUPPORTED",
    "register_persisted_query",
    "resolve_persisted_query",
]

PERSISTED_QUERY_VERSION = 1

PERSISTED_QUERY_NOT_FOUND = "PersistedQueryNotFound"
PERSISTED_QUERY_NOT_SUPPORTED = "PersistedQueryNotSupported"
PERSISTED_QUERY_INVALID_HASH = "PersistedQueryInvalidHash"
PERSISTED_QUERY_HASH_MISMATCH = "PersistedQueryHashMismatch"

SHA256_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class PersistedQueryStore(Protocol):
    """Storage interface for automatic persisted queries.

    Implementations must provide synchronous `get` and `set` methods.
    Both synchronous (WSGI) and asynchronous (ASGI) servers call these
    methods directly, so they should be safe to use from multiple threads.
    """

    def get(self, key: str) -> str | None:
        """Return the query text stored for `key` or `None` if it's missing."""
        ...

    def set(self, key: str, query: str) -> None:
        """Store the `query` text under the `key`."""
        ...


class InMemoryPersistedQueryStore:
    """In-memory `PersistedQueryStore` with LRU eviction.

    Stores up to `max_size` queries. When the limit is exceeded, the least
    recently used entries are evicted. Eviction only affects whether future
    hash-only requests hit the cache; a hash always maps to the query it was
    verified against.
    """

    def __init__(self, max_size: int = 1024) -> None:
        """Initialize the store.

        # Required arguments

        `max_size`: an `int` with maximum number of queries to store.
        Must be greater than 0. Defaults to 1024.
        """
        if max_size < 1:
            raise ValueError("max_size must be greater than 0")
        self._max_size = max_size
        self._queries: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> str | None:
        """Return the query stored for `key`, marking it as recently used."""
        with self._lock:
            if key not in self._queries:
                return None
            self._queries.move_to_end(key)
            return self._queries[key]

    def set(self, key: str, query: str) -> None:
        """Store the `query` under `key`, evicting old entries if needed."""
        with self._lock:
            self._queries[key] = query
            self._queries.move_to_end(key)
            while len(self._queries) > self._max_size:
                self._queries.popitem(last=False)


def resolve_persisted_query(
    data: Any, store: PersistedQueryStore
) -> tuple[Any, str | None]:
    """Resolve the `persistedQuery` extension in GraphQL request data.

    Returns a tuple with two items: request data to execute (with the `query`
    filled in from the store for hash-only requests) and a hash of the query
    that should be registered in the store after the query is successfully
    validated (or `None` if there's nothing to register).

    Raises a `GraphQLError` with a stable error code in its `extensions`
    when the persisted query can't be resolved.

    # Required arguments

    `data`: a GraphQL request data.

    `store`: a `PersistedQueryStore` to retrieve persisted queries from.
    """
    if not isinstance(data, dict):
        return data, None

    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        return data, None

    persisted_query = extensions.get("persistedQuery")
    if persisted_query is None:
        return data, None

    if not isinstance(persisted_query, dict):
        raise _persisted_query_error(
            PERSISTED_QUERY_INVALID_HASH,
            "Persisted query extension is invalid.",
        )

    if persisted_query.get("version") != PERSISTED_QUERY_VERSION:
        raise _persisted_query_error(
            PERSISTED_QUERY_NOT_SUPPORTED,
            "Persisted query version is not supported.",
        )

    sha256_hash = persisted_query.get("sha256Hash")
    if not isinstance(sha256_hash, str) or not SHA256_HASH_RE.match(sha256_hash):
        raise _persisted_query_error(
            PERSISTED_QUERY_INVALID_HASH,
            "Persisted query hash is not a valid SHA-256 hash.",
        )

    key = sha256_hash.lower()
    query = data.get("query")

    if query is None or query == "":
        try:
            cached_query = store.get(key)
        except Exception as exc:
            raise GraphQLError(
                "Persisted query store failed to retrieve the query.",
                original_error=exc,
            ) from exc
        if cached_query is None:
            raise _persisted_query_error(
                PERSISTED_QUERY_NOT_FOUND,
                "Persisted query was not found.",
            )
        return {**data, "query": cached_query}, None

    if not isinstance(query, str):
        # Let the standard request validation report the invalid query
        return data, None

    if hashlib.sha256(query.encode("utf-8")).hexdigest() != key:
        raise _persisted_query_error(
            PERSISTED_QUERY_HASH_MISMATCH,
            "Persisted query hash does not match the query.",
        )

    return data, key


def register_persisted_query(
    store: PersistedQueryStore, key: str, query: str
) -> None:
    """Store the `query` under the `key` in the persisted query store.

    Should be called only after the query was successfully parsed and
    validated, so invalid documents never pollute the store.

    Raises a `GraphQLError` when the store fails, so the error is reported
    through the standard error handling path.

    # Required arguments

    `store`: a `PersistedQueryStore` to store the query in.

    `key`: a `str` with the query's SHA-256 hash.

    `query`: a `str` with the query text.
    """
    try:
        store.set(key, query)
    except Exception as exc:
        raise GraphQLError(
            "Persisted query store failed to store the query.",
            original_error=exc,
        ) from exc


def _persisted_query_error(code: str, message: str) -> GraphQLError:
    return GraphQLError(message, extensions={"code": code})
