from functools import reduce
from operator import add, mul
from typing import Any, cast

from graphql import (
    GraphQLError,
    GraphQLIncludeDirective,
    GraphQLInterfaceType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLSkipDirective,
    GraphQLUnionType,
    get_directive_values,
    get_named_type,
)
from graphql.execution.values import get_argument_values, get_variable_values
from graphql.language import (
    BooleanValueNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    IntValueNode,
    ListValueNode,
    Node,
    OperationDefinitionNode,
    OperationType,
    SelectionNode,
    StringValueNode,
)
from graphql.type import GraphQLFieldMap
from graphql.validation import ValidationContext
from graphql.validation.rules import ASTValidationRule, ValidationRule

cost_directive = """
directive @cost(complexity: Int, multipliers: [String!], 
useMultipliers: Boolean) on FIELD | FIELD_DEFINITION
"""


CostAwareNode = (
    FieldNode
    | FragmentDefinitionNode
    | FragmentSpreadNode
    | InlineFragmentNode
    | OperationDefinitionNode
)


class CostValidator(ValidationRule):
    context: ValidationContext
    maximum_cost: int
    default_cost: int = 0
    default_complexity: int = 1
    variables: dict | None = None
    cost_map: dict[str, dict[str, Any]] | None = None
    operation_aware: bool = False
    operation_name: str | None = None

    def __init__(
        self,
        context: ValidationContext,
        maximum_cost: int,
        *,
        default_cost: int = 0,
        default_complexity: int = 1,
        variables: dict | None = None,
        cost_map: dict[str, dict[str, Any]] | None = None,
        operation_aware: bool = False,
        operation_name: str | None = None,
    ) -> None:
        super().__init__(context)

        self.maximum_cost = maximum_cost
        self.variables = variables
        self.cost_map = cost_map
        self.operation_aware = operation_aware
        self.operation_name = operation_name
        self.default_cost = default_cost
        self.default_complexity = default_complexity
        self.cost = 0
        self.operation_multipliers: list[Any] = []
        self._fragment_cost_coefficients: dict[
            tuple[str, str | None], tuple[int, int]
        ] = {}
        self._operation_aware_fragment_costs: dict[
            tuple[str, str | None, int], int
        ] = {}

    def compute_node_cost(  # noqa C901
        self, node: CostAwareNode, type_def, parent_multipliers=None
    ):
        if parent_multipliers is None:
            parent_multipliers = []
        if isinstance(node, FragmentSpreadNode) or not node.selection_set:
            return 0
        fields: GraphQLFieldMap = {}
        if isinstance(type_def, GraphQLObjectType | GraphQLInterfaceType):
            fields = type_def.fields
        total = 0
        for child_node in node.selection_set.selections:
            self.operation_multipliers = parent_multipliers[:]
            node_cost = self.default_cost
            if isinstance(child_node, FieldNode):
                field = fields.get(child_node.name.value)
                if not field:
                    continue
                field_type = get_named_type(field.type)
                try:
                    field_args: dict[str, Any] = get_argument_values(
                        field, child_node, self.variables
                    )
                except Exception as e:
                    report_error(self.context, e)
                    field_args = {}
                if self.cost_map:
                    cost_map_args = (
                        self.get_args_from_cost_map(
                            child_node, type_def.name, field_args
                        )
                        if type_def and type_def.name
                        else None
                    )
                    if cost_map_args is not None:
                        try:
                            node_cost = self.compute_cost(**cost_map_args)
                        except (TypeError, ValueError) as e:
                            report_error(self.context, e)
                else:
                    cost_is_computed = False
                    if field.ast_node and field.ast_node.directives:
                        directives_args = self.get_args_from_directives(
                            field.ast_node.directives, field_args
                        )
                        if directives_args is not None:
                            try:
                                node_cost = self.compute_cost(**directives_args)
                            except (TypeError, ValueError) as e:
                                report_error(self.context, e)
                            cost_is_computed = True
                    if (
                        field_type
                        and field_type.ast_node
                        and field_type.ast_node.directives
                    ):
                        if not cost_is_computed and isinstance(
                            field_type, GraphQLObjectType
                        ):
                            directives_args = self.get_args_from_directives(
                                field_type.ast_node.directives, field_args
                            )
                            if directives_args is not None:
                                try:
                                    node_cost = self.compute_cost(**directives_args)
                                except (TypeError, ValueError) as e:
                                    report_error(self.context, e)
                child_cost = self.compute_node_cost(
                    child_node, field_type, self.operation_multipliers
                )
                node_cost += child_cost
            if isinstance(child_node, FragmentSpreadNode):
                fragment = self.context.get_fragment(child_node.name.value)
                if fragment:
                    fragment_type = self.context.schema.get_type(
                        fragment.type_condition.name.value
                    )
                    # Read before get_fragment_cost_coefficients(): it
                    # recurses into compute_node_cost, which overwrites
                    # self.operation_multipliers as a side effect.
                    multiplier_product = reduce(mul, self.operation_multipliers, 1)
                    coefficient, offset = self.get_fragment_cost_coefficients(
                        fragment, fragment_type
                    )
                    node_cost = coefficient * multiplier_product + offset
            if isinstance(child_node, InlineFragmentNode):
                inline_fragment_type = type_def
                if child_node.type_condition and child_node.type_condition.name:
                    inline_fragment_type = self.context.schema.get_type(
                        child_node.type_condition.name.value
                    )
                node_cost = self.compute_node_cost(
                    child_node, inline_fragment_type, self.operation_multipliers
                )
            total += node_cost
        return total

    def get_fragment_cost_coefficients(
        self,
        fragment: FragmentDefinitionNode,
        fragment_type: GraphQLNamedType | None,
    ) -> tuple[int, int]:
        """Return (coefficient, offset) that a fragment's cost under an
        ancestor multiplier product P is `coefficient * P + offset`.

        A fragment's cost is linear in P, so these are solved from two
        probe calls to compute_node_cost with different multiplier
        contexts and cached per (fragment, type) rather than per spread.
        Without this, a fragment spread under many distinct multiplier
        contexts (e.g. a compact DAG branching through differently
        multiplied fields) would be re-walked once per context.
        """
        cache_key = (fragment.name.value, getattr(fragment_type, "name", None))
        if cache_key in self._fragment_cost_coefficients:
            return self._fragment_cost_coefficients[cache_key]

        cost_at_one = self.compute_node_cost(fragment, fragment_type, [])
        cost_at_two = self.compute_node_cost(fragment, fragment_type, [2])
        coefficient = cost_at_two - cost_at_one
        offset = cost_at_one - coefficient

        coefficients = (coefficient, offset)
        self._fragment_cost_coefficients[cache_key] = coefficients
        return coefficients

    def enter_operation_definition(self, node, key, parent, path, ancestors):
        if self.operation_aware:
            return

        if self.cost_map:
            try:
                validate_cost_map(self.cost_map, self.context.schema)
            except GraphQLError as cost_map_error:
                self.context.report_error(cost_map_error)
                return

        if node.operation is OperationType.QUERY:
            self.cost += self.compute_node_cost(node, self.context.schema.query_type)
        if node.operation is OperationType.MUTATION:
            self.cost += self.compute_node_cost(node, self.context.schema.mutation_type)
        if node.operation is OperationType.SUBSCRIPTION:
            self.cost += self.compute_node_cost(
                node, self.context.schema.subscription_type
            )

    def leave_operation_definition(self, node, key, parent, path, ancestors):
        if self.operation_aware:
            return

        if self.cost > self.maximum_cost:
            self.context.report_error(self.get_cost_exceeded_error())

    def enter_document(self, node, key, parent, path, ancestors):
        if not self.operation_aware:
            return

        if self.cost_map:
            try:
                validate_cost_map(self.cost_map, self.context.schema)
            except GraphQLError as cost_map_error:
                self.context.report_error(cost_map_error)
                return

        operation = self.get_operation_to_execute(node)
        if operation is None:
            return

        coerced_variables = self.get_coerced_variables(operation)
        if coerced_variables is None:
            return

        schema = self.context.schema
        root_type = None
        if operation.operation is OperationType.QUERY:
            root_type = schema.query_type
        if operation.operation is OperationType.MUTATION:
            root_type = schema.mutation_type
        if operation.operation is OperationType.SUBSCRIPTION:
            root_type = schema.subscription_type

        selections = (
            operation.selection_set.selections if operation.selection_set else ()
        )
        self.cost = self.compute_selection_set_cost(
            selections, root_type, (), frozenset(), coerced_variables
        )
        if self.cost > self.maximum_cost:
            self.context.report_error(self.get_cost_exceeded_error())

    def get_operation_to_execute(
        self, document: DocumentNode
    ) -> OperationDefinitionNode | None:
        operations = [
            definition
            for definition in document.definitions
            if isinstance(definition, OperationDefinitionNode)
        ]
        if self.operation_name is not None:
            for operation in operations:
                if operation.name and operation.name.value == self.operation_name:
                    return operation
            self.context.report_error(
                GraphQLError(f'Unknown operation named "{self.operation_name}".')
            )
            return None
        if len(operations) > 1:
            self.context.report_error(
                GraphQLError(
                    "Must provide operation name "
                    "if query contains multiple operations."
                )
            )
            return None
        if operations:
            return operations[0]
        return None

    def get_coerced_variables(
        self, operation: OperationDefinitionNode
    ) -> dict | None:
        result = get_variable_values(
            self.context.schema,
            operation.variable_definitions or (),
            self.variables or {},
        )
        if isinstance(result, list):
            for error in result:
                self.context.report_error(error)
            return None
        return result

    def compute_selection_set_cost(
        self,
        selections,
        type_def,
        multipliers: tuple[int, ...],
        visited_fragments: frozenset,
        variable_values: dict,
    ) -> int:
        if type_def is None:
            return 0
        if isinstance(type_def, GraphQLInterfaceType | GraphQLUnionType):
            # Abstract types are billed as the most expensive concrete type
            # that may actually run, not as a sum of all possible types.
            return max(
                (
                    self.compute_selection_set_cost(
                        selections,
                        possible_type,
                        multipliers,
                        visited_fragments,
                        variable_values,
                    )
                    for possible_type in self.context.schema.get_possible_types(
                        type_def
                    )
                ),
                default=0,
            )

        fields: GraphQLFieldMap = {}
        if isinstance(type_def, GraphQLObjectType | GraphQLInterfaceType):
            fields = type_def.fields

        field_groups: dict[str, list[FieldNode]] = {}
        fragment_selections: list[SelectionNode] = []
        for selection in selections:
            if not self.should_include_selection(selection, variable_values):
                continue
            if isinstance(selection, FieldNode):
                response_key = (
                    selection.alias.value if selection.alias else selection.name.value
                )
                field_groups.setdefault(response_key, []).append(selection)
            else:
                fragment_selections.append(selection)

        total = 0
        for group in field_groups.values():
            total += self.compute_merged_field_cost(
                group, type_def, fields, multipliers, visited_fragments,
                variable_values,
            )
        for selection in fragment_selections:
            total += self.compute_fragment_selection_cost(
                selection, type_def, multipliers, visited_fragments, variable_values
            )
        return total

    def compute_merged_field_cost(
        self,
        group: list[FieldNode],
        type_def,
        fields: GraphQLFieldMap,
        multipliers: tuple[int, ...],
        visited_fragments: frozenset,
        variable_values: dict,
    ) -> int:
        node = group[0]
        field = fields.get(node.name.value)
        if not field:
            return 0
        field_type = get_named_type(field.type)
        try:
            field_args: dict[str, Any] = get_argument_values(
                field, node, variable_values
            )
        except Exception as e:
            report_error(self.context, e)
            field_args = {}

        cost_args = None
        if self.cost_map:
            cost_args = (
                self.get_args_from_cost_map(node, type_def.name, field_args)
                if type_def and type_def.name
                else None
            )
        else:
            if field.ast_node and field.ast_node.directives:
                cost_args = self.get_args_from_directives(
                    field.ast_node.directives, field_args
                )
            if (
                cost_args is None
                and field_type
                and isinstance(field_type, GraphQLObjectType)
                and field_type.ast_node
                and field_type.ast_node.directives
            ):
                cost_args = self.get_args_from_directives(
                    field_type.ast_node.directives, field_args
                )

        node_cost = self.default_cost
        child_multipliers = multipliers
        if cost_args is not None:
            try:
                node_cost, child_multipliers = self.compute_aware_cost(
                    multipliers, **cost_args
                )
            except (TypeError, ValueError) as e:
                report_error(self.context, e)

        child_selections: list[SelectionNode] = [
            child_selection
            for child_node in group
            if child_node.selection_set
            for child_selection in child_node.selection_set.selections
        ]
        return node_cost + self.compute_selection_set_cost(
            child_selections, field_type, child_multipliers, visited_fragments,
            variable_values,
        )

    def compute_fragment_selection_cost(
        self,
        node: SelectionNode,
        type_def,
        multipliers: tuple[int, ...],
        visited_fragments: frozenset,
        variable_values: dict,
    ) -> int:
        if isinstance(node, InlineFragmentNode):
            if node.type_condition and node.type_condition.name:
                fragment_type = self.context.schema.get_type(
                    node.type_condition.name.value
                )
                if not self.fragment_applies_to_type(fragment_type, type_def):
                    return 0
            return self.compute_selection_set_cost(
                node.selection_set.selections,
                type_def,
                multipliers,
                visited_fragments,
                variable_values,
            )

        if isinstance(node, FragmentSpreadNode):
            fragment_name = node.name.value
            fragment = self.context.get_fragment(fragment_name)
            if fragment is None:
                return 0
            fragment_type = self.context.schema.get_type(
                fragment.type_condition.name.value
            )
            if not self.fragment_applies_to_type(fragment_type, type_def):
                return 0
            if fragment_name in visited_fragments:
                self.context.report_error(
                    GraphQLError(
                        f'Cannot spread fragment "{fragment_name}" within itself.'
                    )
                )
                return 0
            multiplier_product = reduce(mul, multipliers, 1)
            cache_key = (
                fragment_name,
                getattr(type_def, "name", None),
                multiplier_product,
            )
            if cache_key not in self._operation_aware_fragment_costs:
                self._operation_aware_fragment_costs[cache_key] = (
                    self.compute_selection_set_cost(
                        fragment.selection_set.selections,
                        type_def,
                        multipliers,
                        visited_fragments | {fragment_name},
                        variable_values,
                    )
                )
            return self._operation_aware_fragment_costs[cache_key]

        return 0

    def fragment_applies_to_type(self, fragment_type, type_def) -> bool:
        if fragment_type is None or type_def is None:
            return False
        if isinstance(fragment_type, GraphQLObjectType):
            return fragment_type.name == getattr(type_def, "name", None)
        if isinstance(fragment_type, GraphQLInterfaceType | GraphQLUnionType):
            return any(
                possible_type.name == getattr(type_def, "name", None)
                for possible_type in self.context.schema.get_possible_types(
                    fragment_type
                )
            )
        return False

    def should_include_selection(self, node, variable_values: dict) -> bool:
        try:
            skip = get_directive_values(GraphQLSkipDirective, node, variable_values)
            if skip and skip.get("if") is True:
                return False
            include = get_directive_values(
                GraphQLIncludeDirective, node, variable_values
            )
            if include and include.get("if") is False:
                return False
        except Exception as e:
            report_error(self.context, e)
        return True

    def compute_aware_cost(
        self,
        parent_multipliers: tuple[int, ...],
        multipliers=None,
        use_multipliers=True,
        complexity=None,
    ) -> tuple[int, tuple[int, ...]]:
        if complexity is None:
            complexity = self.default_complexity
        if use_multipliers:
            child_multipliers = parent_multipliers
            if multipliers:
                child_multipliers = parent_multipliers + (
                    reduce(add, multipliers, 0),
                )
            return reduce(mul, child_multipliers, complexity), child_multipliers
        return complexity, parent_multipliers

    def compute_cost(self, multipliers=None, use_multipliers=True, complexity=None):
        if complexity is None:
            complexity = self.default_complexity
        if use_multipliers:
            if multipliers:
                multiplier = reduce(add, multipliers, 0)
                self.operation_multipliers = self.operation_multipliers + [multiplier]
            return reduce(mul, self.operation_multipliers, complexity)
        return complexity

    def get_args_from_cost_map(
        self, node: FieldNode, parent_type: str, field_args: dict
    ):
        cost_args = None
        cost_map = cast(dict[Any, dict], self.cost_map)
        if parent_type in cost_map:
            cost_args = cost_map[parent_type].get(node.name.value)
        if not cost_args:
            return None
        cost_args = cost_args.copy()
        if "multipliers" in cost_args:
            cost_args["multipliers"] = self.get_multipliers_from_string(
                cost_args["multipliers"], field_args
            )
        return cost_args

    def get_args_from_directives(self, directives, field_args):
        cost_directive = next(
            (directive for directive in directives if directive.name.value == "cost"),
            None,
        )
        if cost_directive and cost_directive.arguments:
            complexity_arg = next(
                (
                    argument
                    for argument in cost_directive.arguments
                    if argument.name.value == "complexity"
                ),
                None,
            )
            use_multipliers_arg = next(
                (
                    argument
                    for argument in cost_directive.arguments
                    if argument.name.value == "useMultipliers"
                ),
                None,
            )
            multipliers_arg = next(
                (
                    argument
                    for argument in cost_directive.arguments
                    if argument.name.value == "multipliers"
                ),
                None,
            )
            use_multipliers = (
                use_multipliers_arg.value.value
                if use_multipliers_arg
                and use_multipliers_arg.value
                and isinstance(use_multipliers_arg.value, BooleanValueNode)
                else True
            )
            multipliers = (
                self.get_multipliers_from_list_node(
                    cast(list[Node], multipliers_arg.value.values), field_args
                )
                if multipliers_arg
                and multipliers_arg.value
                and isinstance(multipliers_arg.value, ListValueNode)
                else []
            )
            complexity = (
                int(complexity_arg.value.value)
                if complexity_arg
                and complexity_arg.value
                and isinstance(complexity_arg.value, IntValueNode)
                else None
            )
            return {
                "complexity": complexity,
                "multipliers": multipliers,
                "use_multipliers": use_multipliers,
            }

        return None

    def get_multipliers_from_list_node(self, multipliers: list[Node], field_args):
        str_multipliers = [
            node.value for node in multipliers if isinstance(node, StringValueNode)
        ]
        return self.get_multipliers_from_string(str_multipliers, field_args)

    def get_multipliers_from_string(
        self, multipliers: list[str], field_args: dict
    ) -> list[int]:
        accessors = [s.split(".") for s in multipliers]
        parsed_vals: list[int] = []
        for accessor in accessors:
            val: Any = field_args
            for key in accessor:
                if isinstance(val, dict):
                    val = val.get(key)
                else:
                    val = None
                    break
            try:
                if val is not None and not isinstance(val, dict):
                    parsed_vals.append(int(val))
            except (ValueError, TypeError):
                pass
        parsed_vals = [
            len(multiplier) if isinstance(multiplier, list | tuple) else multiplier
            for multiplier in parsed_vals
        ]
        return [m for m in parsed_vals if m > 0]

    def get_cost_exceeded_error(self) -> GraphQLError:
        return GraphQLError(
            cost_analysis_message(self.maximum_cost, self.cost),
            extensions={
                "cost": {
                    "requestedQueryCost": self.cost,
                    "maximumAvailable": self.maximum_cost,
                }
            },
        )


def validate_cost_map(cost_map: dict[str, dict[str, Any]], schema: GraphQLSchema):
    for type_name, type_fields in cost_map.items():
        if type_name not in schema.type_map:
            raise GraphQLError(
                "The query cost could not be calculated because cost map specifies "
                f"a type {type_name} that is not defined by the schema."
            )

        if not isinstance(schema.type_map[type_name], GraphQLObjectType):
            raise GraphQLError(
                "The query cost could not be calculated because cost map specifies "
                f"a type {type_name} that is defined by the schema, "
                "but is not an object type."
            )

        for field_name in type_fields:
            graphql_type = cast(GraphQLObjectType, schema.type_map[type_name])
            if field_name not in graphql_type.fields:
                raise GraphQLError(
                    "The query cost could not be calculated because cost map contains "
                    f"a field {field_name} not defined by the {type_name} type."
                )


def report_error(context: ValidationContext, error: Exception):
    context.report_error(GraphQLError(str(error), original_error=error))


def cost_analysis_message(maximum_cost: int, cost: int) -> str:
    return (
        f"The query exceeds the maximum cost of {maximum_cost}. Actual cost is {cost}"
    )


def cost_validator(
    maximum_cost: int,
    *,
    default_cost: int = 0,
    default_complexity: int = 1,
    variables: dict | None = None,
    cost_map: dict[str, dict[str, Any]] | None = None,
    operation_aware: bool = False,
    operation_name: str | None = None,
) -> type[ASTValidationRule]:
    class _CostValidator(CostValidator):
        def __init__(self, context: ValidationContext) -> None:
            super().__init__(
                context,
                maximum_cost=maximum_cost,
                default_cost=default_cost,
                default_complexity=default_complexity,
                variables=variables,
                cost_map=cost_map,
                operation_aware=operation_aware,
                operation_name=operation_name,
            )

    return cast(type[ASTValidationRule], _CostValidator)
