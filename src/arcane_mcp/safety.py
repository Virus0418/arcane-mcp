"""Destructive-operation classification and two-step confirmation."""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from arcane_mcp.spec import tool_name

logger = logging.getLogger(__name__)

DEFAULT_DESTRUCTIVE_PATTERNS: tuple[str, ...] = (r"/(prune|destroy|kill|down|restore|restore-files)$",)
# Arcane 3 folds lifecycle routes into POST ...?action=. These values stay
# behind the confirmation gate. start/stop/restart/pause and deploy's up do not.
# Comparison is case-insensitive; the stored request keeps the original text.
DESTRUCTIVE_ACTIONS = frozenset({"kill", "down", "restore", "restorefiles", "restore-files"})
TOKEN_TTL_SECONDS = 120
CONFIRM_TOOL_NAME = "confirm_operation"
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


class ConfirmationError(ValueError):
    """Unknown, expired or already-used confirmation token."""


def is_destructive(method: str, path: str, extra_patterns: Sequence[str] = ()) -> bool:
    if method.upper() == "DELETE":
        return True
    return any(re.search(p, path) for p in (*DEFAULT_DESTRUCTIVE_PATTERNS, *extra_patterns))


def destructive_action(arguments: Mapping[str, Any] | None) -> str | None:
    """Return the normalised destructive action on this call, if any.

    The token stores the original arguments. This only decides whether the call
    must be confirmed. A later call with a different action gets its own token.
    """
    if not arguments:
        return None
    raw = arguments.get("action")
    if not isinstance(raw, str):
        return None
    action = raw.strip().lower()
    if action in DESTRUCTIVE_ACTIONS:
        return action
    return None


def destructive_tool_names(spec: dict[str, Any], extra_patterns: Sequence[str] = ()) -> frozenset[str]:
    names: set[str] = set()
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId")
            if op_id and is_destructive(method, path, extra_patterns):
                names.add(tool_name(op_id))
    return frozenset(names)


@dataclass
class PendingOperation:
    tool: str
    arguments: dict[str, Any]
    expires_at: float


class ConfirmationStore:
    def __init__(
        self,
        ttl: float = TOKEN_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._token_factory = token_factory
        self._pending: dict[str, PendingOperation] = {}

    @property
    def ttl(self) -> float:
        return self._ttl

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[token]

    def pending_count(self) -> int:
        self._purge()
        return len(self._pending)

    def issue(self, tool: str, arguments: dict[str, Any]) -> tuple[str, PendingOperation]:
        self._purge()
        token = self._token_factory()
        pending = PendingOperation(tool=tool, arguments=dict(arguments), expires_at=self._clock() + self._ttl)
        self._pending[token] = pending
        return token, pending

    def redeem(self, token: str) -> PendingOperation:
        pending = self._pending.pop(token, None)
        if pending is None:
            raise ConfirmationError("Confirmation token is unknown or already used")
        if pending.expires_at <= self._clock():
            raise ConfirmationError("Confirmation token has expired; call the operation again to get a new one")
        return pending


class ConfirmationMiddleware(Middleware):
    """Intercepts destructive tool calls and returns a confirmation token instead of executing."""

    def __init__(
        self,
        store: ConfirmationStore,
        destructive: frozenset[str],
        *,
        exposed: frozenset[str] | None = None,
    ) -> None:
        self._store = store
        self._destructive = destructive
        # None keeps the direct unit-test servers, which register their own tools.
        # The OpenAPI server passes the names the route map actually exposes so a
        # hidden mutation cannot be turned into a confirmation prompt.
        self._exposed = exposed

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> ToolResult:
        name = context.message.name
        # confirm_operation replays a stored request. Gating it would deadlock
        # the token, including when a caller adds a stray action argument.
        if name == CONFIRM_TOOL_NAME:
            return await call_next(context)
        if self._exposed is not None and name not in self._exposed:
            return await call_next(context)
        arguments = dict(context.message.arguments or {})
        action = destructive_action(arguments)
        if name not in self._destructive and action is None:
            return await call_next(context)
        token, _pending = self._store.issue(name, arguments)
        logger.info("Destructive tool %s action=%s requested; issued confirmation token", name, action)
        ttl = int(self._store.ttl)
        bound = ""
        if action is not None:
            bound = (
                f" This token is bound to action {action!r} and cannot be reused for a different action."
            )
        payload = {
            "requires_confirmation": True,
            "token": token,
            "expires_in": ttl,
            "operation": name,
            "arguments": arguments,
            "message": (
                f"'{name}' is destructive and was NOT executed. Review the arguments, then call "
                f'{CONFIRM_TOOL_NAME}(token="{token}") within {ttl} seconds to proceed.{bound}'
            ),
        }
        if action is not None:
            payload["action"] = action
        return ToolResult(structured_content=payload)


def register_confirm_tool(mcp: FastMCP, store: ConfirmationStore) -> None:
    @mcp.tool(
        name=CONFIRM_TOOL_NAME,
        description=(
            "Execute one previously gated destructive request. Pass the token from that response. "
            "The token replays the original tool and arguments, including action, and cannot be "
            "pointed at a different action. Tokens are single-use and expire after 120 seconds."
        ),
    )
    async def confirm_operation(token: str) -> ToolResult:
        try:
            pending = store.redeem(token)
        except ConfirmationError as exc:
            raise ToolError(str(exc)) from exc
        logger.info("Confirmed destructive tool %s", pending.tool)
        return await mcp.call_tool(pending.tool, pending.arguments, run_middleware=False)
