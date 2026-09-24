from typing import cast

from graphql import GraphQLNamedType
from graphql.type import GraphQLSchema

from ...objects import ObjectType
from ...types import Resolver
from ...utils import type_set_extension


class FederatedObjectType(ObjectType):
    """Add `__referenceResolver` to objects as per apollo-federation."""

    _reference_resolver: Resolver | None = None
    _batch_reference_resolver: Resolver | None = None

    def reference_resolver(self, arg: Resolver | None = None) -> Resolver:
        def register_reference_resolver(f: Resolver) -> Resolver:
            self._reference_resolver = f
            return f

        if callable(arg):
            return register_reference_resolver(arg)
        return register_reference_resolver

    def batch_reference_resolver(self, arg: Resolver | None = None) -> Resolver:
        """Register a batch reference resolver for this federated type.

        Batch reference resolver is called once per `_entities` request with
        a list of all representations of this type (in the order they appeared
        in the request) and should return a list of results of the same length
        and order. It takes precedence over the single reference resolver when
        both are registered.
        """

        def register_batch_reference_resolver(f: Resolver) -> Resolver:
            self._batch_reference_resolver = f
            return f

        if callable(arg):
            return register_batch_reference_resolver(arg)
        return register_batch_reference_resolver

    def bind_to_schema(self, schema: GraphQLSchema) -> None:
        super().bind_to_schema(schema)

        if callable(self._reference_resolver) or callable(
            self._batch_reference_resolver
        ):
            graphql_type = schema.type_map.get(self.name)
            graphql_type = cast(GraphQLNamedType, graphql_type)
            if callable(self._reference_resolver):
                type_set_extension(
                    graphql_type,
                    "__resolve_reference__",
                    self._reference_resolver,
                )
            if callable(self._batch_reference_resolver):
                type_set_extension(
                    graphql_type,
                    "__resolve_reference_batch__",
                    self._batch_reference_resolver,
                )
