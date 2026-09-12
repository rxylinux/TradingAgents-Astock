"""Trusted analysis cutoff propagation (C1, Codex audit R1 fix).

The model controls tool arguments (``end_date`` / ``curr_date``), which must
never widen the analysis window beyond the run's trusted date. The
branch-isolated tool node wrapper — which has graph state — opens a
call-scoped trusted cutoff (``state["trade_date"]``) around exactly one
``ToolNode.invoke``; the evidence graph tools read it and CLAMP the request
to it before the vendor is called, with a visible note in both text and
artifact. The lifecycle is a single synchronous call: no run-level mutable
state, nothing the model can influence, and direct tool invocations outside
the graph (no cutoff set) keep their original behavior.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from datetime import datetime
from typing import Iterator, Optional

_trusted_cutoff: ContextVar[Optional[str]] = ContextVar(
    "trusted_analysis_cutoff", default=None
)

_DATE_FMT = "%Y-%m-%d"


def _valid_date(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value.strip(), _DATE_FMT)
    except ValueError:
        return False
    return True


@contextlib.contextmanager
def trusted_cutoff(trade_date) -> Iterator[Optional[str]]:
    """Scope a trusted analysis date around one tool-node invocation.

    Empty/invalid dates mean "no trusted cutoff" (tools behave as before,
    e.g. direct invocations and legacy states without ``trade_date``).
    """
    value = trade_date.strip() if isinstance(trade_date, str) else ""
    token = _trusted_cutoff.set(value if _valid_date(value) else None)
    try:
        yield value if _valid_date(value) else None
    finally:
        _trusted_cutoff.reset(token)


def get_trusted_cutoff() -> Optional[str]:
    """Current trusted cutoff (``YYYY-MM-DD``) or ``None`` when unset."""
    value = _trusted_cutoff.get()
    return value if _valid_date(value) else None


def clamp_end_date(requested: str) -> tuple[str, bool]:
    """Clamp a requested end/curr date to the trusted cutoff.

    Returns ``(effective_date, clamped)``. Dates are compared strictly as
    ``YYYY-MM-DD``; anything malformed is left untouched so the vendor's
    own strict parsing rejects it downstream.
    """
    cutoff = get_trusted_cutoff()
    if cutoff is None or not _valid_date(requested):
        return requested, False
    if requested.strip() > cutoff:
        return cutoff, True
    return requested, False
