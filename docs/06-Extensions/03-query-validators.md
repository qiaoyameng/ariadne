---
id: query-validators
title: Query validators
---

GraphQL uses query validators to check if Query AST is valid and can be executed. Every GraphQL server implements standard query validators. For example, there is an validator that tests if queried field exists on queried type, that makes query fail with "Cannot query field on type" error if it doesn't.

Ariadne supports extending this server behaviour by including custom query validators.


## Query cost validator

Query cost validation allows server owners to limit maximal allowed cost (or complexity) of GraphQL query. This forces malicious clients to run multiple HTTP requests which in turn allows server owners to limit or filter off their traffic through HTTP server settings.

First, query cost validator needs to be enabled on GraphQL server using `validation_rules` option:

```
from ariadne.asgi import GraphQL
from ariadne.validation import cost_validator

schema = make_executable_schema() # make_executable_schema call with type definitions, resolvers, etc. ect.

graphql = Graphql(
    schema,
    validation_rules=[cost_validator(maximum_cost=5)]
)
```

Next step is assigning costs values to schema fields. This can be done in Python or in the schema.


### Setting fields costs in schema

To set fields costs in schema, first add `cost` directive definition to it:

```graphql
directive @cost(complexity: Int, multipliers: [String!], useMultipliers: Boolean) on FIELD | FIELD_DEFINITION
```

To make your schema future proof, directive's definition is available as import from Ariadne:

```python
from ariadne.validation import cost_directive

schema = make_executable_schema([type_defs, cost_directive], ...)
```

Now you can use this directive to assign costs to selected fields. For example, you can set cost of accessing `poster` field on `Post` type as `3` like this:

```graphql
type Post {
    id: ID
    name: String
    poster: User @cost(complexity: 3)
}
```

If you want to, you can make cost depend on the value of one or more of `Int` arguments that field accepts:

```graphql
type Query {
    news(promoted: Int, regular: Int): Post @cost(complexity: 1, multipliers: ["promoted", "regular"])
}
```

In the above example, final complexity will be multiplied by both `promoted` and `regular` values.

You can also use `useMultipliers` to remove query cost multiplication for specified field without removing `@cost` from it:

```graphql
type Query {
    news(promoted: Int): Post @cost(complexity: 1, multipliers: ["promoted"], useMultipliers: false)
}
```


### Setting fields costs in Python

Fields costs can be set using Python dict passed as an option to `cost_validator`:

```python
cost_map = {
    "Query": {
        "news": {"complexity": 1, "multipliers": ["promoted", "regular"]},
    },
    "Post": {
        "poster": {"complexity": 3},
    },
}

graphql = Graphql(
    type_defs,
    validation_rules=[cost_validator(maximum_cost=5, cost_map=cost_map)]
)
```


### Setting default field cost and complexity

`cost_validator` supports two additional options to make configuring costs less verbose:

- `default_complexity: int` - Default value for field `complexity` if its omitted from `@cost` or `price_map`. Defaults to `1`.
- `default_cost: int` - Default base value for field cost. Defaults to `0`.


### Configuring `cost_validator` dynamically

Because `validation_rules` option can be a `callable` (eg. function), it can be used to dynamically configure query costs validation based on GraphQL context or even parsed query itself:

```python
def get_validation_rules(context_value, document, data):
    user = context_value.get("user")
    if user:
        if user.is_admin:
            return None
        if user.is_high_ltv:
            return [cost_validator(maximum_cost=15)]
    
    return [cost_validator(maximum_cost=5)]


graphql = Graphql(
    schema,
    validation_rules=get_validation_rules
)
```


### Exposing query variables to `cost_validator`

Cost validator will raise an error if query containing variables is made, but variable values are not made available to the validator. Use dynamic configuration to avoid this:
```python
type_defs = gql(
    """
    type Query {
        hello(id: Int!): String!
    }
    """
)


def get_validation_rules(context_value, document, data):
    return [cost_validator(maximum_cost=5, variables=data.get("variables"))]


schema = make_executable_schema(type_defs)

graphql = Graphql(
    schema,
    validation_rules=get_validation_rules,
)
```

### Complexity of lists of items

Query cost validation runs before query execution. This makes it impossible for field cost to depend on real number of returned children.

To deal with that you should use either multipliers, dynamic query cost configuration or make field's cost high enough in advance.


### Operation-aware cost validation

By default `cost_validator` walks the whole query document and sums costs of all operations it contains. Passing `operation_aware=True` switches it to an execution-aware mode in which only the operation selected by the request's `operationName` is evaluated, and only the fields that would actually be executed are billed:

- Variables are coerced with full GraphQL type coercion before computing costs, so operation variable defaults and input object defaults are taken into account. Missing required variables are reported as regular GraphQL errors and block the execution.
- `@skip` and `@include` directives are honored.
- Mergeable fields sharing a response key are billed once, while different aliases are billed separately.
- For `interface` and `union` fields, the cost is the maximum cost over the possible concrete types (including fields common to all of them) instead of the sum of mutually exclusive branches.
- Fragments may be referenced any number of times without repeated re-expansion; fragment cycles are reported as regular GraphQL errors and block the execution.

If the selected operation doesn't exist (or `operationName` is not given while the document contains multiple operations), a regular GraphQL error is returned and resolvers are not executed.

Because the mode depends on request variables and `operationName`, it should be configured dynamically so no costs are shared between requests:

```python
def get_validation_rules(context_value, document, data):
    return [
        cost_validator(
            maximum_cost=5,
            variables=data.get("variables"),
            operation_name=data.get("operationName"),
            operation_aware=True,
        )
    ]


graphql = Graphql(
    schema,
    validation_rules=get_validation_rules,
)
```


## Implementing custom validator

All custom query validators should extend the [`ValidationRule`](https://github.com/graphql-python/graphql-core/blob/v3.0.5/src/graphql/validation/rules/__init__.py#L37) base class importable from the `graphql.validation.rules` module.

Query validators are visitor classes. They are instantiated at the time of query validation with one required argument (`context: ASTValidationContext`).

In order to perform validation, your validator class should define one or more of `enter_*` and `leave_*` methods. For possible enter/leave items as well as details on function documentation, please see contents of the [`visitor`](https://github.com/graphql-python/graphql-core/blob/v3.0.5/src/graphql/language/visitor.py) module.

To make validation fail, you should call validator's `report_error` method with the instance of `GraphQLError` describing failure reason.

Here is an example query validator that visits field definitions in GraphQL query and fails query validation if any of those fields are introspection fields:

```python
from graphql import GraphQLError
from graphql.language import FieldNode
from graphql.validation import ValidationRule


def is_introspection_field(field_name: str):
    return field_name.lower() in [
        "__schema",
        "__directive",
        "__directivelocation",
        "__type",
        "__field",
        "__inputvalue",
        "__enumvalue",
        "__typekind",
    ]


class IntrospectionDisabledRule(ValidationRule):
    def enter_field(self, node: FieldNode, *_args):
        field_name = node.name.value
        if not is_introspection_key(field_name):
            return

        self.report_error(
            GraphQLError(
                f"Cannot query '{field_name}': introspection is disabled.", node,
            )
        )

```
