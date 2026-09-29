import hashlib
import re
from collections import OrderedDict
from threading import Lock
from typing import Protocol, runtime_checkable

from graphql import GraphQLError

__all__ = [
    "InMemoryPersistedQueryStore",
    "PersistedQueryStore",
    "register_persisted_query",
    "resolve_persisted_query",
]

PERSISTED_QUERY_VERSION = 1

ERROR_CODE_NOT_FOUND = "PersistedQueryNotFound"
ERROR_CODE_NOT_SUPPORTED = "PersistedQueryNotSupported"
ERROR_CODE_INVALID_HASH = "PersistedQueryInvalidHash"
ERROR_CODE_HASH_MISMATCH = "PersistedQueryHashMismatch"
ERROR_CODE_STORE_ERROR = "PersistedQueryStoreError"

SHA256_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@runtime_checkable
class PersistedQueryStore(Protocol):
    """Protocol for persisted query stores used by automatic persisted queries.

    Stores keep a mapping of SHA-256 hash of a GraphQL query to the query
    source text. Both methods are synchronous and are called directly by
    both synchronous (WSGI) and asynchronous (ASGI) servers.
    """

    def get(self, query_hash: str) -> str | None:
        """Returns query source for `query_hash` or `None` if it's not stored.

        # Required arguments

        `query_hash`: a `str` with lowercase SHA-256 hexdigest of the query.
        """
        ...

    def set(self, query_hash: str, query: str) -> None:
        """Stores `query` source under its `query_hash`.

        # Required arguments

        `query_hash`: a `str` with lowercase SHA-256 hexdigest of the query.

        `query`: a `str` with GraphQL query source.
        """
        ...


class InMemoryPersistedQueryStore:
    """In-memory persisted query store with LRU eviction.

    Stores up to `max_size` queries in memory. When the store is full, the
    least recently used query is evicted. Eviction only affects whether
    a subsequent lookup hits the cache; a hash is never remapped to
    a different query.

    This store is thread-safe.
    """

    def __init__(self, max_size: int = 1024) -> None:
        """Initializes the store.

        # Optional arguments

        `max_size`: an `int` with maximum number of queries to keep in
        the store. Defaults to `1024`.
        """
        if max_size < 1:
            raise ValueError("max_size must be a positive integer")

        self.max_size = max_size
        self._queries: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()

    def get(self, query_hash: str) -> str | None:
        """Returns query source for `query_hash` or `None` if it's not stored.

        Marks the entry as recently used.

        # Required arguments

        `query_hash`: a `str` with lowercase SHA-256 hexdigest of the query.
        """
        with self._lock:
            try:
                query = self._queries.pop(query_hash)
            except KeyError:
                return None
            self._queries[query_hash] = query
            return query

    def set(self, query_hash: str, query: str) -> None:
        """Stores `query` source under its `query_hash`.

        Evicts least recently used entries if the store exceeds `max_size`.

        # Required arguments

        `query_hash`: a `str` with lowercase SHA-256 hexdigest of the query.

        `query`: a `str` with GraphQL query source.
        """
        with self._lock:
            self._queries.pop(query_hash, None)
            self._queries[query_hash] = query
            while len(self._queries) > self.max_size:
                self._queries.popitem(last=False)


def resolve_persisted_query(
    store: PersistedQueryStore,
    data: dict,
) -> tuple[str, str] | None:
    """Resolves the `persistedQuery` extension from GraphQL request data.

    If request contains only a persisted query hash, looks the query up in
    the `store` and sets it on `data`. If request contains both the query
    and its hash, validates the hash and returns a `(query_hash, query)`
    tuple that should be registered with `register_persisted_query` once
    the query passes parsing and validation.

    Returns `None` if request doesn't use the persisted query protocol.

    Raises `GraphQLError` with a stable `extensions.code` if the persisted
    query extension is invalid, unsupported, unknown or the store fails.

    # Required arguments

    `store`: a `PersistedQueryStore` instance.

    `data`: a `dict` with GraphQL request data.
    """
    extensions = data.get("extensions")
    if not isinstance(extensions, dict) or "persistedQuery" not in extensions:
        return None

    persisted_query = extensions["persistedQuery"]
    if not isinstance(persisted_query, dict):
        raise _persisted_query_error(
            "Persisted query extension is invalid.", ERROR_CODE_INVALID_HASH
        )

    if persisted_query.get("version") != PERSISTED_QUERY_VERSION:
        raise _persisted_query_error(
            "PersistedQueryNotSupported", ERROR_CODE_NOT_SUPPORTED
        )

    query_hash = persisted_query.get("sha256Hash")
    if not isinstance(query_hash, str) or not SHA256_HASH_RE.match(query_hash.lower()):
        raise _persisted_query_error(
            "Persisted query hash is not a valid SHA-256 hash.",
            ERROR_CODE_INVALID_HASH,
        )
    query_hash = query_hash.lower()

    query = data.get("query")
    if isinstance(query, str) and query:
        query_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        if query_digest != query_hash:
            raise _persisted_query_error(
                "Provided query does not match the persisted query hash.",
                ERROR_CODE_HASH_MISMATCH,
            )
        return query_hash, query

    if query is not None and not isinstance(query, str):
        # Query of invalid type, let the regular data validation report it.
        return None

    try:
        cached_query = store.get(query_hash)
    except Exception as error:
        raise _persisted_query_error(
            "Persisted query store failed to retrieve the query.",
            ERROR_CODE_STORE_ERROR,
            original_error=error,
        ) from error

    if cached_query is None:
        raise _persisted_query_error("PersistedQueryNotFound", ERROR_CODE_NOT_FOUND)

    data["query"] = cached_query
    return None


def register_persisted_query(
    store: PersistedQueryStore,
    query_hash: str,
    query: str,
) -> None:
    """Stores the `query` under its `query_hash` in the `store`.

    Raises `GraphQLError` if the store fails.

    # Required arguments

    `store`: a `PersistedQueryStore` instance.

    `query_hash`: a `str` with lowercase SHA-256 hexdigest of the query.

    `query`: a `str` with GraphQL query source.
    """
    try:
        store.set(query_hash, query)
    except Exception as error:
        raise _persisted_query_error(
            "Persisted query store failed to store the query.",
            ERROR_CODE_STORE_ERROR,
            original_error=error,
        ) from error


def _persisted_query_error(
    message: str,
    code: str,
    original_error: Exception | None = None,
) -> GraphQLError:
    return GraphQLError(
        message,
        extensions={"code": code},
        original_error=original_error,
    )
