"""Operator CLI commands, registered on the Flask app (P2-10).

With billing enabled a free account starts at 0 credits and has no in-app way to
get more; ``flask grant-credits <email> <n>`` is the minimum-viable manual grant
so an operator can top someone up (or hand out credits before the optional
``MONTHLY_FREE_CREDITS`` monthly refresh exists).

``flask statlee-doctor`` reports whether the database schema matches the models
(D5-2). It skips the boot upgrade, so it also works on a DB the upgrade cannot
handle.
"""
import click

from .extensions import db
from .models import User


def register_cli(app):
    """Attach STATlee's operator commands to ``app``."""

    @app.cli.command('grant-credits')
    @click.argument('email')
    @click.argument('amount', type=click.IntRange(min=1))
    def grant_credits(email, amount):
        """Add AMOUNT (a positive number) credits to the account with EMAIL."""
        email = (email or '').strip().lower()
        user = db.session.execute(
            db.select(User).filter_by(email=email)).scalar_one_or_none()
        if user is None:
            raise click.ClickException(f"No account found for {email!r}.")
        user.credits = (user.credits or 0) + amount
        db.session.commit()
        click.echo(
            f"Granted {amount} credits to {email}. New balance: {user.credits}.")

    @app.cli.command('statlee-doctor')
    def statlee_doctor():
        """Check that the database schema matches the models."""
        from .app import schema_report
        current, head, missing = schema_report()
        click.echo(f"Database revision: {current or '(unstamped)'}")
        click.echo(f"Migration head:    {head}")
        if missing:
            click.echo("Missing from the database: " + ", ".join(missing))
            raise click.ClickException(
                "Schema drift. Back up the database, then rebuild it (move the "
                "file aside and restart) or add the missing columns by hand.")
        if current != head:
            raise click.ClickException(
                "Database is not at head. Run `flask --app wsgi db upgrade`.")
        click.echo("Schema OK: the database matches the models at head.")

    return app
