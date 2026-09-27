from functools import reduce
from operator import add, mul
from typing import Any, cast

from graphql import (
    GraphQLError,
    GraphQLInterfaceType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
    get_named_type,
)
from graphql.execution.collect_fields import should_include_node
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
    NamedTypeNode,
    Node,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
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
    operation_name: str | None = None
    operation_aware: bool = False

    def __init__(
        self,
        context: ValidationContext,
        maximum_cost: int,
        *,
        default_cost: int = 0,
        default_complexity: int = 1,
        variables: dict | None = None,
        cost_map: dict[str, dict[str, Any]] | None = None,
        operation_name: str | None = None,
        operation_aware: bool = False,
    ) -> None:
        super().__init__(context)

        self.maximum_cost = maximum_cost
        self.variables = variables
        self.cost_map = cost_map
        self.operation_name = operation_name
        self.operation_aware = operation_aware
        self.default_cost = default_cost
        self.default_complexity = default_complexity
        self.cost = 0
        self.operation_multipliers: list[Any] = []
        self._fragment_cost_coefficients: dict[
            tuple[str, str | None], tuple[int, int]
        ] = {}
        self._selected_operation: OperationDefinitionNode | None = None
        self._coerced_variables: dict[str, Any] | None = None
        self._variables_coerced = False
        self._fragment_field_cache: dict[
            tuple[str, str], dict[str, list[FieldNode]]
        ] = {}
        self._reported_fragment_cycles: set[str] = set()

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
            if node is not self._selected_operation:
                return
            root_type = self.get_root_type(node.operation)
            if root_type is not None:
                self.cost += self.compute_operation_cost(node, root_type)
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
        if self.operation_aware and node is not self._selected_operation:
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

        self._selected_operation = self.select_operation(node)
        if self._selected_operation is None:
            self.context.report_error(self.get_operation_not_found_error())

    def select_operation(
        self, document: DocumentNode
    ) -> OperationDefinitionNode | None:
        operations = [
            definition
            for definition in document.definitions
            if isinstance(definition, OperationDefinitionNode)
        ]
        if self.operation_name is None:
            if len(operations) == 1:
                return operations[0]
            return None
        for operation in operations:
            if operation.name and operation.name.value == self.operation_name:
                return operation
        return None

    def get_operation_not_found_error(self) -> GraphQLError:
        if self.operation_name is None:
            return GraphQLError(
                "Must provide operation name if query contains multiple operations."
            )
        return GraphQLError(f'Unknown operation named "{self.operation_name}".')

    def get_root_type(self, operation: OperationType):
        if operation is OperationType.QUERY:
            return self.context.schema.query_type
        if operation is OperationType.MUTATION:
            return self.context.schema.mutation_type
        if operation is OperationType.SUBSCRIPTION:
            return self.context.schema.subscription_type
        return None

    def get_coerced_variables(
        self, operation: OperationDefinitionNode
    ) -> dict[str, Any] | None:
        """Return request variables coerced against the operation's variable
        definitions, applying operation and input object defaults.

        Coercion errors (eg. missing required variables) are reported as
        regular validation errors and abort the cost computation, which in
        turn blocks the query execution.
        """
        if not self._variables_coerced:
            self._variables_coerced = True
            result = get_variable_values(
                self.context.schema,
                operation.variable_definitions or [],
                self.variables or {},
            )
            if isinstance(result, list):
                for error in result:
                    self.context.report_error(
                        error
                        if isinstance(error, GraphQLError)
                        else GraphQLError(str(error))
                    )
            else:
                self._coerced_variables = result
        return self._coerced_variables

    def compute_operation_cost(
        self, operation: OperationDefinitionNode, root_type: GraphQLObjectType
    ) -> int:
        if self.get_coerced_variables(operation) is None:
            return 0
        return self.compute_selection_set_cost([operation.selection_set], root_type, [])

    def compute_selection_set_cost(
        self,
        selection_sets: list[SelectionSetNode],
        type_def: GraphQLNamedType | None,
        parent_multipliers: list[Any],
    ) -> int:
        if isinstance(type_def, GraphQLInterfaceType | GraphQLUnionType):
            # Only one concrete type can be resolved at runtime, so the cost
            # is the maximum cost over all possible types instead of the sum
            # of their mutually exclusive branches.
            possible_types = self.context.schema.get_possible_types(type_def)
            if not possible_types:
                return 0
            return max(
                self.compute_selection_set_cost(
                    selection_sets, possible_type, parent_multipliers
                )
                for possible_type in possible_types
            )
        if not isinstance(type_def, GraphQLObjectType):
            return 0

        grouped_fields = self.collect_execution_fields(selection_sets, type_def)
        total = 0
        for field_nodes in grouped_fields.values():
            field_node = field_nodes[0]
            field = type_def.fields.get(field_node.name.value)
            if not field:
                continue
            self.operation_multipliers = parent_multipliers[:]
            try:
                field_args: dict[str, Any] = get_argument_values(
                    field, field_node, self._coerced_variables
                )
            except Exception as e:
                report_error(self.context, e)
                field_args = {}
            node_cost = self.compute_operation_field_cost(
                field, field_node, type_def, field_args
            )
            child_selection_sets = [
                node.selection_set for node in field_nodes if node.selection_set
            ]
            if child_selection_sets:
                node_cost += self.compute_selection_set_cost(
                    child_selection_sets,
                    get_named_type(field.type),
                    self.operation_multipliers,
                )
            total += node_cost
        return total

    def compute_operation_field_cost(
        self,
        field,
        field_node: FieldNode,
        object_type: GraphQLObjectType,
        field_args: dict[str, Any],
    ) -> int:
        cost_args = self.find_cost_args(field, field_node, object_type, field_args)
        if cost_args is None:
            return self.default_cost
        try:
            return self.compute_cost(**cost_args)
        except (TypeError, ValueError) as e:
            report_error(self.context, e)
            return self.default_cost

    def find_cost_args(
        self,
        field,
        field_node: FieldNode,
        object_type: GraphQLObjectType,
        field_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        # Fields selected on an interface are executed against the concrete
        # type, but their cost may be declared on the interface, so the
        # implemented interfaces are used as a fallback.
        interface_types: list[GraphQLInterfaceType] = list(
            getattr(object_type, "interfaces", [])
        )
        if self.cost_map:
            for type_name in [object_type.name] + [
                interface.name for interface in interface_types
            ]:
                cost_args = self.get_args_from_cost_map(
                    field_node, type_name, field_args
                )
                if cost_args is not None:
                    return cost_args
            return None

        candidate_fields = [field]
        for interface in interface_types:
            interface_field = interface.fields.get(field_node.name.value)
            if interface_field is not None:
                candidate_fields.append(interface_field)
        for candidate in candidate_fields:
            if candidate.ast_node and candidate.ast_node.directives:
                cost_args = self.get_args_from_directives(
                    candidate.ast_node.directives, field_args
                )
                if cost_args is not None:
                    return cost_args

        field_type = get_named_type(field.type)
        if (
            isinstance(field_type, GraphQLObjectType)
            and field_type.ast_node
            and field_type.ast_node.directives
        ):
            return self.get_args_from_directives(
                field_type.ast_node.directives, field_args
            )
        return None

    def collect_execution_fields(
        self,
        selection_sets: list[SelectionSetNode],
        object_type: GraphQLObjectType,
        grouped_fields: dict[str, list[FieldNode]] | None = None,
        visited_fragments: frozenset = frozenset(),
    ) -> dict[str, list[FieldNode]]:
        """Group fields that will actually be executed by their response key.

        Fields are filtered through `@skip`/`@include` directives using the
        coerced variables, fragments are flattened into the grouping and
        memoized per (fragment, type) pair so that fragment DAGs shared
        between many spreads are not re-expanded exponentially.
        """
        if grouped_fields is None:
            grouped_fields = {}
        variables = self._coerced_variables or {}
        for selection_set in selection_sets:
            for selection in selection_set.selections:
                if not should_include_node(variables, selection):
                    continue
                if isinstance(selection, FieldNode):
                    response_key = (
                        selection.alias.value
                        if selection.alias
                        else selection.name.value
                    )
                    grouped_fields.setdefault(response_key, []).append(selection)
                elif isinstance(selection, InlineFragmentNode):
                    if self.type_condition_applies(
                        selection.type_condition, object_type
                    ):
                        self.collect_execution_fields(
                            [selection.selection_set],
                            object_type,
                            grouped_fields,
                            visited_fragments,
                        )
                elif isinstance(selection, FragmentSpreadNode):
                    self.collect_fragment_fields(
                        selection, object_type, grouped_fields, visited_fragments
                    )
        return grouped_fields

    def collect_fragment_fields(
        self,
        spread: FragmentSpreadNode,
        object_type: GraphQLObjectType,
        grouped_fields: dict[str, list[FieldNode]],
        visited_fragments: frozenset,
    ) -> None:
        fragment_name = spread.name.value
        if fragment_name in visited_fragments:
            if fragment_name not in self._reported_fragment_cycles:
                self._reported_fragment_cycles.add(fragment_name)
                self.context.report_error(
                    GraphQLError(
                        f'Cannot spread fragment "{fragment_name}" within itself.'
                    )
                )
            return
        fragment = self.context.get_fragment(fragment_name)
        if not fragment:
            return
        if not self.type_condition_applies(fragment.type_condition, object_type):
            return

        cache_key = (fragment_name, object_type.name)
        cached_fields = self._fragment_field_cache.get(cache_key)
        if cached_fields is None:
            cached_fields = self.collect_execution_fields(
                [fragment.selection_set],
                object_type,
                visited_fragments=visited_fragments | {fragment_name},
            )
            self._fragment_field_cache[cache_key] = cached_fields

        for response_key, field_nodes in cached_fields.items():
            existing_nodes = grouped_fields.setdefault(response_key, [])
            existing_ids = {id(node) for node in existing_nodes}
            existing_nodes.extend(
                node for node in field_nodes if id(node) not in existing_ids
            )

    def type_condition_applies(
        self,
        type_condition: NamedTypeNode | None,
        object_type: GraphQLObjectType,
    ) -> bool:
        if type_condition is None:
            return True
        condition_type = self.context.schema.get_type(type_condition.name.value)
        if condition_type is None:
            return False
        if condition_type is object_type:
            return True
        if isinstance(condition_type, GraphQLInterfaceType | GraphQLUnionType):
            return object_type in self.context.schema.get_possible_types(
                condition_type
            )
        return False

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
    operation_name: str | None = None,
    operation_aware: bool = False,
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
                operation_name=operation_name,
                operation_aware=operation_aware,
            )

    return cast(type[ASTValidationRule], _CostValidator)
