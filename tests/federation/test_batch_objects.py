import asyncio
from copy import deepcopy
from unittest.mock import Mock

import pytest
from graphql import graphql, graphql_sync

from ariadne.contrib.federation import (
    FederatedObjectType,
    make_federated_schema,
)

type_defs = """
    type Query {
        hello: String
    }

    type Product @key(fields: "upc") {
        upc: Int
        name: String
    }

    type User @key(fields: "email") {
        email: String
        firstName: String
    }
"""

entities_query = """
    query GetEntities($representations: [_Any!]!) {
        _entities(representations: $representations) {
            ... on Product {
                __typename
                upc
                name
            }
            ... on User {
                __typename
                email
                firstName
            }
        }
    }
"""

PRODUCTS = {
    1: "Apollo Gateway",
    2: "Apollo Studio",
}

USERS = {
    "jane@example.com": "Jane",
    "john@example.com": "John",
}


def product_batch_resolver(_obj, _info, representations):
    return [
        {"upc": rep["upc"], "name": PRODUCTS.get(rep["upc"])}
        for rep in representations
    ]


def user_batch_resolver(_obj, _info, representations):
    return [
        {"email": rep["email"], "firstName": USERS.get(rep["email"])}
        for rep in representations
    ]


def make_schema(product_batch=None, user_batch=None, product_single=None):
    product = FederatedObjectType("Product")
    user = FederatedObjectType("User")

    if product_batch is not None:
        product.batch_reference_resolver(product_batch)
    if product_single is not None:
        product.reference_resolver(product_single)
    if user_batch is not None:
        user.batch_reference_resolver(user_batch)

    return make_federated_schema(type_defs, product, user)


def execute_sync(schema, representations):
    return graphql_sync(
        schema,
        entities_query,
        variable_values={"representations": representations},
    )


async def execute_async(schema, representations):
    return await graphql(
        schema,
        entities_query,
        variable_values={"representations": representations},
    )


def test_batch_resolver_is_called_once_with_all_representations_in_order():
    batch = Mock(side_effect=product_batch_resolver)
    schema = make_schema(product_batch=batch)

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "Product", "upc": 2},
        {"__typename": "Product", "upc": 1},
    ]
    result = execute_sync(schema, representations)

    assert result.errors is None
    assert batch.call_count == 1
    assert batch.call_args.args[2] == representations
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"},
        {"__typename": "Product", "upc": 2, "name": "Apollo Studio"},
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"},
    ]


def test_batch_resolver_can_be_set_using_decorator():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    def resolve_products(_obj, _info, representations):
        return [
            {"upc": rep["upc"], "name": PRODUCTS[rep["upc"]]}
            for rep in representations
        ]

    schema = make_federated_schema(type_defs, product)
    result = execute_sync(schema, [{"__typename": "Product", "upc": 1}])

    assert result.errors is None
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"}
    ]


def test_batch_resolver_can_be_set_using_decorator_call():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver()
    def resolve_products(_obj, _info, representations):
        return [
            {"upc": rep["upc"], "name": PRODUCTS[rep["upc"]]}
            for rep in representations
        ]

    schema = make_federated_schema(type_defs, product)
    result = execute_sync(schema, [{"__typename": "Product", "upc": 1}])

    assert result.errors is None
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"}
    ]


def test_mixed_entity_types_keep_input_order_and_batch_per_type():
    product_batch = Mock(side_effect=product_batch_resolver)
    user_batch = Mock(side_effect=user_batch_resolver)
    schema = make_schema(product_batch=product_batch, user_batch=user_batch)

    representations = [
        {"__typename": "User", "email": "jane@example.com"},
        {"__typename": "Product", "upc": 2},
        {"__typename": "User", "email": "john@example.com"},
        {"__typename": "Product", "upc": 1},
    ]
    result = execute_sync(schema, representations)

    assert result.errors is None
    assert product_batch.call_count == 1
    assert user_batch.call_count == 1
    assert product_batch.call_args.args[2] == [
        {"__typename": "Product", "upc": 2},
        {"__typename": "Product", "upc": 1},
    ]
    assert user_batch.call_args.args[2] == [
        {"__typename": "User", "email": "jane@example.com"},
        {"__typename": "User", "email": "john@example.com"},
    ]
    assert result.data["_entities"] == [
        {
            "__typename": "User",
            "email": "jane@example.com",
            "firstName": "Jane",
        },
        {"__typename": "Product", "upc": 2, "name": "Apollo Studio"},
        {
            "__typename": "User",
            "email": "john@example.com",
            "firstName": "John",
        },
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"},
    ]


def test_type_without_batch_resolver_uses_single_reference_resolver():
    product_single = Mock(
        side_effect=lambda _obj, _info, rep: {
            "upc": rep["upc"],
            "name": PRODUCTS[rep["upc"]],
        }
    )
    user_batch = Mock(side_effect=user_batch_resolver)
    schema = make_schema(product_single=product_single, user_batch=user_batch)

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "User", "email": "jane@example.com"},
        {"__typename": "Product", "upc": 2},
    ]
    result = execute_sync(schema, representations)

    assert result.errors is None
    assert product_single.call_count == 2
    assert user_batch.call_count == 1
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"},
        {
            "__typename": "User",
            "email": "jane@example.com",
            "firstName": "Jane",
        },
        {"__typename": "Product", "upc": 2, "name": "Apollo Studio"},
    ]


def test_batch_resolver_takes_precedence_over_single_resolver():
    product_single = Mock(
        side_effect=lambda _obj, _info, rep: {"upc": rep["upc"], "name": "Single"}
    )
    product_batch = Mock(side_effect=product_batch_resolver)
    schema = make_schema(
        product_batch=product_batch,
        product_single=product_single,
    )

    result = execute_sync(schema, [{"__typename": "Product", "upc": 1}])

    assert result.errors is None
    assert product_batch.call_count == 1
    assert product_single.call_count == 0
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"}
    ]


@pytest.mark.asyncio
async def test_async_batch_resolver():
    calls = []

    async def async_product_batch(_obj, _info, representations):
        await asyncio.sleep(0)
        calls.append(list(representations))
        return product_batch_resolver(_obj, _info, representations)

    schema = make_schema(product_batch=async_product_batch)

    representations = [
        {"__typename": "Product", "upc": 2},
        {"__typename": "Product", "upc": 1},
    ]
    result = await execute_async(schema, representations)

    assert result.errors is None
    assert calls == [representations]
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 2, "name": "Apollo Studio"},
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"},
    ]


@pytest.mark.asyncio
async def test_duplicate_representations_keep_their_positions():
    async def async_user_batch(_obj, _info, representations):
        await asyncio.sleep(0)
        return user_batch_resolver(_obj, _info, representations)

    schema = make_schema(user_batch=async_user_batch)

    representations = [
        {"__typename": "User", "email": "jane@example.com"},
        {"__typename": "User", "email": "john@example.com"},
        {"__typename": "User", "email": "jane@example.com"},
    ]
    result = await execute_async(schema, representations)

    assert result.errors is None
    assert result.data["_entities"] == [
        {"__typename": "User", "email": "jane@example.com", "firstName": "Jane"},
        {"__typename": "User", "email": "john@example.com", "firstName": "John"},
        {"__typename": "User", "email": "jane@example.com", "firstName": "Jane"},
    ]


def test_none_results_keep_their_positions():
    def batch_with_none(_obj, _info, representations):
        return [None, {"upc": 2, "name": "Apollo Studio"}, None]

    schema = make_schema(product_batch=batch_with_none)

    representations = [
        {"__typename": "Product", "upc": 99},
        {"__typename": "Product", "upc": 2},
        {"__typename": "Product", "upc": 98},
    ]
    result = execute_sync(schema, representations)

    assert result.errors is None
    assert result.data["_entities"] == [
        None,
        {"__typename": "Product", "upc": 2, "name": "Apollo Studio"},
        None,
    ]


@pytest.mark.asyncio
async def test_exception_in_batch_results_fails_only_that_position():
    def partial_batch(_obj, _info, representations):
        return [
            {"upc": 1, "name": "Apollo Gateway"},
            ValueError("Product not found"),
            {"upc": 2, "name": "Apollo Studio"},
        ]

    schema = make_schema(product_batch=partial_batch, user_batch=user_batch_resolver)

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "Product", "upc": 99},
        {"__typename": "Product", "upc": 2},
        {"__typename": "User", "email": "jane@example.com"},
    ]
    result = await execute_async(schema, representations)

    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"},
        None,
        {"__typename": "Product", "upc": 2, "name": "Apollo Studio"},
        {"__typename": "User", "email": "jane@example.com", "firstName": "Jane"},
    ]
    assert result.errors is not None
    assert len(result.errors) == 1
    assert result.errors[0].message == "Product not found"
    assert result.errors[0].path == ["_entities", 1]


@pytest.mark.asyncio
async def test_exception_in_async_batch_results_fails_only_that_position():
    async def partial_async_batch(_obj, _info, representations):
        await asyncio.sleep(0)
        return [
            ValueError("Product not found"),
            {"upc": 2, "name": "Apollo Studio"},
        ]

    schema = make_schema(product_batch=partial_async_batch)

    representations = [
        {"__typename": "Product", "upc": 99},
        {"__typename": "Product", "upc": 2},
    ]
    result = await execute_async(schema, representations)

    assert result.data["_entities"] == [
        None,
        {"__typename": "Product", "upc": 2, "name": "Apollo Studio"},
    ]
    assert result.errors is not None
    assert len(result.errors) == 1
    assert result.errors[0].message == "Product not found"
    assert result.errors[0].path == ["_entities", 0]


@pytest.mark.asyncio
async def test_batch_resolver_error_fails_all_positions_of_that_type():
    def failing_batch(_obj, _info, representations):
        raise RuntimeError("Database is down")

    schema = make_schema(product_batch=failing_batch, user_batch=user_batch_resolver)

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "User", "email": "jane@example.com"},
        {"__typename": "Product", "upc": 2},
    ]
    result = await execute_async(schema, representations)

    assert result.data["_entities"] == [
        None,
        {"__typename": "User", "email": "jane@example.com", "firstName": "Jane"},
        None,
    ]
    assert result.errors is not None
    assert len(result.errors) == 2
    assert {tuple(error.path) for error in result.errors} == {
        ("_entities", 0),
        ("_entities", 2),
    }
    assert all(error.message == "Database is down" for error in result.errors)


@pytest.mark.asyncio
async def test_async_batch_resolver_error_fails_all_positions_of_that_type():
    async def failing_async_batch(_obj, _info, representations):
        await asyncio.sleep(0)
        raise RuntimeError("Database is down")

    schema = make_schema(product_batch=failing_async_batch)

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "Product", "upc": 2},
    ]
    result = await execute_async(schema, representations)

    assert result.data["_entities"] == [None, None]
    assert result.errors is not None
    assert len(result.errors) == 2
    assert {tuple(error.path) for error in result.errors} == {
        ("_entities", 0),
        ("_entities", 1),
    }


@pytest.mark.asyncio
async def test_batch_result_length_mismatch_is_reported():
    def short_batch(_obj, _info, representations):
        return [{"upc": 1, "name": "Apollo Gateway"}]

    schema = make_schema(product_batch=short_batch)

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "Product", "upc": 2},
    ]
    result = await execute_async(schema, representations)

    assert result.data["_entities"] == [None, None]
    assert result.errors is not None
    assert len(result.errors) == 2
    for error in result.errors:
        assert "returned 1 results" in error.message
        assert "2 representations" in error.message


@pytest.mark.asyncio
async def test_async_batch_result_length_mismatch_is_reported():
    async def short_async_batch(_obj, _info, representations):
        await asyncio.sleep(0)
        return [{"upc": 1, "name": "Apollo Gateway"}]

    schema = make_schema(product_batch=short_async_batch)

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "Product", "upc": 2},
    ]
    result = await execute_async(schema, representations)

    assert result.data["_entities"] == [None, None]
    assert result.errors is not None
    assert len(result.errors) == 2
    for error in result.errors:
        assert "returned 1 results" in error.message
        assert "2 representations" in error.message


@pytest.mark.asyncio
async def test_non_list_batch_result_is_reported():
    def invalid_batch(_obj, _info, representations):
        return {"upc": 1, "name": "Apollo Gateway"}

    schema = make_schema(product_batch=invalid_batch)

    representations = [{"__typename": "Product", "upc": 1}]
    result = await execute_async(schema, representations)

    assert result.data["_entities"] == [None]
    assert result.errors is not None
    assert len(result.errors) == 1
    assert "dict instead of a list" in result.errors[0].message


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_batches():
    calls = []

    async def async_product_batch(_obj, _info, representations):
        await asyncio.sleep(0.01)
        calls.append([dict(rep) for rep in representations])
        return product_batch_resolver(_obj, _info, representations)

    schema = make_schema(product_batch=async_product_batch)

    first = [{"__typename": "Product", "upc": 1}]
    second = [
        {"__typename": "Product", "upc": 2},
        {"__typename": "Product", "upc": 1},
    ]
    first_result, second_result = await asyncio.gather(
        execute_async(schema, first),
        execute_async(schema, second),
    )

    assert first_result.errors is None
    assert second_result.errors is None
    assert sorted(calls, key=len) == [first, second]
    assert first_result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"}
    ]
    assert second_result.data["_entities"] == [
        {"__typename": "Product", "upc": 2, "name": "Apollo Studio"},
        {"__typename": "Product", "upc": 1, "name": "Apollo Gateway"},
    ]


def test_input_representations_are_not_modified():
    schema = make_schema(
        product_batch=product_batch_resolver,
        user_batch=user_batch_resolver,
    )

    representations = [
        {"__typename": "User", "email": "jane@example.com"},
        {"__typename": "Product", "upc": 1},
    ]
    snapshot = deepcopy(representations)
    result = execute_sync(schema, representations)

    assert result.errors is None
    assert representations == snapshot
