from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from inspect import isawaitable

from graphql import GraphQLError
from graphql.execution import MiddlewareManager

from .types import ContextValue, Extension, ExtensionList, MiddlewareList


def _close_unawaited_awaitable(awaitable) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


def _async_hook_not_supported_error(hook_name: str) -> RuntimeError:
    return RuntimeError(
        f"Extension hook '{hook_name}' returned an awaitable, but asynchronous "
        "extension hooks are not supported by the synchronous query executor. "
        "Use the asynchronous 'graphql' function or make the hook synchronous."
    )


class ExtensionManager:
    """Container and runner for extensions and middleware, used by the GraphQL servers.

    # Attributes

    `context`: the `ContextValue` of type specific to the server.

    `extensions`: a `tuple` with instances of initialized extensions.

    `extensions_reversed`: a `tuple` created from reversing `extensions`.
    """

    __slots__ = ("context", "extensions", "extensions_reversed")

    def __init__(
        self,
        extensions: ExtensionList | None = None,
        context: ContextValue | None = None,
    ) -> None:
        """Initializes extensions and stores them with context on instance.

        # Optional arguments

        `extensions`: a `list` of `Extension` types to initialize.

        `context`: the `ContextValue` of type specific to the server.
        """
        self.context = context

        if extensions:
            self.extensions = tuple(ext() for ext in extensions)
            self.extensions_reversed = tuple(reversed(self.extensions))
        else:
            self.extensions_reversed = self.extensions = tuple()

    def as_middleware_manager(
        self,
        middleware: MiddlewareList = None,
        manager_class: type[MiddlewareManager] | None = None,
    ) -> MiddlewareManager | None:
        """Creates middleware manager instance combining middleware and extensions.

        Returns instance of the type passed in `manager_class` argument
        or `MiddlewareManager` that query executor then uses.

        # Optional arguments

        `middleware`: a `list` of `Middleware` instances

        `manager_class` a `type` of middleware manager to use. `MiddlewareManager`
        is used if this argument is passed `None` or omitted.
        """
        if not middleware and not self.extensions:
            return None

        middleware = middleware or []
        if manager_class:
            return manager_class(*middleware, *self.extensions)

        return MiddlewareManager(*middleware, *self.extensions)

    @contextmanager
    def request(self) -> Generator[None, None, None]:
        """A context manager that should wrap request processing.

        Runs `request_started` hook at beginning and `request_finished` at
        the end of request processing, enabling APM extensions like ApolloTracing.

        Asynchronous hooks are not supported here and raise `RuntimeError`.
        Use `request_async` with the asynchronous query executor instead.
        """
        started: list[Extension] = []
        error: BaseException | None = None
        try:
            for ext in self.extensions:
                result = ext.request_started(self.context)
                if isawaitable(result):
                    _close_unawaited_awaitable(result)
                    raise _async_hook_not_supported_error("request_started")
                started.append(ext)
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._run_request_finished(started, suppress_errors=error is not None)

    def _run_request_finished(
        self, started: list[Extension], *, suppress_errors: bool
    ) -> None:
        errors: list[Exception] = []
        for ext in reversed(started):
            try:
                result = ext.request_finished(self.context)
                if isawaitable(result):
                    _close_unawaited_awaitable(result)
                    raise _async_hook_not_supported_error("request_finished")
            except Exception as exc:
                errors.append(exc)
        if errors and not suppress_errors:
            raise errors[0]

    @asynccontextmanager
    async def request_async(self) -> AsyncGenerator[None, None]:
        """An async context manager that should wrap request processing.

        Awaits `request_started` hooks in extensions registration order at
        the beginning and `request_finished` hooks in reversed order at the
        end of request processing, enabling extensions to use asynchronous
        tracing or auditing services.

        If a `request_started` hook fails, only extensions that were already
        started are finished. `request_finished` hooks are always all called,
        even if some of them fail. An exception raised by request processing
        takes precedence over errors raised by `request_finished` hooks, but
        if request processing didn't raise, the first `request_finished`
        error is reraised to the caller.
        """
        started: list[Extension] = []
        error: BaseException | None = None
        try:
            for ext in self.extensions:
                result = ext.request_started(self.context)
                if isawaitable(result):
                    await result
                started.append(ext)
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            await self._run_request_finished_async(
                started, suppress_errors=error is not None
            )

    async def _run_request_finished_async(
        self, started: list[Extension], *, suppress_errors: bool
    ) -> None:
        errors: list[Exception] = []
        for ext in reversed(started):
            try:
                result = ext.request_finished(self.context)
                if isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(exc)
        if errors and not suppress_errors:
            raise errors[0]

    def has_errors(self, errors: list[GraphQLError]):
        """Propagates GraphQL errors returned by GraphQL server to extensions.

        Should be called only when there are errors.

        Asynchronous hooks are not supported here and raise `RuntimeError`.
        Use `has_errors_async` with the asynchronous query executor instead.
        """
        for ext in self.extensions:
            result = ext.has_errors(errors, self.context)
            if isawaitable(result):
                _close_unawaited_awaitable(result)
                raise _async_hook_not_supported_error("has_errors")

    async def has_errors_async(self, errors: list[GraphQLError]):
        """Asynchronously propagates GraphQL errors to extensions.

        Awaits `has_errors` hooks in extensions registration order.
        Should be called only when there are errors.
        """
        for ext in self.extensions:
            result = ext.has_errors(errors, self.context)
            if isawaitable(result):
                await result

    def format(self) -> dict:
        """Gathers data from extensions for inclusion in server's response JSON.

        This data can be retrieved from the `extensions` key in response JSON.

        Returns `dict` with JSON-serializable data.

        Asynchronous hooks are not supported here and raise `RuntimeError`.
        Use `format_async` with the asynchronous query executor instead.
        """
        data = {}
        for ext in self.extensions:
            ext_data = ext.format(self.context)
            if isawaitable(ext_data):
                _close_unawaited_awaitable(ext_data)
                raise _async_hook_not_supported_error("format")
            if ext_data:
                data.update(ext_data)
        return data

    async def format_async(self) -> dict:
        """Asynchronously gathers data from extensions for server's response.

        Awaits `format` hooks in extensions registration order.

        Returns `dict` with JSON-serializable data.
        """
        data = {}
        for ext in self.extensions:
            ext_data = ext.format(self.context)
            if isawaitable(ext_data):
                ext_data = await ext_data
            if ext_data:
                data.update(ext_data)
        return data
