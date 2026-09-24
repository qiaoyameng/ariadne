from collections.abc import Awaitable, Callable
from typing import Any, cast

from graphql import GraphQLNamedType, GraphQLObjectType, GraphQLResolveInfo
from graphql.type import GraphQLSchema

from ...objects import ObjectType
from ...types import Resolver
from ...utils import type_set_extension

BatchReferenceResolver = Callable[
    [GraphQLObjectType, GraphQLResolveInfo, list[dict[str, Any]]],
    list[Any] | Awaitable[list[Any]],
]


class FederatedObjectType(ObjectType):
    """Add `__referenceResolver` to objects as per apollo-federation."""

    _reference_resolver: Resolver | None = None
    _batch_reference_resolver: BatchReferenceResolver | None = None

    def reference_resolver(self, arg: Resolver | None = None) -> Resolver:
        def register_reference_resolver(f: Resolver) -> Resolver:
            self._reference_resolver = f
            return f

        if callable(arg):
            return register_reference_resolver(arg)
        return register_reference_resolver

    def batch_reference_resolver(
        self, arg: BatchReferenceResolver | None = None
    ) -> BatchReferenceResolver:
        """Register a batch reference resolver for this federated type.

        Batch reference resolver is called once per `_entities` request with
        all representations for this type (in the order they appeared in the
        request) and should return a list of results of matching length.

        When both `reference_resolver` and `batch_reference_resolver` are
        registered for the same type, the batch resolver takes precedence.
        """

        def register_batch_reference_resolver(
            f: BatchReferenceResolver,
        ) -> BatchReferenceResolver:
            self._batch_reference_resolver = f
            return f

        if callable(arg):
            return register_batch_reference_resolver(arg)
        return register_batch_reference_resolver

    def bind_to_schema(self, schema: GraphQLSchema) -> None:
        super().bind_to_schema(schema)

        if callable(self._batch_reference_resolver):
            graphql_type = schema.type_map.get(self.name)
            graphql_type = cast(GraphQLNamedType, graphql_type)
            type_set_extension(
                graphql_type,
                "__resolve_references__",
                self._batch_reference_resolver,
            )

        if callable(self._reference_resolver):
            graphql_type = schema.type_map.get(self.name)
            graphql_type = cast(GraphQLNamedType, graphql_type)
            type_set_extension(
                graphql_type,
                "__resolve_reference__",
                self._reference_resolver,
            )
