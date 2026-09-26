import json

import pytest
from graphql import parse
from graphql.validation import validate
from werkzeug.test import Client

from ariadne import QueryType, graphql, graphql_sync, make_executable_schema
from ariadne.validation import cost_directive, cost_validator
from ariadne.wsgi import GraphQL as WSGIGraphQL

type_defs = """
    interface Character {
        name: String!
    }

    type Human implements Character {
        name: String! @cost(complexity: 2)
        height: Float @cost(complexity: 3)
    }

    type Droid implements Character {
        name: String! @cost(complexity: 2)
        primaryFunction: String @cost(complexity: 5)
    }

    union SearchResult = Human | Droid

    input ItemsInput {
        limit: Int = 4
    }

    type Item {
        name: String! @cost(complexity: 1)
    }

    type Query {
        fieldA: Int @cost(complexity: 2)
        fieldB: Int @cost(complexity: 3)
        expensive: Int @cost(complexity: 100)
        hero: Character
        search: SearchResult @cost(complexity: 2)
        characters(first: Int): [Character!]!
            @cost(complexity: 1, multipliers: ["first"])
        items(filter: ItemsInput): [Item!]!
            @cost(complexity: 1, multipliers: ["filter.limit"])
    }
"""

cost_map = {
    "Query": {
        "fieldA": {"complexity": 2},
        "fieldB": {"complexity": 3},
        "expensive": {"complexity": 100},
    },
}


@pytest.fixture
def schema():
    return make_executable_schema([type_defs, cost_directive])


def validate_operation_aware(
    schema, query, maximum_cost=1000, variables=None, operation_name=None, **kwargs
):
    rule = cost_validator(
        maximum_cost=maximum_cost,
        variables=variables,
        operation_aware=True,
        operation_name=operation_name,
        **kwargs,
    )
    return validate(schema, parse(query), [rule])


def get_cost_error(errors):
    return next(
        error for error in errors if error.extensions and "cost" in error.extensions
    )


def test_only_selected_operation_is_billed(schema):
    query = "query A { fieldA } query B { fieldB }"
    errors = validate_operation_aware(schema, query, maximum_cost=2, operation_name="B")
    assert get_cost_error(errors).extensions["cost"] == {
        "requestedQueryCost": 3,
        "maximumAvailable": 2,
    }


def test_selected_operation_within_limit_passes(schema):
    query = "query A { fieldA } query B { expensive }"
    errors = validate_operation_aware(schema, query, maximum_cost=2, operation_name="A")
    assert errors == []


def test_multiple_operations_without_operation_name_return_error(schema):
    query = "query A { fieldA } query B { fieldB }"
    errors = validate_operation_aware(schema, query)
    assert len(errors) == 1
    assert "Must provide operation name" in errors[0].message


def test_unknown_operation_name_returns_error(schema):
    errors = validate_operation_aware(
        schema, "query A { fieldA }", operation_name="Missing"
    )
    assert len(errors) == 1
    assert errors[0].message == 'Unknown operation named "Missing".'


def test_same_response_key_fields_are_billed_once(schema):
    errors = validate_operation_aware(schema, "{ fieldA fieldA }", maximum_cost=2)
    assert errors == []


def test_different_aliases_are_billed_separately(schema):
    errors = validate_operation_aware(
        schema, "{ first: fieldA second: fieldA }", maximum_cost=3
    )
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 4


def test_merged_field_subselections_are_billed_once(schema):
    query = "{ hero { name } hero { name } }"
    errors = validate_operation_aware(schema, query, maximum_cost=2)
    assert errors == []


def test_aliased_field_subselections_are_billed_separately(schema):
    query = "{ first: hero { name } second: hero { name } }"
    errors = validate_operation_aware(schema, query, maximum_cost=3)
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 4


def test_nested_fragments_are_billed(schema):
    query = """
        { ...LevelOne }
        fragment LevelOne on Query { fieldA ...LevelTwo }
        fragment LevelTwo on Query { fieldB }
    """
    errors = validate_operation_aware(schema, query, maximum_cost=4)
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 5


def test_fragment_spread_twice_is_billed_twice(schema):
    query = "{ ...Fields ...Fields } fragment Fields on Query { fieldA }"
    errors = validate_operation_aware(schema, query, maximum_cost=3)
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 4


def test_operation_variable_default_is_used_for_multipliers(schema):
    query = "query ($first: Int = 3) { characters(first: $first) { name } }"
    errors = validate_operation_aware(schema, query, maximum_cost=8)
    # characters: 1 * 3 = 3, name: 2 * 3 = 6, total 9
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 9


def test_input_object_default_is_used_for_multipliers(schema):
    query = "query ($filter: ItemsInput = {}) { items(filter: $filter) { name } }"
    errors = validate_operation_aware(schema, query, maximum_cost=7)
    # items: 1 * 4 (input default) = 4, name: 1 * 4 = 4, total 8
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 8


def test_missing_required_variable_returns_error(schema):
    query = "query ($first: Int!) { characters(first: $first) { name } }"
    errors = validate_operation_aware(schema, query)
    assert len(errors) == 1
    assert errors[0].message == (
        "Variable '$first' of required type 'Int!' was not provided."
    )


def test_skip_directive_removes_field_from_cost(schema):
    errors = validate_operation_aware(
        schema, "{ expensive @skip(if: true) }", maximum_cost=0
    )
    assert errors == []


def test_include_directive_removes_field_from_cost(schema):
    errors = validate_operation_aware(
        schema, "{ expensive @include(if: false) }", maximum_cost=0
    )
    assert errors == []


def test_skip_directive_with_variable(schema):
    query = "query ($skip: Boolean!) { expensive @skip(if: $skip) }"
    errors = validate_operation_aware(
        schema, query, maximum_cost=0, variables={"skip": True}
    )
    assert errors == []
    errors = validate_operation_aware(
        schema, query, maximum_cost=0, variables={"skip": False}
    )
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 100


def test_skip_directive_on_fragment_spread(schema):
    query = "{ ...Fields @skip(if: true) } fragment Fields on Query { expensive }"
    errors = validate_operation_aware(schema, query, maximum_cost=0)
    assert errors == []


def test_interface_is_billed_as_most_expensive_concrete_type(schema):
    query = """
        { hero { name ... on Human { height } ... on Droid { primaryFunction } } }
    """
    errors = validate_operation_aware(schema, query, maximum_cost=6)
    # Human branch: 2 + 3 = 5, Droid branch: 2 + 5 = 7, max is 7 (not 12)
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 7


def test_interface_common_fields_are_not_skipped(schema):
    errors = validate_operation_aware(schema, "{ hero { name } }", maximum_cost=1)
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 2


def test_union_is_billed_as_most_expensive_concrete_type(schema):
    query = """
        { search { ... on Human { name height } ... on Droid { primaryFunction } } }
    """
    errors = validate_operation_aware(schema, query, maximum_cost=6)
    # search: 2, Human branch: 2 + 3 = 5, Droid branch: 5, total 2 + 5 = 7
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 7


def test_cyclic_fragments_return_error(schema):
    query = """
        { ...A }
        fragment A on Query { ...B }
        fragment B on Query { ...A }
    """
    errors = validate_operation_aware(schema, query)
    assert any("Cannot spread fragment" in error.message for error in errors)


def test_fragment_dag_is_evaluated_in_linear_time(schema):
    depth = 40
    fragments = ["fragment F0 on Query { fieldA }"]
    for i in range(1, depth):
        fragments.append(
            f"fragment F{i} on Query {{ ...F{i - 1} ...F{i - 1} }}"
        )
    query = "{ ...F%d } " % (depth - 1) + " ".join(fragments)
    expected_cost = 2**depth  # fieldA complexity 2, doubled depth times
    errors = validate_operation_aware(
        schema, query, maximum_cost=expected_cost - 1
    )
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == (
        expected_cost
    )


def test_cost_map_is_used_in_operation_aware_mode(schema):
    errors = validate_operation_aware(
        schema, "{ fieldA }", maximum_cost=1, cost_map=cost_map
    )
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 2


def test_non_aware_mode_still_bills_whole_document(schema):
    query = "query A { fieldA } query B { fieldB }"
    rule = cost_validator(maximum_cost=4, operation_name="B")
    errors = validate(schema, parse(query), [rule])
    # legacy mode ignores operation_name and bills both operations
    assert get_cost_error(errors).extensions["cost"]["requestedQueryCost"] == 5


resolver_type = QueryType()
resolver_calls = []


@resolver_type.field("fieldA")
def resolve_field_a(*_):
    resolver_calls.append("fieldA")
    return 1


@resolver_type.field("fieldB")
def resolve_field_b(*_):
    resolver_calls.append("fieldB")
    return 2


@resolver_type.field("items")
def resolve_items(*_, filter=None):
    limit = (filter or {}).get("limit") or 1
    return [{"name": "item"}] * limit


@pytest.fixture
def executable_schema():
    resolver_calls.clear()
    return make_executable_schema([type_defs, cost_directive], resolver_type)


def operation_aware_rules(maximum_cost):
    def rules_factory(context_value, document, data):
        return [
            cost_validator(
                maximum_cost=maximum_cost,
                variables=data.get("variables"),
                operation_name=data.get("operationName"),
                operation_aware=True,
            )
        ]

    return rules_factory


def test_graphql_sync_rejects_costly_query_before_execution(executable_schema):
    success, result = graphql_sync(
        executable_schema,
        {"query": "query A { fieldA } query B { fieldB }", "operationName": "B"},
        validation_rules=operation_aware_rules(maximum_cost=2),
    )
    assert not success
    assert resolver_calls == []
    assert result["errors"][0]["extensions"]["cost"] == {
        "requestedQueryCost": 3,
        "maximumAvailable": 2,
    }


def test_graphql_sync_executes_query_within_cost_limit(executable_schema):
    success, result = graphql_sync(
        executable_schema,
        {"query": "query A { fieldA } query B { fieldB }", "operationName": "A"},
        validation_rules=operation_aware_rules(maximum_cost=2),
    )
    assert success
    assert resolver_calls == ["fieldA"]
    assert result["data"] == {"fieldA": 1}


def test_graphql_sync_rejects_query_with_unknown_operation(executable_schema):
    success, result = graphql_sync(
        executable_schema,
        {"query": "query A { fieldA }", "operationName": "Missing"},
        validation_rules=operation_aware_rules(maximum_cost=100),
    )
    assert not success
    assert resolver_calls == []
    assert result["errors"][0]["message"] == 'Unknown operation named "Missing".'


def test_graphql_sync_rejects_query_with_missing_variable(executable_schema):
    success, result = graphql_sync(
        executable_schema,
        {"query": "query ($first: Int!) { characters(first: $first) { name } }"},
        validation_rules=operation_aware_rules(maximum_cost=100),
    )
    assert not success
    assert resolver_calls == []


def test_graphql_sync_does_not_cache_costs_between_requests(executable_schema):
    rules = operation_aware_rules(maximum_cost=8)
    query = "query ($filter: ItemsInput = {}) { items(filter: $filter) { name } }"

    success, _ = graphql_sync(
        executable_schema,
        {"query": query, "variables": {"filter": {"limit": 1}}},
        validation_rules=rules,
    )
    assert success

    success, result = graphql_sync(
        executable_schema,
        {"query": query, "variables": {"filter": {"limit": 10}}},
        validation_rules=rules,
    )
    assert not success
    assert result["errors"][0]["extensions"]["cost"]["requestedQueryCost"] == 20


@pytest.mark.asyncio
async def test_graphql_rejects_costly_query_before_execution(executable_schema):
    success, result = await graphql(
        executable_schema,
        {"query": "{ first: fieldA second: fieldA }"},
        validation_rules=operation_aware_rules(maximum_cost=3),
    )
    assert not success
    assert resolver_calls == []
    assert result["errors"][0]["extensions"]["cost"]["requestedQueryCost"] == 4


@pytest.mark.asyncio
async def test_graphql_executes_query_within_cost_limit(executable_schema):
    success, result = await graphql(
        executable_schema,
        {"query": "{ fieldA }"},
        validation_rules=operation_aware_rules(maximum_cost=2),
    )
    assert success
    assert resolver_calls == ["fieldA"]
    assert result["data"] == {"fieldA": 1}


def test_wsgi_handler_rejects_costly_query_before_execution(executable_schema):
    app = WSGIGraphQL(
        executable_schema, validation_rules=operation_aware_rules(maximum_cost=2)
    )
    client = Client(app)
    response = client.post(
        "/graphql/",
        data=json.dumps(
            {
                "query": "query A { fieldA } query B { fieldB }",
                "operationName": "B",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert resolver_calls == []
    result = json.loads(response.get_data(as_text=True))
    assert result["errors"][0]["extensions"]["cost"] == {
        "requestedQueryCost": 3,
        "maximumAvailable": 2,
    }


def test_wsgi_handler_executes_query_within_cost_limit(executable_schema):
    app = WSGIGraphQL(
        executable_schema, validation_rules=operation_aware_rules(maximum_cost=2)
    )
    client = Client(app)
    response = client.post(
        "/graphql/",
        data=json.dumps(
            {
                "query": "query A { fieldA } query B { fieldB }",
                "operationName": "A",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert resolver_calls == ["fieldA"]
    result = json.loads(response.get_data(as_text=True))
    assert result["data"] == {"fieldA": 1}
