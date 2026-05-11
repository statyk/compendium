"""SMTP sender — thin wrapper around ``smtplib`` (stdlib, no new dep).

All SMTP configuration, including the password, is read via ``get_site_setting``
so it can be set either through the environment (COMPENDIUM_SMTP_PASSWORD) or
the admin UI (stored encrypted at rest).
"""

from __future__ import annotations

import email.policy
import logging
import smtplib
import ssl
from email.message import EmailMessage

from compendium.services.site_settings import get_site_setting

_log = logging.getLogger("compendium.notifications.smtp")


def _validate_header(value: str, name: str) -> str:
    """Reject CR/LF in header values to prevent email header injection."""
    if "\r" in value or "\n" in value:
        raise ValueError(f"Email header '{name}' must not contain CR or LF characters")
    return value


class SMTPSender:
    def __init__(self, _settings=None) -> None:
        pass

    def is_configured(self) -> bool:
        return bool(get_site_setting("smtp_host") and get_site_setting("smtp_from_address"))

    def send(self, *, to: str, subject: str, body: str) -> None:
        """Send a plain-text email. Raises ``smtplib.SMTPException`` on failure."""
        if not self.is_configured():
            raise RuntimeError("SMTP is not configured")

        msg = EmailMessage(policy=email.policy.default)
        msg["Subject"] = _validate_header(subject, "Subject")
        msg["From"] = _validate_header(self._format_from(), "From")
        msg["To"] = _validate_header(to, "To")
        msg.set_content(body)

        host = get_site_setting("smtp_host")
        port = get_site_setting("smtp_port")
        # Without an explicit context, smtplib falls back to a stdlib context
        # with verify_mode=CERT_NONE / check_hostname=False — i.e. no TLS
        # validation. Hand it a verifying context so MitM is detected.
        tls_ctx = ssl.create_default_context()

        if get_site_setting("smtp_use_ssl"):
            with smtplib.SMTP_SSL(host, port, timeout=30, context=tls_ctx) as smtp:
                self._authenticate(smtp)
                smtp.send_message(msg)
            return

        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            if get_site_setting("smtp_use_starttls"):
                smtp.starttls(context=tls_ctx)
                smtp.ehlo()
            self._authenticate(smtp)
            smtp.send_message(msg)

    def _format_from(self) -> str:
        addr = get_site_setting("smtp_from_address")
        name = get_site_setting("smtp_from_name")
        if addr:
            _validate_header(addr, "smtp_from_address")
        if name:
            _validate_header(name, "smtp_from_name")
            return f"{name} <{addr}>"
        return addr or ""

    def _authenticate(self, smtp: smtplib.SMTP) -> None:
        username = get_site_setting("smtp_username")
        password = get_site_setting("smtp_password")
        if username and password:
            smtp.login(username, password)
