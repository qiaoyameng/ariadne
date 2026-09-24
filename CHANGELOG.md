# CHANGELOG

All notable unreleased changes to this project will be documented in this file.

For released versions, see the [Releases](https://github.com/mirumee/ariadne/releases) page.

## Unreleased

- Added `batch_reference_resolver` to `FederatedObjectType`, allowing federation entities to be resolved in batches (one call per type per `_entities` request) instead of one resolver call per representation. Supports sync and async resolvers, per-position errors, and takes precedence over `reference_resolver` when both are registered.
