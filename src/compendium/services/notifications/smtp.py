"""SMTP sender — thin wrapper around ``smtplib`` (stdlib, no new dep).

Reads non-secret SMTP settings from the site_setting registry; reads the
password from the env-only ``Settings`` (hybrid model — secrets stay env-only).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from compendium.config.settings import Settings
from compendium.services.site_settings import get_site_setting

_log = logging.getLogger("compendium.notifications.smtp")


class SMTPSender:
    def __init__(self, settings: Settings) -> None:
        # Settings is retained only for the env-only secret (smtp_password).
        # All other SMTP knobs go through get_site_setting().
        self._s = settings

    def is_configured(self) -> bool:
        return bool(get_site_setting("smtp_host") and get_site_setting("smtp_from_address"))

    def send(self, *, to: str, subject: str, body: str) -> None:
        """Send a plain-text email. Raises ``smtplib.SMTPException`` on failure."""
        if not self.is_configured():
            raise RuntimeError("SMTP is not configured")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._format_from()
        msg["To"] = to
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
        if name:
            return f"{name} <{addr}>"
        return addr or ""

    def _authenticate(self, smtp: smtplib.SMTP) -> None:
        username = get_site_setting("smtp_username")
        password = self._s.smtp_password  # env-only secret
        if username and password:
            smtp.login(username, password)
