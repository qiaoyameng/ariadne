"""
Example: Apollo Federation entities resolved in batches with Ariadne and Starlette.

Instead of calling a reference resolver once per representation (N+1 queries),
a batch reference resolver receives all representations for its type in a
single call and fetches them with one query to the data source.

Run with:

    uvicorn examples.federation_batch_example:app --reload

or:
    uv run --with "uvicorn[standard]" --with ariadne \\
        uvicorn examples.federation_batch_example:app --reload

Then query the `_entities` field like a gateway would:

    curl -X POST http://127.0.0.1:8000/graphql \\
        -H "Content-Type: application/json" \\
        -d '{
            "query": "query($reps: [_Any!]!) { _entities(representations: $reps)
                { ... on Product { upc name price } } }",
            "variables": {
                "reps": [
                    {"__typename": "Product", "upc": "1"},
                    {"__typename": "Product", "upc": "2"},
                    {"__typename": "Product", "upc": "1"}
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

PRODUCTS_DB = {
    "1": {"upc": "1", "name": "Table", "price": 899},
    "2": {"upc": "2", "name": "Couch", "price": 1299},
    "3": {"upc": "3", "name": "Chair", "price": 549},
}

product = FederatedObjectType("Product")


@product.batch_reference_resolver
async def resolve_products_reference(_, _info, representations):
    """Fetch all requested products with a single batched query.

    Called once per `_entities` request with all `Product` representations,
    in request order. Must return one result per representation.
    """
    upcs = [representation["upc"] for representation in representations]
    # A real service would do one `SELECT ... WHERE upc IN (...)` here.
    return [PRODUCTS_DB.get(upc) for upc in upcs]


schema = make_federated_schema(type_defs, product)

app = Starlette()
graphql_app = GraphQL(schema, debug=True)
app.mount("/graphql", graphql_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
