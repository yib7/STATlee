"""The one place STATlee hands an email to SMTP.

``smtplib.SMTP.starttls()`` without a context negotiates TLS but does not
verify the server's certificate, so anyone on the path to ``SMTP_HOST`` could
pose as the server and collect ``SMTP_PASSWORD`` and the reset and
verification links. Every send goes through ``send`` instead, which upgrades
with a verifying context before logging in.
"""
import smtplib
import ssl


def send(cfg, msg):
    """Send ``msg`` through ``cfg.smtp_host`` over verified STARTTLS."""
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
        server.starttls(context=ssl.create_default_context())
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)
