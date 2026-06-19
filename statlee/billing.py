"""Monetization seam (roadmap workstream E).

A single chokepoint for the question: *"is this request allowed, and what does
it cost?"* It stays a **no-op that always authorizes** until ``billing_enabled``
is turned on, so wiring (the priority toggle) can depend on one stable call.

When enabled it does two things:
- enforces a **global monthly ceiling** on priority (high-tier) requests, so the
  operator's own API key can't be run up without bound, and
- debits ``User.credits`` for a logged-in free-plan user's priority requests.

``User.plan`` / ``User.credits`` (see models.py) are the storage side of this
seam. Nothing outside this module should read or write ``credits``.
"""
import logging
import threading
from datetime import UTC, datetime

logger = logging.getLogger('statlee.billing')

# Credits a single priority (high-tier) request will cost once billing is real.
PRIORITY_COST = 1

# Global, per-process monthly counter for the spend ceiling. Across multiple
# workers each enforces its own copy (a shared store would be needed for an
# exact global cap); it is a coarse guardrail, not an accounting ledger.
_lock = threading.Lock()
_spend = {'month': None, 'priority_calls': 0}


def reset_spend():
    """Test hook: clear the in-process monthly counter."""
    with _lock:
        _spend['month'] = None
        _spend['priority_calls'] = 0


def _within_monthly_ceiling(ceiling):
    """Consume one unit against the current month's ceiling. Returns False (and
    consumes nothing) once the ceiling is reached. ``ceiling<=0`` disables it."""
    if not ceiling or ceiling <= 0:
        return True
    month = datetime.now(UTC).strftime('%Y-%m')
    with _lock:
        if _spend['month'] != month:
            _spend['month'] = month
            _spend['priority_calls'] = 0
        if _spend['priority_calls'] >= ceiling:
            return False
        _spend['priority_calls'] += 1
        return True


def check_and_debit(user, *, priority=False, cost=None, config=None):
    """Authorize a request and (when billing is enabled) debit credits.

    Args:
        user: the ``User`` model for the caller, or ``None`` for anonymous use.
        priority: whether the request asked for the faster/higher model tier.
        cost: explicit credit cost; defaults to ``PRIORITY_COST`` for priority
            requests and ``0`` otherwise.
        config: the active ``Config``. When omitted or ``billing_enabled`` is
            False, this is a no-op that always authorizes (the default).

    Returns:
        ``(allowed: bool, message: str | None)`` — ``message`` is a user-facing
        reason when ``allowed`` is ``False``.
    """
    if config is None or not getattr(config, 'billing_enabled', False):
        return True, None

    need = cost if cost is not None else (PRIORITY_COST if priority else 0)

    # Operator-protecting global ceiling (applies to everyone, incl. anonymous).
    if priority and not _within_monthly_ceiling(
            getattr(config, 'monthly_priority_call_ceiling', 0)):
        logger.warning("Monthly priority ceiling reached; denying priority request.")
        return False, ('The service has hit its monthly priority-usage ceiling. '
                       'Try again later, or run without priority.')

    # Per-account credit debit for logged-in free-plan users.
    if user is not None and getattr(user, 'plan', 'free') == 'free' and need:
        if getattr(user, 'credits', 0) < need:
            return False, ('Out of credits — upgrade or wait for your monthly '
                           'reset.')
        user.credits -= need
        from .extensions import db
        db.session.commit()
    return True, None
