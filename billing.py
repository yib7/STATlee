"""Monetization seam (roadmap workstream E).

A single chokepoint for the question: *"is this request allowed, and what does
it cost?"* Today it is a deliberate **no-op that always authorizes** — there is
no real billing yet. The seam exists so that:

- the priority/quality toggle has one place to ask "may I use the expensive
  model tier for this user?", and
- shipping a paid tier later is *implementing one function* (plus a Stripe
  webhook that tops up ``User.credits``), not a refactor that threads billing
  logic through every route.

``User.plan`` / ``User.credits`` (see models.py) are the storage side of this
seam. Nothing outside this module should read or write ``credits``.
"""
import logging

logger = logging.getLogger('statlee.billing')

# Credits a single priority (high-tier) request will cost once billing is real.
PRIORITY_COST = 1


def check_and_debit(user, *, priority=False, cost=None):
    """Authorize a request and (eventually) debit credits.

    Args:
        user: the ``User`` model for the caller, or ``None`` for anonymous use.
        priority: whether the request asked for the faster/higher model tier.
        cost: explicit credit cost; defaults to ``PRIORITY_COST`` for priority
            requests and ``0`` otherwise.

    Returns:
        ``(allowed: bool, message: str | None)``. ``message`` is a
        user-facing reason when ``allowed`` is ``False``.

    Today this always returns ``(True, None)``. When a paid tier ships, the
    body below becomes::

        need = cost if cost is not None else (PRIORITY_COST if priority else 0)
        if user and user.plan == 'free' and need and user.credits < need:
            return False, 'Out of credits — upgrade or wait for your monthly reset.'
        if user and need:
            user.credits -= need
            db.session.commit()
        return True, None
    """
    _ = (user, priority, cost)  # referenced so the future signature is stable
    return True, None
