"""Operator CLI commands, registered on the Flask app (P2-10).

With billing enabled a free account starts at 0 credits and has no in-app way to
get more; ``flask grant-credits <email> <n>`` is the minimum-viable manual grant
so an operator can top someone up (or hand out credits before the optional
``MONTHLY_FREE_CREDITS`` monthly refresh exists).
"""
import click

from .extensions import db
from .models import User


def register_cli(app):
    """Attach STATlee's operator commands to ``app``."""

    @app.cli.command('grant-credits')
    @click.argument('email')
    @click.argument('amount', type=int)
    def grant_credits(email, amount):
        """Add AMOUNT credits to the account with EMAIL and commit."""
        email = (email or '').strip().lower()
        user = db.session.execute(
            db.select(User).filter_by(email=email)).scalar_one_or_none()
        if user is None:
            raise click.ClickException(f"No account found for {email!r}.")
        user.credits = (user.credits or 0) + amount
        db.session.commit()
        click.echo(
            f"Granted {amount} credits to {email}. New balance: {user.credits}.")

    return app
