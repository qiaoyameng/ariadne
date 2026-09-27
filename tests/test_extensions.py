import asyncio
import gc
from unittest.mock import Mock

import pytest

from ariadne import (
    ExtensionManager,
    QueryType,
    graphql,
    graphql_sync,
    make_executable_schema,
)
from ariadne.types import Extension

context = {}
exception = ValueError()


def test_request_started_hook_is_called_by_extension_manager():
    extension = Mock(spec=Extension)
    manager = ExtensionManager([Mock(return_value=extension)], context)
    with manager.request():
        pass

    extension.request_started.assert_called_once_with(context)


def test_request_finished_hook_is_called_by_extension_manager():
    extension = Mock(spec=Extension)
    manager = ExtensionManager([Mock(return_value=extension)], context)
    with manager.request():
        pass

    extension.request_finished.assert_called_once_with(context)


def test_has_errors_hook_is_called_with_errors_list_and_context():
    extension = Mock(spec=Extension)
    manager = ExtensionManager([Mock(return_value=extension)], context)
    manager.has_errors([exception])
    extension.has_errors.assert_called_once_with([exception], context)


def test_extension_format_hook_is_called_with_context():
    extension = Mock(spec=Extension, format=Mock(return_value={"a": 1}))
    manager = ExtensionManager([Mock(return_value=extension)], context)
    manager.format()
    extension.format.assert_called_once_with(context)


def test_extensions_are_formatted():
    extensions = [
        Mock(spec=Extension, format=Mock(return_value={"a": 1})),
        Mock(spec=Extension, format=Mock(return_value={"b": 2})),
    ]
    manager = ExtensionManager([Mock(return_value=ext) for ext in extensions])
    assert manager.format() == {"a": 1, "b": 2}


class BaseExtension(Extension):
    pass


@pytest.mark.asyncio
async def test_default_extension_hooks_dont_interrupt_query_execution(schema):
    _, response = await graphql(
        schema, {"query": "{ status }"}, extensions=[BaseExtension]
    )
    assert response["data"] == {"status": True}


class SyncLifecycleExtension(Extension):
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def request_started(self, context):
        self.events.append(f"{self.name}.request_started")

    def request_finished(self, context):
        self.events.append(f"{self.name}.request_finished")

    def has_errors(self, errors, context):
        self.events.append(f"{self.name}.has_errors")

    def format(self, context):
        self.events.append(f"{self.name}.format")
        return {self.name: True}


class AsyncLifecycleExtension(Extension):
    def __init__(self, name, events):
        self.name = name
        self.events = events

    async def request_started(self, context):
        await asyncio.sleep(0)
        self.events.append(f"{self.name}.request_started")

    async def request_finished(self, context):
        await asyncio.sleep(0)
        self.events.append(f"{self.name}.request_finished")

    async def has_errors(self, errors, context):
        await asyncio.sleep(0)
        self.events.append(f"{self.name}.has_errors")

    async def format(self, context):
        await asyncio.sleep(0)
        self.events.append(f"{self.name}.format")
        return {self.name: True}


@pytest.mark.asyncio
async def test_mixed_sync_and_async_extensions_hooks_are_called_in_order(schema):
    events = []
    extensions = [
        lambda: SyncLifecycleExtension("sync_ext", events),
        lambda: AsyncLifecycleExtension("async_ext", events),
    ]

    success, response = await graphql(
        schema, {"query": "{ status }"}, extensions=extensions
    )

    assert success
    assert events == [
        "sync_ext.request_started",
        "async_ext.request_started",
        "sync_ext.format",
        "async_ext.format",
        "async_ext.request_finished",
        "sync_ext.request_finished",
    ]
    assert response["extensions"] == {"sync_ext": True, "async_ext": True}


@pytest.mark.asyncio
async def test_has_errors_hook_completes_before_error_response_is_formatted(schema):
    events = []

    def error_formatter(error, debug=False):
        events.append("error_formatter")
        return {"message": str(error)}

    _, response = await graphql(
        schema,
        {"query": "{ testError }"},
        extensions=[lambda: AsyncLifecycleExtension("ext", events)],
        error_formatter=error_formatter,
    )

    assert response["errors"]
    assert events == [
        "ext.request_started",
        "ext.has_errors",
        "error_formatter",
        "ext.format",
        "ext.request_finished",
    ]


@pytest.mark.asyncio
async def test_validation_failure_exits_started_extensions(schema):
    events = []

    success, response = await graphql(
        schema,
        {"query": "{ invalidField }"},
        extensions=[lambda: AsyncLifecycleExtension("ext", events)],
    )

    assert not success
    assert response["errors"]
    assert events == [
        "ext.request_started",
        "ext.has_errors",
        "ext.format",
        "ext.request_finished",
    ]


@pytest.mark.asyncio
async def test_parsing_failure_exits_started_extensions(schema):
    events = []

    success, response = await graphql(
        schema,
        {"query": "{ invalid"},
        extensions=[lambda: AsyncLifecycleExtension("ext", events)],
    )

    assert not success
    assert response["errors"]
    assert events == [
        "ext.request_started",
        "ext.has_errors",
        "ext.format",
        "ext.request_finished",
    ]


@pytest.mark.asyncio
async def test_failed_request_started_cleans_up_only_previously_started_extensions():
    events = []

    class FailingStartExtension(Extension):
        def request_started(self, context):
            events.append("failing.request_started")
            raise ValueError("start failed")

        def request_finished(self, context):
            events.append("failing.request_finished")

    manager = ExtensionManager(
        [
            lambda: SyncLifecycleExtension("first", events),
            FailingStartExtension,
            lambda: SyncLifecycleExtension("never_started", events),
        ],
        context,
    )

    with pytest.raises(ValueError, match="start failed"):
        async with manager.request_async():
            pass

    assert events == [
        "first.request_started",
        "failing.request_started",
        "first.request_finished",
    ]


@pytest.mark.asyncio
async def test_request_finished_errors_dont_prevent_other_extensions_cleanup():
    events = []

    class FailingFinishExtension(Extension):
        def __init__(self, name):
            self.name = name

        def request_started(self, context):
            events.append(f"{self.name}.request_started")

        def request_finished(self, context):
            events.append(f"{self.name}.request_finished")
            raise ValueError(self.name)

    manager = ExtensionManager(
        [
            lambda: FailingFinishExtension("first"),
            lambda: SyncLifecycleExtension("middle", events),
            lambda: FailingFinishExtension("last"),
        ],
        context,
    )

    with pytest.raises(ValueError, match="last"):
        async with manager.request_async():
            pass

    assert events == [
        "first.request_started",
        "middle.request_started",
        "last.request_started",
        "last.request_finished",
        "middle.request_finished",
        "first.request_finished",
    ]


@pytest.mark.asyncio
async def test_request_finished_errors_dont_mask_request_processing_exception():
    events = []

    class FailingFinishExtension(Extension):
        def request_finished(self, context):
            events.append("failing.request_finished")
            raise ValueError("cleanup failed")

    manager = ExtensionManager(
        [
            FailingFinishExtension,
            lambda: SyncLifecycleExtension("other", events),
        ],
        context,
    )

    with pytest.raises(KeyError, match="original"):
        async with manager.request_async():
            raise KeyError("original")

    assert events == [
        "other.request_started",
        "other.request_finished",
        "failing.request_finished",
    ]


@pytest.mark.asyncio
async def test_cancelled_request_exits_started_extensions():
    events = []
    resolver_entered = asyncio.Event()

    query = QueryType()

    @query.field("slow")
    async def resolve_slow(*_):
        resolver_entered.set()
        await asyncio.sleep(60)
        return None

    slow_schema = make_executable_schema("type Query { slow: String }", query)

    task = asyncio.create_task(
        graphql(
            slow_schema,
            {"query": "{ slow }"},
            extensions=[lambda: AsyncLifecycleExtension("ext", events)],
        )
    )
    await asyncio.wait_for(resolver_entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["ext.request_started", "ext.request_finished"]


@pytest.mark.asyncio
async def test_concurrent_requests_use_isolated_extension_instances(schema):
    instances = []

    class TrackingExtension(Extension):
        def __init__(self):
            self.seen_request_ids = []
            instances.append(self)

        async def request_started(self, context):
            self.seen_request_ids.append(context["request_id"])
            await asyncio.sleep(0.01)

        async def request_finished(self, context):
            await asyncio.sleep(0.01)
            assert self.seen_request_ids == [context["request_id"]]

    results = await asyncio.gather(
        *(
            graphql(
                schema,
                {"query": "{ status }"},
                context_value={"request_id": request_id},
                extensions=[TrackingExtension],
            )
            for request_id in range(5)
        )
    )

    assert all(success for success, _ in results)
    assert len(instances) == 5


@pytest.mark.asyncio
async def test_extension_resolve_middleware_works_with_async_lifecycle_hooks(schema):
    events = []

    class AsyncMiddlewareExtension(Extension):
        async def request_started(self, context):
            events.append("request_started")

        def resolve(self, next_, obj, info, **kwargs):
            events.append(f"resolve.{info.field_name}")
            return next_(obj, info, **kwargs)

        async def request_finished(self, context):
            events.append("request_finished")

    success, response = await graphql(
        schema, {"query": "{ status }"}, extensions=[AsyncMiddlewareExtension]
    )

    assert success
    assert response["data"] == {"status": True}
    assert events == ["request_started", "resolve.status", "request_finished"]


def test_graphql_sync_supports_sync_extensions(schema):
    events = []

    success, response = graphql_sync(
        schema,
        {"query": "{ status }"},
        extensions=[lambda: SyncLifecycleExtension("ext", events)],
    )

    assert success
    assert events == ["ext.request_started", "ext.format", "ext.request_finished"]
    assert response["extensions"] == {"ext": True}


class AsyncOnlyExtension(Extension):
    async def request_started(self, context):
        pass

    async def request_finished(self, context):
        pass

    async def has_errors(self, errors, context):
        pass

    async def format(self, context):
        return {"ext": True}


def test_graphql_sync_raises_error_for_async_request_started_hook(schema):
    with pytest.raises(RuntimeError, match="asynchronous extension hooks"):
        graphql_sync(schema, {"query": "{ status }"}, extensions=[AsyncOnlyExtension])


def test_graphql_sync_raises_error_for_async_has_errors_hook(schema):
    class AsyncHasErrorsExtension(Extension):
        async def has_errors(self, errors, context):
            pass

    with pytest.raises(RuntimeError, match="asynchronous extension hooks"):
        graphql_sync(
            schema, {"query": "{ testError }"}, extensions=[AsyncHasErrorsExtension]
        )


def test_graphql_sync_raises_error_for_async_format_hook(schema):
    class AsyncFormatExtension(Extension):
        async def format(self, context):
            return {"ext": True}

    with pytest.raises(RuntimeError, match="asynchronous extension hooks"):
        graphql_sync(schema, {"query": "{ status }"}, extensions=[AsyncFormatExtension])


def test_graphql_sync_raises_error_for_async_request_finished_hook(schema):
    class AsyncFinishedExtension(Extension):
        async def request_finished(self, context):
            pass

    with pytest.raises(RuntimeError, match="asynchronous extension hooks"):
        graphql_sync(
            schema, {"query": "{ status }"}, extensions=[AsyncFinishedExtension]
        )


def test_graphql_sync_async_hooks_dont_cause_unawaited_coroutine_warnings(
    schema, recwarn
):
    with pytest.raises(RuntimeError, match="asynchronous extension hooks"):
        graphql_sync(schema, {"query": "{ status }"}, extensions=[AsyncOnlyExtension])

    gc.collect()

    unawaited_warnings = [
        warning
        for warning in recwarn.list
        if "was never awaited" in str(warning.message)
    ]
    assert not unawaited_warnings
