# CHANGELOG

All notable unreleased changes to this project will be documented in this file.

For released versions, see the [Releases](https://github.com/mirumee/ariadne/releases) page.

## Unreleased

- Added `operation_aware` and `operation_name` options to `cost_validator`. In operation-aware mode only the operation selected by `operationName` is billed, using coerced variables (including operation and input object defaults), honoring `@skip`/`@include`, merging fields by response key, and billing abstract types as the most expensive possible concrete type.
