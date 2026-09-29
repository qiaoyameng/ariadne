---
id: persisted-queries
title: Automatic persisted queries
---

Ariadne's ASGI and WSGI applications optionally support
[automatic persisted queries](https://www.apollographql.com/docs/apollo-server/performance/apq/)
(APQ). With APQ, clients send a SHA-256 hash of the query instead of the query
itself, reducing request sizes for repeated queries.

APQ support is disabled by default. To enable it, pass a
`persisted_query_store` to the `GraphQL` application:

```python
from ariadne import InMemoryPersistedQueryStore
from ariadne.asgi import GraphQL

store = InMemoryPersistedQueryStore(max_size=1024)
app = GraphQL(schema, persisted_query_store=store)
```

The same option is available on the WSGI `GraphQL` application. Both
transports share the same store implementation and behavior.


## Protocol

Clients communicate persisted queries through the `extensions.persistedQuery`
field of the GraphQL request:

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

`sha256Hash` must be the SHA-256 hash of the query's UTF-8 source. Only
`version` `1` is supported.

- If the request contains only the hash and the hash is not known to the
  store, a GraphQL error with the `PersistedQueryNotFound` code is returned.
- If the request contains both the `query` and the hash, the hash is verified
  against the query. The query is then executed and, if it passes parsing and
  validation, registered in the store so subsequent requests can use the hash
  alone.
- On a cache hit, the stored query source is used for the request, but the
  query is still parsed, validated and executed against the current schema on
  every request. Operation selection, variables, validation rules,
  introspection restrictions and other per-request checks all apply as usual.
  In particular, mutations are never executed over `GET` requests, even when
  served from the cache.

Persisted queries also work over `GET` requests (when `execute_get_queries`
is enabled) by passing the `extensions` as a JSON-encoded query string
parameter.

Invalid or malicious documents never enter the store: registration happens
only after the query was successfully parsed and validated.

Errors are reported as GraphQL errors with stable codes in
`errors[].extensions.code`:

| Code | Reason |
|------|--------|
| `PersistedQueryNotFound` | Hash is not present in the store. |
| `PersistedQueryNotSupported` | Unsupported `persistedQuery` version. |
| `PersistedQueryInvalidHash` | `sha256Hash` is not a valid SHA-256 hash. |
| `PersistedQueryHashMismatch` | Provided query doesn't match the hash. |
| `PersistedQueryStoreError` | The store raised an exception. |


## Custom stores

A persisted query store is any object implementing the `PersistedQueryStore`
protocol with two synchronous methods:

```python
class PersistedQueryStore(Protocol):
    def get(self, query_hash: str) -> str | None: ...
    def set(self, query_hash: str, query: str) -> None: ...
```

Both methods are synchronous and are called directly by both the WSGI and the
ASGI servers. Stores only keep the query source text, keyed by its hash.

`InMemoryPersistedQueryStore` is the included implementation. It keeps up to
`max_size` queries in memory, evicting the least recently used ones, and is
thread-safe. Eviction only affects whether a later lookup hits the cache; a
hash is never remapped to a different query.
