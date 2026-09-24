import asyncio
from inspect import isawaitable
from typing import Any, cast

from graphql import (
    DirectiveDefinitionNode,
    Node,
    parse,
    print_ast,
)
from graphql.language import DirectiveNode
from graphql.type import (
    GraphQLInputObjectType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLResolveInfo,
    GraphQLSchema,
)

from ariadne.utils import type_get_extension

_allowed_directives = [
    "skip",  # Default directive as per specs.
    "include",  # Default directive as per specs.
    "deprecated",  # Default directive as per specs.
    "external",  # Federation directive.
    "requires",  # Federation directive.
    "provides",  # Federation directive.
    "key",  # Federation directive.
    "extends",  # Federation directive.
    "link",  # Federation 2 directive.
    "shareable",  # Federation 2 directive.
    "tag",  # Federation 2 directive.
    "override",  # Federation 2 directive.
    "inaccessible",  # Federation 2 directive.
    "composeDirective",  # Federation 2.1 directive.
    "interfaceObject",  # Federation 2.3 directive.
    "authenticated",  # Federation 2.5 directive.
    "requiresScopes",  # Federation 2.5 directive.
    "policy",  # Federation 2.6 directive.
]


def _purge_directive_nodes(nodes: tuple[Node, ...]) -> tuple[Node, ...]:
    return tuple(
        node
        for node in nodes
        if not isinstance(node, DirectiveNode | DirectiveDefinitionNode)
        or node.name.value in _allowed_directives
    )


def _purge_type_directives(definition: Node):
    # Recursively check every field defined on the Node definition
    # and remove any directives found.
    for key in definition.keys:
        value = getattr(definition, key, None)
        if isinstance(value, tuple):
            # Remove directive nodes from the tuple
            # e.g. doc -> definitions [DirectiveDefinitionNode]
            next_value = _purge_directive_nodes(cast(tuple[Node, ...], value))
            for item in next_value:
                if isinstance(item, Node):
                    # Look for directive nodes on sub-nodes, e.g.: doc ->
                    # definitions [ObjectTypeDefinitionNode] -> fields -> directives
                    _purge_type_directives(item)
            setattr(definition, key, next_value)
        elif isinstance(value, Node):
            _purge_type_directives(value)


def purge_schema_directives(joined_type_defs: str) -> str:
    """Remove custom schema directives from federation."""
    ast_document = parse(joined_type_defs)
    _purge_type_directives(ast_document)
    return print_ast(ast_document)


def resolve_entities(_: Any, info: GraphQLResolveInfo, **kwargs) -> Any:
    representations = list(kwargs.get("representations", []))

    results: list[Any] = [None] * len(representations)
    batched_typenames: dict[str, tuple[GraphQLObjectType, Any, list[int]]] = {}

    for index, reference in enumerate(representations):
        __typename = reference["__typename"]
        type_object = info.schema.get_type(__typename)

        if not type_object or not isinstance(type_object, GraphQLObjectType):
            raise TypeError(
                f"The `_entities` resolver tried to load an entity for"
                f' type "{__typename}", but no object type of that name'
                f" was found in the schema",
            )

        batch_resolver = type_get_extension(
            type_object,
            "__resolve_references__",
            None,
        )

        if batch_resolver is not None:
            if __typename in batched_typenames:
                batched_typenames[__typename][2].append(index)
            else:
                batched_typenames[__typename] = (type_object, batch_resolver, [index])
            continue

        resolve_reference = type_get_extension(
            type_object,
            "__resolve_reference__",
            lambda o, i, r: reference,
        )

        representation = resolve_reference(type_object, info, reference)

        if isawaitable(representation):
            results[index] = add_typename_to_async_return(representation, __typename)
        else:
            results[index] = add_typename_to_possible_return(representation, __typename)

    for __typename, (type_object, batch_resolver, indices) in batched_typenames.items():
        resolve_entities_batch(
            type_object,
            batch_resolver,
            [representations[index] for index in indices],
            indices,
            results,
            __typename,
            info,
        )

    return results


def resolve_entities_batch(
    type_object: GraphQLObjectType,
    batch_resolver: Any,
    references: list[Any],
    indices: list[int],
    results: list[Any],
    __typename: str,
    info: GraphQLResolveInfo,
) -> None:
    try:
        batch = batch_resolver(type_object, info, references)
    except Exception as error:
        for index in indices:
            results[index] = raise_entity_error(error)
        return

    if isawaitable(batch):
        shared_batch = asyncio.ensure_future(
            await_validated_batch(batch, __typename, len(indices))
        )
        for position, index in enumerate(indices):
            results[index] = resolve_entity_from_batch(
                shared_batch, position, __typename
            )
        return

    try:
        validate_batch_results(batch, __typename, len(indices))
    except Exception as error:
        for index in indices:
            results[index] = raise_entity_error(error)
        return

    for position, index in enumerate(indices):
        entity = batch[position]
        if isinstance(entity, Exception):
            results[index] = raise_entity_error(entity)
        else:
            results[index] = add_typename_to_possible_return(entity, __typename)


async def await_validated_batch(
    batch: Any, __typename: str, expected_count: int
) -> list[Any]:
    return validate_batch_results(await batch, __typename, expected_count)


async def resolve_entity_from_batch(
    shared_batch: "asyncio.Future[list[Any]]",
    position: int,
    __typename: str,
) -> Any:
    batch = await shared_batch
    entity = batch[position]
    if isinstance(entity, Exception):
        raise entity
    return add_typename_to_possible_return(entity, __typename)


async def raise_entity_error(error: Exception) -> Any:
    raise error


def validate_batch_results(
    batch: Any, __typename: str, expected_count: int
) -> list[Any]:
    if not isinstance(batch, list | tuple) or len(batch) != expected_count:
        returned = (
            f"{len(batch)} results"
            if isinstance(batch, list | tuple)
            else f"{type(batch).__name__} instead of a list"
        )
        raise ValueError(
            f'Batch reference resolver for type "{__typename}" returned '
            f"{returned}, but {expected_count} representations were sent. "
            "Batch reference resolvers must return a list with exactly one "
            "result per representation, in the same order."
        )
    return list(batch)


async def add_typename_to_async_return(obj: Any, typename: str) -> Any:
    return add_typename_to_possible_return(await obj, typename)


def get_entity_types(schema: GraphQLSchema) -> list[GraphQLNamedType]:
    """Get all types that include the @key directive."""
    schema_types = schema.type_map.values()

    def check_type(t):
        return isinstance(t, GraphQLObjectType) and includes_directive(t, "key")

    return [t for t in schema_types if check_type(t)]


def includes_directive(
    type_object: GraphQLNamedType,
    directive_name: str,
) -> bool:
    """Check if specified type includes a directive."""
    if isinstance(type_object, GraphQLInputObjectType):
        return False

    directives = gather_directives(type_object)
    return any(d.name.value == directive_name for d in directives)


def gather_directives(
    type_object: GraphQLNamedType,
) -> list[DirectiveNode]:
    """Get all directive attached to a type."""
    directives: list[DirectiveNode] = []

    if hasattr(type_object, "extension_ast_nodes") and type_object.extension_ast_nodes:
        for ast_node in type_object.extension_ast_nodes:
            if ast_node.directives:
                directives.extend(ast_node.directives)

    if (
        hasattr(type_object, "ast_node")
        and type_object.ast_node
        and type_object.ast_node.directives
    ):
        directives.extend(type_object.ast_node.directives)

    return directives


def add_typename_to_possible_return(obj: Any, typename: str) -> Any:
    if obj is not None:
        if isinstance(obj, dict):
            obj["__typename"] = typename
        else:
            setattr(obj, f"_{obj.__class__.__name__}__typename", typename)
        return obj
    return None
