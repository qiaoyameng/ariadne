import pytest
from graphql import parse, validate
from werkzeug.test import Client
from werkzeug.wrappers import Response

from ariadne import QueryType, graphql, graphql_sync, make_executable_schema
from ariadne.validation import cost_directive, cost_validator
from ariadne.wsgi import GraphQL

type_defs = """
    interface Character {
        id: ID! @cost(complexity: 1)
        name: String! @cost(complexity: 1)
    }

    type Human implements Character {
        id: ID!
        name: String!
        homePlanet: String @cost(complexity: 2)
    }

    type Droid implements Character {
        id: ID!
        name: String!
        primaryFunction: String @cost(complexity: 5)
    }

    union SearchResult = Human | Droid

    input FilterInput {
        limit: Int = 2
    }

    type Query {
        constant: Int! @cost(complexity: 3)
        items(first: Int = 4): [String!]!
            @cost(complexity: 1, multipliers: ["first"])
        filtered(filter: FilterInput): [String!]!
            @cost(complexity: 1, multipliers: ["filter.limit"])
        hero: Character
        search: SearchResult
    }
"""


@pytest.fixture
def schema():
    return make_executable_schema([type_defs, cost_directive])


def get_cost_errors(schema, query, maximum_cost, **kwargs):
    kwargs.setdefault("operation_aware", True)
    rule = cost_validator(maximum_cost=maximum_cost, **kwargs)
    return validate(schema, parse(query), [rule])


def get_requested_cost(errors):
    assert len(errors) == 1
    return errors[0].extensions["cost"]["requestedQueryCost"]


def test_only_selected_operation_is_charged(schema):
    query = """
        query Cheap { constant }
        query Pricey { constant extra: constant }
    """
    assert get_cost_errors(schema, query, 5, operation_name="Cheap") == []

    errors = get_cost_errors(schema, query, 5, operation_name="Pricey")
    assert get_requested_cost(errors) == 6


def test_legacy_mode_still_sums_all_operations(schema):
    query = """
        query Cheap { constant }
        query Pricey { constant extra: constant }
    """
    errors = get_cost_errors(
        schema, query, 8, operation_name="Cheap", operation_aware=False
    )
    assert get_requested_cost(errors) == 9


def test_multiple_operations_without_operation_name_are_an_error(schema):
    query = """
        query Cheap { constant }
        query Pricey { constant }
    """
    errors = get_cost_errors(schema, query, 100)
    assert len(errors) == 1
    assert "Must provide operation name" in errors[0].message


def test_unknown_operation_name_is_an_error(schema):
    errors = get_cost_errors(
        schema, "query Cheap { constant }", 100, operation_name="Missing"
    )
    assert len(errors) == 1
    assert errors[0].message == 'Unknown operation named "Missing".'


def test_single_anonymous_operation_needs_no_operation_name(schema):
    assert get_cost_errors(schema, "{ constant }", 3) == []


def test_mergeable_fields_with_same_response_key_are_charged_once(schema):
    errors = get_cost_errors(schema, "{ constant constant }", 2)
    assert get_requested_cost(errors) == 3


def test_fields_merged_through_fragments_are_charged_once(schema):
    query = """
        { constant ...ConstantBits }
        fragment ConstantBits on Query { constant }
    """
    errors = get_cost_errors(schema, query, 2)
    assert get_requested_cost(errors) == 3


def test_repeated_fragment_spread_is_charged_once(schema):
    query = """
        { ...ConstantBits ...ConstantBits }
        fragment ConstantBits on Query { constant }
    """
    errors = get_cost_errors(schema, query, 2)
    assert get_requested_cost(errors) == 3


def test_different_aliases_are_charged_separately(schema):
    errors = get_cost_errors(schema, "{ first: constant second: constant }", 5)
    assert get_requested_cost(errors) == 6


def test_nested_fragments_are_charged(schema):
    query = """
        { ...HeroBits }
        fragment HeroBits on Query { hero { ...NameBits } }
        fragment NameBits on Character { name }
    """
    errors = get_cost_errors(schema, query, 0)
    assert get_requested_cost(errors) == 1


def test_operation_variable_default_is_used_for_multipliers(schema):
    errors = get_cost_errors(
        schema, "query ($first: Int = 5) { items(first: $first) }", 4
    )
    assert get_requested_cost(errors) == 5


def test_provided_variable_overrides_operation_default(schema):
    errors = get_cost_errors(
        schema,
        "query ($first: Int = 5) { items(first: $first) }",
        1,
        variables={"first": 2},
    )
    assert get_requested_cost(errors) == 2


def test_argument_default_is_used_for_multipliers(schema):
    errors = get_cost_errors(schema, "{ items }", 3)
    assert get_requested_cost(errors) == 4


def test_input_object_default_is_used_for_multipliers(schema):
    errors = get_cost_errors(
        schema, "query ($filter: FilterInput = {}) { filtered(filter: $filter) }", 1
    )
    assert get_requested_cost(errors) == 2


def test_coerced_input_object_default_is_used_for_multipliers(schema):
    errors = get_cost_errors(
        schema,
        "query ($filter: FilterInput!) { filtered(filter: $filter) }",
        1,
        variables={"filter": {}},
    )
    assert get_requested_cost(errors) == 2


def test_missing_required_variable_is_an_error(schema):
    errors = get_cost_errors(schema, "query ($first: Int!) { items(first: $first) }", 5)
    assert len(errors) == 1
    assert errors[0].message == (
        "Variable '$first' of required type 'Int!' was not provided."
    )


def test_skip_directive_removes_field_from_cost(schema):
    assert get_cost_errors(schema, "{ constant @skip(if: true) }", 0) == []


def test_include_directive_removes_field_from_cost(schema):
    assert get_cost_errors(schema, "{ constant @include(if: false) }", 0) == []


def test_skip_and_include_use_coerced_variables(schema):
    query = "query ($flag: Boolean!) { constant @include(if: $flag) }"
    assert get_cost_errors(schema, query, 0, variables={"flag": False}) == []

    errors = get_cost_errors(schema, query, 2, variables={"flag": True})
    assert get_requested_cost(errors) == 3


def test_skip_directive_on_fragment_spread(schema):
    query = """
        query ($skip: Boolean!) { ...ConstantBits @skip(if: $skip) }
        fragment ConstantBits on Query { constant }
    """
    assert get_cost_errors(schema, query, 0, variables={"skip": True}) == []

    errors = get_cost_errors(schema, query, 2, variables={"skip": False})
    assert get_requested_cost(errors) == 3


def test_interface_cost_is_max_of_possible_types_not_sum(schema):
    query = """
        {
            hero {
                name
                ... on Human { homePlanet }
                ... on Droid { primaryFunction }
            }
        }
    """
    # Human: name (1) + homePlanet (2) = 3
    # Droid: name (1) + primaryFunction (5) = 6
    # max is 6, sum of all branches would be 8
    errors = get_cost_errors(schema, query, 5)
    assert get_requested_cost(errors) == 6


def test_interface_common_fields_are_not_missed(schema):
    errors = get_cost_errors(schema, "{ hero { id name } }", 1)
    assert get_requested_cost(errors) == 2


def test_union_cost_is_max_of_possible_types(schema):
    query = """
        {
            search {
                ... on Human { homePlanet }
                ... on Droid { primaryFunction }
            }
        }
    """
    errors = get_cost_errors(schema, query, 4)
    assert get_requested_cost(errors) == 5


def test_fragment_cycle_is_reported_as_graphql_error(schema):
    query = """
        { ...A }
        fragment A on Query { ...B }
        fragment B on Query { ...A }
    """
    errors = get_cost_errors(schema, query, 100)
    assert len(errors) == 1
    assert "within itself" in errors[0].message


def test_fragment_dag_is_not_reexpanded_exponentially(schema):
    depth = 30
    dag_type_defs = (
        "type Query {\n"
        + "\n".join(f"    f{i}: Int @cost(complexity: 1)" for i in range(depth + 1))
        + "\n}"
    )
    dag_schema = make_executable_schema([dag_type_defs, cost_directive])
    fragments = [
        f"fragment F{depth} on Query {{ f{depth} }}",
    ]
    for i in range(depth - 1, -1, -1):
        fragments.append(
            f"fragment F{i} on Query {{ f{i} ...F{i + 1} ...F{i + 1} }}"
        )
    query = "{ ...F0 }\n" + "\n".join(fragments)

    # Naive expansion would visit 2**30 field occurrences; memoized
    # collection bills each of the 31 distinct fields exactly once.
    errors = get_cost_errors(dag_schema, query, depth)
    assert get_requested_cost(errors) == depth + 1


def test_cost_map_is_used_in_operation_aware_mode():
    plain_schema = make_executable_schema("type Query { constant: Int! }")
    errors = get_cost_errors(
        plain_schema,
        "{ constant }",
        2,
        cost_map={"Query": {"constant": {"complexity": 3}}},
    )
    assert get_requested_cost(errors) == 3


@pytest.fixture
def counting_schema():
    calls = []
    query_type = QueryType()

    @query_type.field("constant")
    def resolve_constant(*_):
        calls.append("constant")
        return 42

    @query_type.field("items")
    def resolve_items(*_, first):
        calls.append("items")
        return ["x"] * first

    schema = make_executable_schema([type_defs, cost_directive], query_type)
    return schema, calls


def make_operation_aware_rules(maximum_cost):
    def get_validation_rules(context_value, document, data):
        return [
            cost_validator(
                maximum_cost=maximum_cost,
                variables=data.get("variables"),
                operation_name=data.get("operationName"),
                operation_aware=True,
            )
        ]

    return get_validation_rules


def test_graphql_sync_rejects_before_executing_resolvers(counting_schema):
    schema, calls = counting_schema
    success, result = graphql_sync(
        schema,
        {"query": "{ constant }"},
        validation_rules=make_operation_aware_rules(2),
    )
    assert not success
    assert calls == []
    assert result["errors"][0]["extensions"]["cost"] == {
        "requestedQueryCost": 3,
        "maximumAvailable": 2,
    }


def test_graphql_sync_executes_query_within_cost(counting_schema):
    schema, calls = counting_schema
    success, result = graphql_sync(
        schema,
        {"query": "{ constant }"},
        validation_rules=make_operation_aware_rules(3),
    )
    assert success
    assert result == {"data": {"constant": 42}}
    assert calls == ["constant"]


def test_graphql_sync_selects_operation_by_operation_name(counting_schema):
    schema, calls = counting_schema
    query = """
        query Cheap { constant }
        query Pricey { constant extra: constant }
    """
    success, _ = graphql_sync(
        schema,
        {"query": query, "operationName": "Cheap"},
        validation_rules=make_operation_aware_rules(5),
    )
    assert success
    assert calls == ["constant"]

    calls.clear()
    success, result = graphql_sync(
        schema,
        {"query": query, "operationName": "Pricey"},
        validation_rules=make_operation_aware_rules(5),
    )
    assert not success
    assert calls == []
    assert result["errors"][0]["extensions"]["cost"]["requestedQueryCost"] == 6


def test_graphql_sync_missing_required_variable_blocks_execution(counting_schema):
    schema, calls = counting_schema
    success, result = graphql_sync(
        schema,
        {"query": "query ($first: Int!) { items(first: $first) }"},
        validation_rules=make_operation_aware_rules(100),
    )
    assert not success
    assert calls == []
    assert "was not provided" in result["errors"][0]["message"]


def test_graphql_sync_fragment_cycle_blocks_execution(counting_schema):
    schema, calls = counting_schema
    query = """
        { constant ...A }
        fragment A on Query { ...B }
        fragment B on Query { ...A }
    """
    success, _ = graphql_sync(
        schema,
        {"query": query},
        validation_rules=make_operation_aware_rules(100),
    )
    assert not success
    assert calls == []


@pytest.mark.asyncio
async def test_graphql_rejects_before_executing_resolvers(counting_schema):
    schema, calls = counting_schema
    success, result = await graphql(
        schema,
        {"query": "{ constant }"},
        validation_rules=make_operation_aware_rules(2),
    )
    assert not success
    assert calls == []
    assert result["errors"][0]["extensions"]["cost"]["requestedQueryCost"] == 3


@pytest.mark.asyncio
async def test_graphql_executes_query_within_cost(counting_schema):
    schema, calls = counting_schema
    success, result = await graphql(
        schema,
        {"query": "{ constant }"},
        validation_rules=make_operation_aware_rules(3),
    )
    assert success
    assert result == {"data": {"constant": 42}}
    assert calls == ["constant"]


class TestClient(Client):
    __test__ = False

    def __init__(self, app):
        super().__init__(app, Response)


def test_http_handler_rejects_before_executing_resolvers(counting_schema):
    schema, calls = counting_schema
    app = GraphQL(schema, validation_rules=make_operation_aware_rules(2))
    client = TestClient(app)

    response = client.post("/", json={"query": "{ constant }"})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["errors"][0]["extensions"]["cost"] == {
        "requestedQueryCost": 3,
        "maximumAvailable": 2,
    }
    assert calls == []


def test_http_handler_executes_query_within_cost(counting_schema):
    schema, calls = counting_schema
    app = GraphQL(schema, validation_rules=make_operation_aware_rules(3))
    client = TestClient(app)

    response = client.post("/", json={"query": "{ constant }"})

    assert response.status_code == 200
    assert response.get_json() == {"data": {"constant": 42}}
    assert calls == ["constant"]


def test_http_handler_does_not_cache_costs_between_requests(counting_schema):
    schema, calls = counting_schema
    app = GraphQL(schema, validation_rules=make_operation_aware_rules(4))
    client = TestClient(app)
    query = "query ($first: Int!) { items(first: $first) }"

    cheap_response = client.post("/", json={"query": query, "variables": {"first": 2}})
    assert cheap_response.get_json() == {"data": {"items": ["x", "x"]}}

    calls.clear()
    pricey_response = client.post(
        "/", json={"query": query, "variables": {"first": 10}}
    )
    payload = pricey_response.get_json()
    assert payload["errors"][0]["extensions"]["cost"]["requestedQueryCost"] == 10
    assert calls == []
