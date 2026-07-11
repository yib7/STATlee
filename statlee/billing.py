"""Monetization seam (roadmap workstream E).

A single chokepoint for the question: *"is this request allowed, and what does
it cost?"* It stays a **no-op that always authorizes** until ``billing_enabled``
is turned on, so wiring (Pro mode) can depend on one stable call.

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


def _return_ceiling_unit():
    """Give back one unit consumed by ``_within_monthly_ceiling``. Used when
    the request is refused after the unit was taken (the credit debit was
    denied), so out-of-credit retries cannot drain the operator-wide ceiling
    and lock paying users out of priority requests (P2-1)."""
    with _lock:
        if _spend['priority_calls'] > 0:
            _spend['priority_calls'] -= 1


def _apply_monthly_free_credits(user, config):
    """Lazily top a free user's balance up to ``MONTHLY_FREE_CREDITS`` on the
    first billed request of a new calendar month (P2-10).

    Semantics: a *set-to-at-least-N* on month rollover, not an additive grant.
    ``credits_month`` records the last month already topped up so the top-up
    fires exactly once per month; the update is a single conditional statement
    (``WHERE credits_month IS NULL OR credits_month != <month>``) so two
    concurrent first-of-month requests can't both apply it.
    """
    free = getattr(config, 'monthly_free_credits', 0) or 0
    if free <= 0:
        return
    if user is None or getattr(user, 'plan', 'free') != 'free':
        return
    month = datetime.now(UTC).strftime('%Y-%m')
    if getattr(user, 'credits_month', None) == month:
        return
    from sqlalchemy import case, or_

    from .extensions import db
    from .models import User
    rows = db.session.execute(
        db.update(User)
        .where(User.id == user.id,
               or_(User.credits_month.is_(None), User.credits_month != month))
        .values(
            credits=case((User.credits < free, free), else_=User.credits),
            credits_month=month)).rowcount
    db.session.commit()
    if rows:
        # Bulk UPDATE does not sync onto the ORM instance; refresh so the debit
        # below (and any downstream read) sees the topped-up balance.
        db.session.refresh(user)


def refund(user, *, priority=False, cost=None, config=None):
    """Reverse a debit made by ``check_and_debit`` (e.g. the request failed
    after the credit was taken, so the user got nothing for it).

    A no-op with the same gating as ``check_and_debit``: only credits a
    logged-in free-plan user, only when billing is enabled and a non-zero cost
    was actually debited. The per-process monthly priority ceiling is a coarse
    guardrail and is deliberately NOT rolled back here — over-counting it fails
    safe (toward the operator's protection), and it self-resets each month.
    """
    if config is None or not getattr(config, 'billing_enabled', False):
        return
    need = cost if cost is not None else (PRIORITY_COST if priority else 0)
    if user is not None and getattr(user, 'plan', 'free') == 'free' and need:
        from .extensions import db
        from .models import User
        db.session.execute(
            db.update(User)
            .where(User.id == user.id)
            .values(credits=User.credits + need))
        db.session.commit()
        db.session.refresh(user)


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

    # P2-10: refresh this user's monthly free credits before deciding, so the
    # first billed request of a new month sees the topped-up balance.
    _apply_monthly_free_credits(user, config)

    need = cost if cost is not None else (PRIORITY_COST if priority else 0)

    # Operator-protecting global ceiling (applies to everyone, incl. anonymous).
    ceiling = getattr(config, 'monthly_priority_call_ceiling', 0)
    if priority and not _within_monthly_ceiling(ceiling):
        logger.warning("Monthly priority ceiling reached; denying priority request.")
        return False, ('The service has hit its monthly priority-usage ceiling. '
                       'Try again later, or run without priority.')
    ceiling_unit_taken = bool(priority and ceiling and ceiling > 0)

    # Per-account credit debit for logged-in free-plan users. This is a single
    # atomic conditional UPDATE (check-and-debit in one statement) rather than
    # a read-then-write: two concurrent requests racing this function can't
    # both pass a Python-level `if credits < need` check and then both debit,
    # which would double-spend or drive credits negative. The `credits >= need`
    # clause in the WHERE makes the row only update when there's still enough
    # balance, so whichever request's UPDATE commits first wins and the other
    # necessarily sees rowcount == 0.
    if user is not None and getattr(user, 'plan', 'free') == 'free' and need:
        from .extensions import db
        from .models import User
        rows = db.session.execute(
            db.update(User)
            .where(User.id == user.id, User.credits >= need)
            .values(credits=User.credits - need)).rowcount
        db.session.commit()
        if not rows:
            # The debit was refused, so the request does no priority work:
            # give the ceiling unit back (P2-1). Out-of-credit retries must
            # not erode the operator-wide quota for everyone else.
            if ceiling_unit_taken:
                _return_ceiling_unit()
            # Only promise a monthly reset when one actually exists (P2-10): a
            # MONTHLY_FREE_CREDITS top-up is what refreshes the balance. With it
            # off, saying "wait for your reset" would be a lie.
            if getattr(config, 'monthly_free_credits', 0) > 0:
                return False, ('Out of credits. Your free credits refresh at '
                               'the start of next month.')
            return False, ('Out of credits. Contact the site operator to add '
                           'more credits.')
        # The UPDATE above is a bulk/Core statement — SQLAlchemy does not sync
        # it back onto the in-memory ORM instance, so `user.credits` is stale
        # here. Refresh it so any downstream read (this request's response
        # payload, /check_auth, etc.) sees the real post-debit balance.
        db.session.refresh(user)
    return True, None
