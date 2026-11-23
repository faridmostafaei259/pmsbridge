"""The tool framework: every MCP tool is registered through `pms_tool` or `account_tool`.

A tool function states what it needs and does only its own work; the framework does the rest, in
this order, for every call:

1. builds the caller's `ToolContext` from the verified token (never from an argument);
2. finds the connection (`pms_tool` only) among the ones the token allows;
3. refuses a connection that is turned off or needs attention, without building a provider;
4. refuses a provider-specific tool on a connection of another provider;
5. for a write capability, requires an administrator to have allowed changes on the connection;
6. loads the provider and checks the capability with `require_api`;
7. sets `call_deadline` and stops the call shortly after it;
8. maps every error to a message that is safe to show (never raw PMS text, never a stack trace);
9. refuses a result larger than `MAX_RESULT_BYTES`;
10. writes one audit entry (who, connection, tool, outcome, time; never arguments or results);
11. marks the tool read-only or as a change in its MCP annotations.

A tool function takes a `ToolCall` (or a `ToolContext` for `account_tool`) as its first argument.
Its other arguments become the tool's parameters; `pms_tool` adds the optional `connection`
parameter itself.
"""

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Final

import pydantic_core
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from pmsbridge.core import audit
from pmsbridge.mcp.context import (
    ToolContext,
    ToolRefusedError,
    build_context,
    current_principal,
    resolve_connection_state,
)
from pmsbridge.pms import registry
from pmsbridge.pms.base import (
    WRITE_CAPABILITIES,
    Capability,
    CapabilityNotSupportedError,
    ConnectionRef,
    PMSError,
    PMSProvider,
    PMSRateLimitedError,
    Provider,
    call_deadline,
    require_api,
)

logger = logging.getLogger(__name__)

DEADLINE_SECONDS: Final = 25.0
# Adapters stop at the deadline themselves; this lets them finish their own error before the call
# is cut off.
DEADLINE_GRACE_SECONDS: Final = 2.0
MAX_RESULT_BYTES: Final = 256 * 1024
MAX_ERROR_CHARS: Final = 500
DEFAULT_LIMIT: Final = 25
MAX_LIMIT: Final = 100

CONNECTION_PARAMETER: Final = "connection"
CONNECTION_DESCRIPTION: Final = (
    "The connection to use: its name or id as shown by list_connections. Optional when only one "
    "connection is available."
)
WRITE_NOTICE: Final = (
    "This changes data in the property management system. It only works on connections where an "
    "administrator has allowed changes, and it is never repeated automatically."
)
_STATUS_WORDS: Final = {"disabled": "it is turned off", "needs_attention": "it needs attention"}
GENERIC_ERROR: Final = (
    "Something went wrong while handling this request. Nothing is shown here to protect "
    "private data; the person running pmsbridge can find details in its logs."
)


@dataclass(frozen=True, slots=True)
class ToolCall[A]:
    """What a `pms_tool` function receives: the caller, the connection and the checked API."""

    ctx: ToolContext
    connection: ConnectionRef
    provider: PMSProvider
    api: A


def clamp_limit(
    limit: int | None, *, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT
) -> int:
    """A list size in 1..maximum (the default when not given). Tools use it for every list."""
    if limit is None:
        return default
    return max(1, min(limit, maximum))


@dataclass(slots=True)
class _Run:
    tool: str
    write: bool
    ctx: ToolContext | None = None
    connection_id: str | None = None
    status: str = "ok"
    error_class: str | None = None


def _safe_text(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_ERROR_CHARS else flat[: MAX_ERROR_CHARS - 1] + "..."


def _pms_message(exc: PMSError) -> str:
    message = _safe_text(exc.message)
    if isinstance(exc, PMSRateLimitedError) and exc.retry_after_seconds is not None:
        message += f" Try again in about {max(1, round(exc.retry_after_seconds))} seconds."
    return message


def _timeout_message(write: bool) -> str:
    if write:
        return (
            "The request took too long and was stopped. The outcome is unknown: the change may or "
            "may not have been made. Check the current state before trying again."
        )
    return "The property management system did not answer in time. Try again, or ask for less."


@asynccontextmanager
async def _guarded(run: _Run) -> AsyncGenerator[None]:
    """Deadline, error mapping and the audit entry around one call.

    A call whose caller could not be identified (`run.ctx` is None) is refused and not audited.
    """
    started = time.monotonic()
    token = call_deadline.set(started + DEADLINE_SECONDS)
    try:
        async with asyncio.timeout(DEADLINE_SECONDS + DEADLINE_GRACE_SECONDS):
            yield
    except ToolRefusedError as exc:
        run.status, run.error_class = "denied", exc.code
        raise
    except ToolError as exc:
        # A tool or the framework may already have named the failure (for example ResultTooLarge).
        run.status = "error"
        run.error_class = run.error_class or type(exc).__name__
        raise
    except CapabilityNotSupportedError as exc:
        run.status, run.error_class = "denied", type(exc).__name__
        raise ToolError(_pms_message(exc)) from None
    except PMSError as exc:
        run.status, run.error_class = "error", type(exc).__name__
        raise ToolError(_pms_message(exc)) from None
    except asyncio.CancelledError:
        # The caller went away. The audit entry below is still written.
        run.status, run.error_class = "error", "Cancelled"
        raise
    except TimeoutError:
        run.status, run.error_class = "timeout", "DeadlineExceeded"
        raise ToolError(_timeout_message(run.write)) from None
    except Exception as exc:
        run.status, run.error_class = "error", type(exc).__name__
        logger.exception("tool crashed", extra={"tool": run.tool})
        raise ToolError(GENERIC_ERROR) from None
    finally:
        call_deadline.reset(token)
        ctx = run.ctx
        if ctx is not None:
            elapsed = round((time.monotonic() - started) * 1000)
            await asyncio.shield(
                audit.record(
                    user_id=ctx.user_id,
                    client_id=ctx.client_id,
                    connection_id=run.connection_id,
                    tool=run.tool,
                    status=run.status,
                    duration_ms=elapsed,
                    error_class=run.error_class,
                )
            )


async def _identify(run: _Run) -> ToolContext:
    principal = current_principal()
    if principal is None:
        raise ToolRefusedError("Sign-in is required to use this tool.", "not_signed_in")
    run.ctx = await build_context(principal)
    return run.ctx


def _check_size(result: object, run: _Run) -> None:
    size = len(pydantic_core.to_json(result, serialize_unknown=True))
    if size > MAX_RESULT_BYTES:
        run.status, run.error_class = "error", "ResultTooLarge"
        raise ToolError(
            f"The result is too large to return ({size // 1024} KiB). "
            "Ask for fewer items or narrow the request with filters."
        )


def _tool_signature(
    fn: Callable[..., Any], *, first: str, add_connection: bool
) -> tuple[inspect.Signature, list[inspect.Parameter]]:
    signature = inspect.signature(fn, eval_str=True)
    params = list(signature.parameters.values())
    if not params or params[0].name != first:
        raise TypeError(f"{fn.__name__} must take '{first}' as its first argument")
    rest = [param.replace(kind=inspect.Parameter.KEYWORD_ONLY) for param in params[1:]]
    if any(param.name == CONNECTION_PARAMETER for param in rest) and add_connection:
        raise TypeError(f"{fn.__name__} must not declare '{CONNECTION_PARAMETER}' itself")
    if add_connection:
        rest.append(
            inspect.Parameter(
                CONNECTION_PARAMETER,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=Annotated[str | None, Field(description=CONNECTION_DESCRIPTION)],
            )
        )
    return signature, rest


def _publish(
    mcp: MCPServer,
    wrapper: Callable[..., Awaitable[object]],
    fn: Callable[..., Any],
    signature: inspect.Signature,
    params: list[inspect.Parameter],
    *,
    name: str,
    title: str | None,
    write: bool,
) -> None:
    wrapper.__name__ = name
    wrapper.__qualname__ = name
    wrapper.__module__ = fn.__module__
    wrapper.__annotations__ = {}
    doc = inspect.cleandoc(fn.__doc__ or "")
    if not doc:
        raise TypeError(f"{fn.__name__} needs a docstring: it is the tool's description")
    wrapper.__doc__ = f"{doc}\n\n{WRITE_NOTICE}" if write else doc
    # The SDK reads the signature to build the tool's input schema.
    wrapper.__dict__["__signature__"] = signature.replace(parameters=params)
    mcp.add_tool(
        wrapper,
        name=name,
        title=title,
        annotations=ToolAnnotations(
            title=title,
            read_only_hint=not write,
            destructive_hint=write,
            idempotent_hint=not write,
            open_world_hint=True,
        ),
    )


def account_tool(
    mcp: MCPServer, *, name: str, title: str | None = None
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Register a read-only tool that is about the caller, not about one PMS connection.

    The function's first argument is the `ToolContext`.
    """

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        signature, params = _tool_signature(fn, first="ctx", add_connection=False)

        async def wrapper(**kwargs: Any) -> object:
            run = _Run(tool=name, write=False)
            async with _guarded(run):
                ctx = await _identify(run)
                result = await fn(ctx, **kwargs)
                _check_size(result, run)
                return result

        _publish(mcp, wrapper, fn, signature, params, name=name, title=title, write=False)
        return fn

    return decorator


def pms_tool[A](
    mcp: MCPServer,
    *,
    name: str,
    capability: Capability,
    api: Callable[[PMSProvider], A | None],
    provider: Provider | None = None,
    title: str | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Register a tool that works on one PMS connection.

    `capability` is what the tool needs (a write capability makes it a gated change), and `api`
    picks the domain API from the provider, for example `lambda p: p.listings`. `provider` is set
    for a provider-specific tool (ADR 0010). The function's first argument is a `ToolCall`.
    """
    write = capability in WRITE_CAPABILITIES

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        signature, params = _tool_signature(fn, first="call", add_connection=True)

        async def wrapper(**kwargs: Any) -> object:
            connection_arg = kwargs.pop(CONNECTION_PARAMETER, None)
            run = _Run(tool=name, write=write)
            async with _guarded(run):
                ctx = await _identify(run)
                chosen = await resolve_connection_state(ctx, connection_arg)
                run.connection_id = chosen.ref.connection_id
                label = chosen.ref.label
                if not chosen.usable:
                    raise ToolRefusedError(
                        f'The connection "{label}" is not available right now '
                        f"({_STATUS_WORDS.get(chosen.status, 'it needs attention')}). "
                        "An administrator must fix it on the Connections page.",
                        f"connection_{chosen.status}",
                    )
                if provider is not None and chosen.ref.provider != provider:
                    raise ToolRefusedError(
                        f"This tool only works with {provider.value.capitalize()} connections, "
                        f'and "{label}" is a {chosen.ref.provider.value.capitalize()} connection.',
                        "wrong_provider",
                    )
                if write and not chosen.writes_enabled:
                    raise ToolRefusedError(
                        f'Changes are not allowed on the connection "{label}". An administrator '
                        "must allow changes for it on the Connections page first.",
                        "writes_disabled",
                    )
                loaded = await registry.load_provider(chosen.ref.connection_id)
                try:
                    selected = require_api(loaded, capability, api(loaded))
                    result = await fn(
                        ToolCall(ctx, chosen.ref, loaded, selected),
                        **kwargs,
                    )
                finally:
                    await loaded.aclose()
                _check_size(result, run)
                return result

        _publish(mcp, wrapper, fn, signature, params, name=name, title=title, write=write)
        return fn

    return decorator
