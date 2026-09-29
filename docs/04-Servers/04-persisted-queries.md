---
id: persisted-queries
title: Automatic persisted queries
---

Ariadne supports [automatic persisted queries](https://www.apollographql.com/docs/apollo-server/performance/apq/) (APQ) in its ASGI and WSGI applications.

With APQ enabled, clients can send a SHA-256 hash of the query in the `extensions.persistedQuery` attribute of the request instead of the query itself:

```json
{
    "extensions": {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": "ecf4edb46db40b5132295c0291d62fb65d6759a9eedfa4d5d612dd5ec54a6b38"
        }
    }
}
```

If the hash is not known to the server, a GraphQL error with the `PersistedQueryNotFound` code is returned. The client should then retry the request with both the `query` and the hash, which registers the query for future use:

```json
{
    "query": "{ hello }",
    "extensions": {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": "ecf4edb46db40b5132295c0291d62fb65d6759a9eedfa4d5d612dd5ec54a6b38"
        }
    }
}
```

The `sha256Hash` must be a SHA-256 hash of the query's exact UTF-8 representation. When both `query` and hash are sent, the server verifies that they match before executing the query. Queries that fail parsing or validation are never stored.

Only the query text is cached. Every request is still parsed, validated and executed against the current schema, so validation rules, introspection policies and the ban on mutations over `GET` requests apply to persisted queries the same way they apply to regular ones.

APQ is disabled by default. To enable it, pass a `persisted_query_store` to the application:

```python
from ariadne import InMemoryPersistedQueryStore
from ariadne.asgi import GraphQL

app = GraphQL(
    schema,
    persisted_query_store=InMemoryPersistedQueryStore(max_size=1024),
)
```

The same option is available on the WSGI `GraphQL` application and the ASGI `GraphQLHTTPHandler`. Both transports share the same store interface, so a single store instance can be reused between them.


## Error codes

APQ errors are reported as GraphQL errors with a stable `code` in error's `extensions`:

- `PersistedQueryNotFound`: hash-only request didn't match any stored query.
- `PersistedQueryNotSupported`: `persistedQuery.version` other than `1`.
- `PersistedQueryInvalidHash`: `sha256Hash` is not a valid SHA-256 hash.
- `PersistedQueryHashMismatch`: sent `query` doesn't match the sent hash.


## Custom stores

A store is any object implementing the synchronous `PersistedQueryStore` protocol:

```python
class PersistedQueryStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, query: str) -> None: ...
```

Both methods are synchronous and are called directly by sync (WSGI) and async (ASGI) servers alike, so custom implementations should be thread-safe and non-blocking. Exceptions raised by the store are reported to the client through the standard GraphQL error handling.

`InMemoryPersistedQueryStore` is the included implementation. It keeps up to `max_size` queries in memory, evicting the least recently used ones when the limit is exceeded.
