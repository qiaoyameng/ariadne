# CHANGELOG

All notable unreleased changes to this project will be documented in this file.

For released versions, see the [Releases](https://github.com/mirumee/ariadne/releases) page.

## Unreleased

- Added support for asynchronous `request_started`, `request_finished`, `has_errors` and `format` extension hooks in the asynchronous `graphql` query executor. Synchronous `graphql_sync` now raises `RuntimeError` when an extension hook returns an awaitable.
