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

    # Group representation positions by type, preserving the order in which
    # types first appeared in the request. Batches are built per request, so
    # no state is ever shared between requests.
    type_objects: dict[str, GraphQLObjectType] = {}
    batched_indices: dict[str, list[int]] = {}
    for index, reference in enumerate(representations):
        __typename = reference["__typename"]
        if __typename not in type_objects:
            type_object = info.schema.get_type(__typename)

            if not type_object or not isinstance(type_object, GraphQLObjectType):
                raise TypeError(
                    f"The `_entities` resolver tried to load an entity for"
                    f' type "{__typename}", but no object type of that name'
                    f" was found in the schema",
                )
            type_objects[__typename] = type_object
        batched_indices.setdefault(__typename, []).append(index)

    result: list[Any] = [None] * len(representations)

    for __typename, indices in batched_indices.items():
        type_object = type_objects[__typename]
        resolve_reference_batch = type_get_extension(
            type_object,
            "__resolve_reference_batch__",
        )

        if callable(resolve_reference_batch):
            resolve_entities_batch(
                result,
                resolve_reference_batch,
                type_object,
                info,
                [representations[index] for index in indices],
                indices,
                __typename,
            )
            continue

        resolve_reference = type_get_extension(
            type_object,
            "__resolve_reference__",
            lambda o, i, r: r,
        )

        for index in indices:
            representation = resolve_reference(
                type_object, info, representations[index]
            )

            if isawaitable(representation):
                result[index] = add_typename_to_async_return(
                    representation, __typename
                )
            else:
                result[index] = add_typename_to_possible_return(
                    representation, __typename
                )

    return result


def resolve_entities_batch(
    result: list[Any],
    resolve_reference_batch: Any,
    type_object: GraphQLObjectType,
    info: GraphQLResolveInfo,
    representations: list[Any],
    indices: list[int],
    __typename: str,
) -> None:
    try:
        batch_result = resolve_reference_batch(type_object, info, representations)
    except Exception as error:
        # The batch failed as a whole: report the error on every position
        # this batch was responsible for instead of swallowing it.
        for index in indices:
            result[index] = error
        return

    if isawaitable(batch_result):
        pending_batch = asyncio.ensure_future(
            await_batch_result(batch_result, __typename, len(indices))
        )
        for position, index in enumerate(indices):
            result[index] = add_typename_to_async_batch_item(
                pending_batch, position, __typename
            )
        return

    validate_batch_result(batch_result, __typename, len(indices))
    for position, index in enumerate(indices):
        result[index] = add_typename_to_possible_return(
            batch_result[position], __typename
        )


async def await_batch_result(
    batch_awaitable: Any, __typename: str, expected_count: int
) -> Any:
    batch_result = await batch_awaitable
    validate_batch_result(batch_result, __typename, expected_count)
    return batch_result


async def add_typename_to_async_batch_item(
    pending_batch: "asyncio.Future[Any]", position: int, __typename: str
) -> Any:
    batch_result = await pending_batch
    return add_typename_to_possible_return(batch_result[position], __typename)


def validate_batch_result(
    batch_result: Any, __typename: str, expected_count: int
) -> None:
    if not isinstance(batch_result, list | tuple):
        is_valid_length = False
    else:
        is_valid_length = len(batch_result) == expected_count

    if not is_valid_length:
        returned_count = (
            len(batch_result) if isinstance(batch_result, list | tuple) else "no"
        )
        raise TypeError(
            f'Batch reference resolver for type "{__typename}" returned '
            f"{returned_count} results for {expected_count} representations. "
            "Batch reference resolvers must return a list of results of the "
            "same length and order as the representations they received."
        )


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
        if isinstance(obj, Exception):
            # Exceptions are returned as-is so graphql-core converts them
            # into errors on their respective `_entities` list positions.
            return obj
        if isinstance(obj, dict):
            obj["__typename"] = typename
        else:
            setattr(obj, f"_{obj.__class__.__name__}__typename", typename)
        return obj
    return None
