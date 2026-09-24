"""
Example: Federated subgraph using a batch reference resolver.

Instead of resolving entity representations one by one (which may cause
N+1 queries against a database), a batch reference resolver receives all
representations of its type from a single `_entities` request at once and
returns one result per representation, in the same order.

Run with:

    uvicorn examples.federated_batch_reference_example:app --reload

or:
    uv run --with "uvicorn[standard]" --with ariadne \\
        uvicorn examples.federated_batch_reference_example:app --reload

Then query the subgraph the way a federation gateway would:

    curl -X POST http://127.0.0.1:8000/graphql \\
        -H "Content-Type: application/json" \\
        -d '{
            "query": "query($representations: [_Any!]!) { '
                '_entities(representations: $representations) { '
                '... on Product { upc name price } } }",
            "variables": {
                "representations": [
                    {"__typename": "Product", "upc": "1"},
                    {"__typename": "Product", "upc": "2"},
                    {"__typename": "Product", "upc": "3"}
                ]
            }
        }'
"""

from starlette.applications import Starlette

from ariadne.asgi import GraphQL
from ariadne.contrib.federation import FederatedObjectType, make_federated_schema

type_defs = """
    type Query {
        topProducts: [Product]
    }

    type Product @key(fields: "upc") {
        upc: String!
        name: String
        price: Int
    }
"""

# Fake "database" of products.
PRODUCTS = {
    "1": {"upc": "1", "name": "Table", "price": 899},
    "2": {"upc": "2", "name": "Couch", "price": 1299},
    "3": {"upc": "3", "name": "Chair", "price": 549},
}

product = FederatedObjectType("Product")


@product.batch_reference_resolver
def resolve_product_references(_obj, _info, representations):
    # All representations of the "Product" type from a single `_entities`
    # request are delivered here at once, in their original order, so the
    # data can be fetched with a single query instead of one query per
    # representation. The returned list must have the same length and order
    # as the received representations.
    upcs = [representation["upc"] for representation in representations]
    print(f"Batch fetching {len(upcs)} products in a single query: {upcs}")
    return [PRODUCTS.get(upc) for upc in upcs]


schema = make_federated_schema(type_defs, product)

app = Starlette()
graphql_app = GraphQL(schema, debug=True)
app.mount("/graphql", graphql_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
