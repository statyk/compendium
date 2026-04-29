"""SMTPSender TLS-context regression tests (H2).

`smtplib.SMTP.starttls()` and `smtplib.SMTP_SSL()` use Python's
`_create_stdlib_context` when no `context=` is supplied — that context has
`verify_mode=CERT_NONE` and `check_hostname=False`, i.e. no validation. A
network attacker can MitM the SMTP session and harvest the password.
These tests pin SMTPSender to a verifying context.
"""
from __future__ import annotations

import ssl
from unittest.mock import MagicMock, patch

import pytest

from compendium.config.settings import Settings
from compendium.services.notifications.smtp import SMTPSender


def _settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        smtp_password="hunter2",
    )


def _site_settings(**overrides):
    base = {
        "smtp_host": "mail.example.com",
        "smtp_port": 587,
        "smtp_username": "alice",
        "smtp_use_starttls": True,
        "smtp_use_ssl": False,
        "smtp_from_address": "noreply@example.com",
        "smtp_from_name": "Compendium",
    }
    base.update(overrides)
    return lambda key: base[key]


def _assert_verifying_context(ctx) -> None:
    assert isinstance(ctx, ssl.SSLContext), f"expected SSLContext, got {type(ctx)!r}"
    assert ctx.verify_mode == ssl.CERT_REQUIRED, (
        f"verify_mode is {ctx.verify_mode!r}; certificate is not validated"
    )
    assert ctx.check_hostname is True, "check_hostname is off; CN/SAN not validated"


@pytest.fixture
def fake_smtp():
    """Patch smtplib.SMTP and SMTP_SSL in the smtp module's namespace."""
    with (
        patch("compendium.services.notifications.smtp.smtplib.SMTP") as smtp,
        patch("compendium.services.notifications.smtp.smtplib.SMTP_SSL") as smtp_ssl,
    ):
        smtp.return_value.__enter__.return_value = MagicMock()
        smtp_ssl.return_value.__enter__.return_value = MagicMock()
        yield smtp, smtp_ssl


def test_starttls_passes_verifying_context(fake_smtp):
    smtp_cls, _ = fake_smtp
    with patch(
        "compendium.services.notifications.smtp.get_site_setting",
        side_effect=_site_settings(),
    ):
        SMTPSender(_settings()).send(
            to="to@example.com", subject="hi", body="b"
        )
    smtp_instance = smtp_cls.return_value.__enter__.return_value
    assert smtp_instance.starttls.call_count == 1, (
        "starttls() should run when smtp_use_starttls is true"
    )
    call = smtp_instance.starttls.call_args
    ctx = call.kwargs.get("context") or (call.args[0] if call.args else None)
    _assert_verifying_context(ctx)


def test_smtp_ssl_uses_verifying_context(fake_smtp):
    _, smtp_ssl_cls = fake_smtp
    with patch(
        "compendium.services.notifications.smtp.get_site_setting",
        side_effect=_site_settings(smtp_use_ssl=True, smtp_use_starttls=False),
    ):
        SMTPSender(_settings()).send(
            to="to@example.com", subject="hi", body="b"
        )
    assert smtp_ssl_cls.call_count == 1
    call = smtp_ssl_cls.call_args
    ctx = call.kwargs.get("context")
    if ctx is None:
        # Allow positional only as a last resort: SMTP_SSL(host, port, ..., context)
        # but our code should pass it by keyword.
        pytest.fail("SMTP_SSL was called without an explicit context= kwarg")
    _assert_verifying_context(ctx)
