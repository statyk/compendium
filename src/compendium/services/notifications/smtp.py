"""SMTP sender — thin wrapper around ``smtplib`` (stdlib, no new dep)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from compendium.config.settings import Settings

_log = logging.getLogger("compendium.notifications.smtp")


class SMTPSender:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def is_configured(self) -> bool:
        return bool(self._s.smtp_host and self._s.smtp_from_address)

    def send(self, *, to: str, subject: str, body: str) -> None:
        """Send a plain-text email. Raises ``smtplib.SMTPException`` on failure."""
        if not self.is_configured():
            raise RuntimeError("SMTP is not configured")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._format_from()
        msg["To"] = to
        msg.set_content(body)

        host = self._s.smtp_host
        port = self._s.smtp_port

        if self._s.smtp_use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                self._authenticate(smtp)
                smtp.send_message(msg)
            return

        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            if self._s.smtp_use_starttls:
                smtp.starttls()
                smtp.ehlo()
            self._authenticate(smtp)
            smtp.send_message(msg)

    def _format_from(self) -> str:
        addr = self._s.smtp_from_address
        name = self._s.smtp_from_name
        if name:
            return f"{name} <{addr}>"
        return addr or ""

    def _authenticate(self, smtp: smtplib.SMTP) -> None:
        if self._s.smtp_username and self._s.smtp_password:
            smtp.login(self._s.smtp_username, self._s.smtp_password)
