"""clipper.actions — Action Registry.

The action registry is the single source of truth for every capability
clipper exposes. The MCP server pulls from this registry — every registered
action becomes a tool, with no separate wiring needed when a new one is added.

Usage::

    from clipper.actions import Action, register, get, registry
    from pydantic import BaseModel

    class MyParams(BaseModel):
        value: int

    async def my_handler(flipper, params: MyParams) -> dict:
        return {"result": params.value}

    register(Action(
        name="my_action",
        description="Does something",
        params=MyParams,
        handler=my_handler,
    ))

Design notes:
- Fail fast: ActionParamError is raised on validation failure — the raw
  pydantic ValidationError is never swallowed or returned as None.
- Concurrency: handlers call FlipperConnection.send_command which is already
  lock-protected; no additional locking is needed here.
- Safety gate: every Action carries an emissive flag. ``invoke`` checks the
  flag against the safety gate; this dataclass only stores it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

import clipper.audit as audit

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ActionError(Exception):
    """Base for all action-layer errors."""


class ActionNotFound(ActionError):
    """No action with that name is registered."""


class ActionParamError(ActionError):
    """Raised when params fail pydantic validation.

    Attributes:
        action: Name of the action that rejected the params.
        errors: Pydantic v2 error list (each entry has 'loc', 'msg', 'type').
    """

    def __init__(self, action: str, errors: list[dict]) -> None:
        super().__init__(f"invalid params for {action!r}: {errors}")
        self.action = action
        self.errors = errors


class ActionRuntimeError(ActionError):
    """Raised when an action handler fails after passing validation/emission gates.

    Wraps any non-clipper exception (e.g. ``RuntimeError`` from a handler that
    saw the device reject its command, or ``ValueError`` from a parse failure)
    so the MCP layer can surface a clean tool error instead of leaking a raw
    traceback to the client.

    Attributes:
        action: Name of the action that failed.
        detail: Human-readable description of what went wrong.
    """

    def __init__(self, action: str, detail: str) -> None:
        super().__init__(f"{action}: {detail}")
        self.action = action
        self.detail = detail


class EmissionBlocked(ActionError):
    """Raised when an emissive action is invoked without CLIPPER_ALLOW_EMIT=1.

    The action name is the exception's string value so MCP layer can
    surface it directly without str() gymnastics.

    Fail-fast: let this propagate to the MCP layer cleanly.
    """

    def __init__(self, action_name: str) -> None:
        super().__init__(action_name)
        self.action_name = action_name

    def __str__(self) -> str:
        return self.action_name


# ---------------------------------------------------------------------------
# Handler type alias
# ---------------------------------------------------------------------------

# Handler signature: (flipper_connection, validated_params_model) -> dict
Handler = Callable[["FlipperConnection", BaseModel], Awaitable[dict]]


# ---------------------------------------------------------------------------
# Action dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """A single capability entry in the registry.

    Attributes:
        name:        Unique identifier; used as the MCP tool name.
        description: Human-readable description surfaced as the MCP tool description.
        params:      Pydantic model class that defines + validates the input params.
        handler:     Async callable (flipper, params_model) -> dict.
        emissive:    True if the action transmits RF/IR/USB — requires safety gate.
        redact_params: Names of params whose VALUES must never reach the audit
                     log (credential material like RFID/iButton key_data). They
                     are replaced with "***" before audit.log sees them.
    """

    name: str
    description: str
    params: type[BaseModel]
    handler: Handler
    emissive: bool = field(default=False)
    redact_params: frozenset[str] = field(default_factory=frozenset)

    def json_schema(self) -> dict:
        """Return the JSON Schema for this action's params (pydantic v2 model_json_schema)."""
        return self.params.model_json_schema()

    def _audit_params(self, raw_params: dict) -> dict:
        """Return a copy of *raw_params* with redact_params values masked.

        The audit log persists params to disk; secrets (e.g. key_data) MUST be
        masked here so they never land in ~/.clipper/audit.log.
        """
        if not self.redact_params:
            return raw_params
        return {
            k: ("***" if k in self.redact_params else v) for k, v in raw_params.items()
        }

    async def invoke(
        self,
        flipper: FlipperConnection,
        raw_params: dict,
        transport: str = "mcp",
    ) -> dict:
        """Validate raw_params, enforce the emission gate, then call the handler.

        Args:
            flipper:    Live FlipperConnection (or None in tests that don't need it).
            raw_params: Unvalidated dict from the caller (the MCP tool input).
            transport:  Caller transport label, recorded in the audit log for
                        every emissive action. Defaults to "mcp".

        Returns:
            Dict result from the handler.

        Raises:
            ActionParamError:  if pydantic rejects raw_params (fail fast).
            EmissionBlocked:   if this action is emissive and the safety gate is off.
                               Audit log entry written with outcome="denied" before raise.
        """
        # --- 1. Param validation (before touching hardware or audit log) ---
        try:
            model = self.params.model_validate(raw_params)
        except ValidationError as exc:
            log.debug("param validation failed for action %r: %s", self.name, exc)
            raise ActionParamError(self.name, exc.errors()) from exc

        # --- 2. Safety gate ---
        # Gate state comes from CLIPPER_SAFETY (or the legacy CLIPPER_ALLOW_EMIT
        # alias) in the environment — set it for headless launches like MCP stdio.
        from clipper.safety import safety_allowed  # local import: avoid cycle at import time

        # Redact sensitive param values (e.g. credential key_data) before any
        # audit.log call — the audit log is persisted to disk (C1 fix).
        audit_params = self._audit_params(raw_params)

        if self.emissive and not safety_allowed():
            audit.log(
                transport=transport,
                action=self.name,
                params=audit_params,
                outcome="denied",
                detail="emit gate is off",
            )
            raise EmissionBlocked(self.name)

        # --- 3. Handler execution (only emissive actions are audited) ---
        if self.emissive:
            try:
                result = await self.handler(flipper, model)
            except Exception as exc:
                audit.log(
                    transport=transport,
                    action=self.name,
                    params=audit_params,
                    outcome="error",
                    detail=str(exc),
                )
                self._reraise_handler_exception(exc)
            audit.log(
                transport=transport,
                action=self.name,
                params=audit_params,
                outcome="ok",
            )
            return result

        try:
            return await self.handler(flipper, model)
        except Exception as exc:
            self._reraise_handler_exception(exc)

    def _reraise_handler_exception(self, exc: BaseException) -> None:
        """Re-raise an exception from a handler, wrapping unknown types.

        Lets clipper's own exception hierarchy (``ActionError`` subclasses and
        ``FlipperDisconnected``) propagate untouched — they have dedicated
        MCP error handler. Any other exception (typically
        ``RuntimeError`` from a handler that saw the device reject its
        command, or ``ValueError`` from a parse failure) is wrapped in
        ``ActionRuntimeError`` so it becomes a clean tool error rather than a
        bare 500.
        """
        from clipper.flipper import FlipperError  # local import: avoid cycle

        if isinstance(exc, ActionError | FlipperError):
            raise exc
        log.warning("action %r handler raised %s: %s", self.name, type(exc).__name__, exc)
        raise ActionRuntimeError(self.name, str(exc)) from exc


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

registry: dict[str, Action] = {}


def register(action: Action) -> Action:
    """Add an Action to the registry.

    Args:
        action: The action to register.

    Returns:
        The same action (so it can be used as a decorator target or assigned).

    Raises:
        ValueError: if an action with the same name is already registered.
    """
    if action.name in registry:
        raise ValueError(f"action already registered: {action.name!r}")
    registry[action.name] = action
    log.debug("registered action %r (emissive=%s)", action.name, action.emissive)
    return action


def get(name: str) -> Action:
    """Look up an action by name.

    Raises:
        ActionNotFound: if name is not in the registry.
    """
    try:
        return registry[name]
    except KeyError as exc:
        raise ActionNotFound(f"no action registered with name {name!r}") from exc


# ---------------------------------------------------------------------------
# Built-in hardware actions — import triggers registration at startup
# ---------------------------------------------------------------------------

from clipper.hardware import (  # noqa: E402
    badusb,  # noqa: F401
    device,  # noqa: F401
    feedback,  # noqa: F401
    gpio,  # noqa: F401
    ibutton,  # noqa: F401
    ir,  # noqa: F401
    loader,  # noqa: F401
    mfkey,  # noqa: F401
    nfc,  # noqa: F401
    power,  # noqa: F401
    rfid,  # noqa: F401
    storage,  # noqa: F401
    subghz,  # noqa: F401
)
