"""The single "logged-in User, or None" primitive (P2-5).

Deliberately dependency-free (no imports from other ``statlee`` modules) so
it can be imported from anywhere — routes, ``storage.py``, ``extensions.py``
— without any risk of a circular import.
"""


def current_user_or_none():
    """The logged-in User model, or None for anonymous callers.

    Lazy import + getattr guard so it is safe when the login manager isn't
    initialised (scripts/tests).
    """
    try:
        from flask_login import current_user
    except Exception:
        return None
    if current_user and getattr(current_user, 'is_authenticated', False):
        return current_user
    return None
