import asyncio
import copy
from unittest.mock import Mock

import pytest
from graphql import graphql, graphql_sync

from ariadne.contrib.federation import (
    FederatedObjectType,
    make_federated_schema,
)

TYPE_DEFS = """
    type Query {
        rootField: String
    }

    type Product @key(fields: "upc") {
        upc: Int
        name: String
    }

    type User @key(fields: "id") {
        id: Int
        firstName: String
    }
"""

ENTITIES_QUERY = """
    query GetEntities($representations: [_Any!]!) {
        _entities(representations: $representations) {
            ... on Product {
                __typename
                upc
                name
            }
            ... on User {
                __typename
                id
                firstName
            }
        }
    }
"""


def make_product_batch_resolver(calls):
    def resolve_product_references(_obj, _info, representations):
        calls.append([r["upc"] for r in representations])
        return [
            {"upc": r["upc"], "name": f"Product {r['upc']}"} for r in representations
        ]

    return resolve_product_references


def make_user_batch_resolver(calls):
    def resolve_user_references(_obj, _info, representations):
        calls.append([r["id"] for r in representations])
        return [
            {"id": r["id"], "firstName": f"User {r['id']}"} for r in representations
        ]

    return resolve_user_references


def execute_entities(schema, representations):
    return graphql_sync(
        schema,
        ENTITIES_QUERY,
        variable_values={"representations": representations},
    )


def test_batch_resolver_called_once_per_type_with_ordered_representations():
    product_calls, user_calls = [], []

    product = FederatedObjectType("Product")
    product.batch_reference_resolver(make_product_batch_resolver(product_calls))

    user = FederatedObjectType("User")
    user.batch_reference_resolver(make_user_batch_resolver(user_calls))

    schema = make_federated_schema(TYPE_DEFS, [product, user])

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "User", "id": 1},
        {"__typename": "Product", "upc": 2},
        {"__typename": "User", "id": 2},
        {"__typename": "Product", "upc": 1},
    ]
    snapshot = copy.deepcopy(representations)

    result = execute_entities(schema, representations)

    assert result.errors is None
    assert product_calls == [[1, 2, 1]]
    assert user_calls == [[1, 2]]
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Product 1"},
        {"__typename": "User", "id": 1, "firstName": "User 1"},
        {"__typename": "Product", "upc": 2, "name": "Product 2"},
        {"__typename": "User", "id": 2, "firstName": "User 2"},
        {"__typename": "Product", "upc": 1, "name": "Product 1"},
    ]
    assert representations == snapshot


def test_batch_reference_resolver_can_be_set_using_setter():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    def resolve_product_references(_obj, _info, representations):
        return [{"name": "Malbec"} for _ in representations]

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = execute_entities(schema, [{"__typename": "Product", "upc": 1}])

    assert result.errors is None
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": None, "name": "Malbec"}
    ]


def test_batch_reference_resolver_can_be_set_using_decorator_call():
    product = FederatedObjectType("Product")
    product.batch_reference_resolver()(
        lambda _obj, _info, representations: [
            {"name": "Malbec"} for _ in representations
        ]
    )

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = execute_entities(schema, [{"__typename": "Product", "upc": 1}])

    assert result.errors is None
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": None, "name": "Malbec"}
    ]


def test_single_reference_resolver_is_used_when_no_batch_resolver_is_registered():
    product = FederatedObjectType("Product")
    reference_resolver = Mock(side_effect=lambda _obj, _info, r: {"name": "Malbec"})
    product.reference_resolver(reference_resolver)

    schema = make_federated_schema(TYPE_DEFS, [product])

    representations = [
        {"__typename": "Product", "upc": 1},
        {"__typename": "Product", "upc": 1},
    ]
    result = execute_entities(schema, representations)

    assert result.errors is None
    assert reference_resolver.call_count == 2
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": None, "name": "Malbec"},
        {"__typename": "Product", "upc": None, "name": "Malbec"},
    ]


def test_batch_reference_resolver_takes_precedence_over_single_reference_resolver():
    product = FederatedObjectType("Product")
    single_resolver = Mock(return_value={"name": "Single"})
    product.reference_resolver(single_resolver)

    batch_calls = []

    @product.batch_reference_resolver
    def resolve_product_references(_obj, _info, representations):
        batch_calls.append(representations)
        return [{"name": "Batch"} for _ in representations]

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = execute_entities(
        schema,
        [
            {"__typename": "Product", "upc": 1},
            {"__typename": "Product", "upc": 2},
        ],
    )

    assert result.errors is None
    assert single_resolver.call_count == 0
    assert len(batch_calls) == 1
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": None, "name": "Batch"},
        {"__typename": "Product", "upc": None, "name": "Batch"},
    ]


def test_batch_reference_resolver_returning_none_keeps_position():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    def resolve_product_references(_obj, _info, representations):
        return [
            {"name": "Malbec"} if r["upc"] != 2 else None for r in representations
        ]

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = execute_entities(
        schema,
        [
            {"__typename": "Product", "upc": 1},
            {"__typename": "Product", "upc": 2},
            {"__typename": "Product", "upc": 3},
        ],
    )

    assert result.errors is None
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": None, "name": "Malbec"},
        None,
        {"__typename": "Product", "upc": None, "name": "Malbec"},
    ]


def test_batch_reference_resolver_length_mismatch_is_reported():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    def resolve_product_references(_obj, _info, representations):
        return [{"name": "Malbec"}]  # too few results

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = execute_entities(
        schema,
        [
            {"__typename": "Product", "upc": 1},
            {"__typename": "Product", "upc": 2},
        ],
    )

    assert result.errors
    assert len(result.errors) == 1
    assert (
        'Batch reference resolver for type "Product" returned 1 results '
        "for 2 representations" in result.errors[0].message
    )


def test_batch_reference_resolver_returning_non_list_is_reported():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    def resolve_product_references(_obj, _info, representations):
        return {"name": "Malbec"}  # not a list of results

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = execute_entities(schema, [{"__typename": "Product", "upc": 1}])

    assert result.errors
    assert len(result.errors) == 1
    assert (
        'Batch reference resolver for type "Product" returned no results '
        "for 1 representations" in result.errors[0].message
    )


def test_batch_reference_resolver_can_return_exception_for_single_position():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    def resolve_product_references(_obj, _info, representations):
        return [
            {"name": "Malbec"} if r["upc"] != 2 else ValueError("Missing product")
            for r in representations
        ]

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = execute_entities(
        schema,
        [
            {"__typename": "Product", "upc": 1},
            {"__typename": "Product", "upc": 2},
            {"__typename": "Product", "upc": 3},
        ],
    )

    assert result.errors
    assert len(result.errors) == 1
    assert result.errors[0].message == "Missing product"
    assert result.errors[0].path == ["_entities", 1]
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": None, "name": "Malbec"},
        None,
        {"__typename": "Product", "upc": None, "name": "Malbec"},
    ]


def test_batch_reference_resolver_error_affects_only_its_type_positions():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    def resolve_product_references(_obj, _info, representations):
        raise RuntimeError("Database is down")

    user = FederatedObjectType("User")
    user.batch_reference_resolver(make_user_batch_resolver([]))

    schema = make_federated_schema(TYPE_DEFS, [product, user])

    result = execute_entities(
        schema,
        [
            {"__typename": "Product", "upc": 1},
            {"__typename": "User", "id": 1},
            {"__typename": "Product", "upc": 2},
        ],
    )

    assert result.errors
    assert len(result.errors) == 2
    assert {tuple(e.path) for e in result.errors} == {
        ("_entities", 0),
        ("_entities", 2),
    }
    assert all(e.message == "Database is down" for e in result.errors)
    assert result.data["_entities"] == [
        None,
        {"__typename": "User", "id": 1, "firstName": "User 1"},
        None,
    ]


@pytest.mark.asyncio
async def test_async_batch_reference_resolver():
    product_calls = []

    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    async def resolve_product_references(_obj, _info, representations):
        await asyncio.sleep(0)
        product_calls.append([r["upc"] for r in representations])
        return [
            {"upc": r["upc"], "name": f"Product {r['upc']}"} for r in representations
        ]

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = await graphql(
        schema,
        ENTITIES_QUERY,
        variable_values={
            "representations": [
                {"__typename": "Product", "upc": 1},
                {"__typename": "Product", "upc": 2},
            ]
        },
    )

    assert result.errors is None
    assert product_calls == [[1, 2]]
    assert result.data["_entities"] == [
        {"__typename": "Product", "upc": 1, "name": "Product 1"},
        {"__typename": "Product", "upc": 2, "name": "Product 2"},
    ]


@pytest.mark.asyncio
async def test_async_batch_reference_resolver_partial_failure():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    async def resolve_product_references(_obj, _info, representations):
        await asyncio.sleep(0)
        return [
            {"name": "Malbec"} if r["upc"] != 2 else ValueError("Missing product")
            for r in representations
        ]

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = await graphql(
        schema,
        ENTITIES_QUERY,
        variable_values={
            "representations": [
                {"__typename": "Product", "upc": 1},
                {"__typename": "Product", "upc": 2},
                {"__typename": "Product", "upc": 3},
            ]
        },
    )

    assert result.errors
    assert len(result.errors) == 1
    assert result.errors[0].path == ["_entities", 1]
    assert result.data["_entities"][0] == {
        "__typename": "Product",
        "upc": None,
        "name": "Malbec",
    }
    assert result.data["_entities"][1] is None
    assert result.data["_entities"][2] == {
        "__typename": "Product",
        "upc": None,
        "name": "Malbec",
    }


@pytest.mark.asyncio
async def test_async_batch_reference_resolver_length_mismatch_is_reported():
    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    async def resolve_product_references(_obj, _info, representations):
        return [{"name": "Malbec"}]

    schema = make_federated_schema(TYPE_DEFS, [product])

    result = await graphql(
        schema,
        ENTITIES_QUERY,
        variable_values={
            "representations": [
                {"__typename": "Product", "upc": 1},
                {"__typename": "Product", "upc": 2},
            ]
        },
    )

    assert result.errors
    assert all(
        'Batch reference resolver for type "Product" returned 1 results '
        "for 2 representations" in error.message
        for error in result.errors
    )


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_batches_or_results():
    batch_calls = []

    product = FederatedObjectType("Product")

    @product.batch_reference_resolver
    async def resolve_product_references(_obj, _info, representations):
        upcs = [r["upc"] for r in representations]
        batch_calls.append(upcs)
        await asyncio.sleep(0.01)
        return [{"upc": upc, "name": f"Product {upc}"} for upc in upcs]

    schema = make_federated_schema(TYPE_DEFS, [product])

    async def query(upcs):
        return await graphql(
            schema,
            ENTITIES_QUERY,
            variable_values={
                "representations": [
                    {"__typename": "Product", "upc": upc} for upc in upcs
                ]
            },
        )

    result_a, result_b = await asyncio.gather(query([1, 2]), query([3, 4, 5]))

    assert result_a.errors is None
    assert result_b.errors is None
    assert sorted(batch_calls) == [[1, 2], [3, 4, 5]]
    assert [e["upc"] for e in result_a.data["_entities"]] == [1, 2]
    assert [e["upc"] for e in result_b.data["_entities"]] == [3, 4, 5]
