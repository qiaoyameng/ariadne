import json
from http import HTTPStatus

import pytest

from ariadne.asgi import GraphQL
from ariadne.asgi.handlers import GraphQLHTTPHandler
from ariadne.types import Extension


class AsyncLifecycleExtension(Extension):
    def __init__(self, events):
        self.events = events

    async def request_started(self, context):
        self.events.append("request_started")

    async def request_finished(self, context):
        self.events.append("request_finished")

    async def has_errors(self, errors, context):
        self.events.append("has_errors")

    async def format(self, context):
        self.events.append("format")
        return {"asyncExt": True}


async def post_graphql_query(app, query):
    body = json.dumps({"query": query}).encode("utf-8")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1")),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    sent_messages = []
    request_body_sent = False

    async def receive():
        nonlocal request_body_sent
        if not request_body_sent:
            request_body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent_messages.append(message)

    await app(scope, receive, send)

    status = next(
        message["status"]
        for message in sent_messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in sent_messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(response_body.decode("utf-8"))


@pytest.mark.asyncio
async def test_async_extension_lifecycle_is_supported_by_asgi_http_handler(schema):
    events = []
    app = GraphQL(
        schema,
        http_handler=GraphQLHTTPHandler(
            extensions=[lambda: AsyncLifecycleExtension(events)]
        ),
    )

    status, response = await post_graphql_query(app, "{ status }")

    assert status == HTTPStatus.OK
    assert response["data"] == {"status": True}
    assert response["extensions"] == {"asyncExt": True}
    assert events == ["request_started", "format", "request_finished"]


@pytest.mark.asyncio
async def test_async_extension_has_errors_hook_is_called_by_asgi_http_handler(schema):
    events = []
    app = GraphQL(
        schema,
        http_handler=GraphQLHTTPHandler(
            extensions=[lambda: AsyncLifecycleExtension(events)]
        ),
    )

    status, response = await post_graphql_query(app, "{ testError }")

    assert status == HTTPStatus.OK
    assert response["errors"]
    assert response["extensions"] == {"asyncExt": True}
    assert events == [
        "request_started",
        "has_errors",
        "format",
        "request_finished",
    ]
