"""Mail pipeline — ONE provider surface for every outbound email.

OTP codes, password-reset links and email-verification links all leave the
platform through MailProvider.send. Two implementations:

- LogMailSender (DEFAULT, no env): stdout/container logs + in-memory
  outbox; the entire automated suite runs against it (hermetic).
- SmtpMailSender (dev-only manual QA, active ONLY when DEV_SMTP_HOST is
  set — default OFF): plain smtplib + STARTTLS. From-address must equal
  the SMTP account address (Outlook rejects other domains) — asserted in
  from_env() config validation.

Prod wiring (human follow-up, in the task report): SendGrid/SES + a real
sending domain with SPF/DKIM/DMARC on seavix.ai. No Firebase Trigger
Email extension — SeaVix owns its templates end to end.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger("seavix.ports.mail")


@dataclass(frozen=True)
class MailMessage:
    to: str
    subject: str
    body_text: str
    body_html: str | None = None


class MailProvider(Protocol):
    async def send(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None: ...


@dataclass
class LogMailSender:
    """Dev implementation: every message is logged (the dev 'mailbox') and
    kept in an in-memory outbox for tests. DEFAULT — no env needed; the
    whole automated suite runs against this (hermetic, no network)."""

    outbox: list[MailMessage] = field(default_factory=list)

    async def send(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None:
        message = MailMessage(to=to, subject=subject, body_text=body_text,
                              body_html=body_html)
        self.outbox.append(message)
        logger.info(
            "MAIL to=%s subject=%s\n----\n%s\n----",
            to, subject, body_text.strip(),
        )


@dataclass
class SmtpMailSender:
    """DEV-ONLY manual-QA sender. Active ONLY when DEV_SMTP_HOST is set
    (default OFF); flip it on, eyeball a template in a real inbox, flip it
    off. Plain smtplib + STARTTLS. NEVER enable in CI/tests.

    Outlook constraint: for Outlook-direct hosts the From address must
    equal the SMTP account address (the provider rejects other domains) —
    from_env() raises for that case. Relay setups (Brevo et al.) may
    legitimately send From a different, provider-verified address; those
    produce a warning, not an error.
    """

    host: str
    port: int = 587
    user: str = ""
    password: str = ""
    from_addr: str = ""

    @classmethod
    def from_env(cls) -> "SmtpMailSender":
        import os

        host = os.environ["DEV_SMTP_HOST"]
        port = int(os.environ.get("DEV_SMTP_PORT") or "587")
        user = os.environ.get("DEV_SMTP_USER", "")
        password = os.environ.get("DEV_SMTP_PASSWORD", "")
        from_addr = os.environ.get("DEV_SMTP_FROM", "") or user
        if not user or not password:
            raise ValueError(
                "DEV_SMTP_USER and DEV_SMTP_PASSWORD are required when "
                "DEV_SMTP_HOST is set"
            )
        outlook_hosts = ("smtp-mail.outlook.com", "outlook.office365.com")
        if host.lower() in outlook_hosts and from_addr != user:
            # Outlook rejects mail sent as any domain other than the account's.
            raise ValueError(
                "DEV_SMTP_FROM must equal DEV_SMTP_USER for Outlook-direct "
                "SMTP — Outlook rejects sending as another domain "
                f"(user={user!r}, from={from_addr!r})"
            )
        if from_addr != user:
            # Relay setups (e.g. Brevo SMTP with a verified sender address)
            # legitimately send From an address other than the relay login;
            # the provider enforces sender verification on its side.
            logger.warning(
                "DEV_SMTP_FROM (%s) differs from DEV_SMTP_USER (%s) — only "
                "valid if the relay authorizes this sender",
                from_addr, user,
            )
        return cls(host=host, port=port, user=user, password=password,
                   from_addr=from_addr)

    async def send(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None:
        import asyncio
        import smtplib
        from email.message import EmailMessage

        def _deliver() -> None:
            message = EmailMessage()
            message["From"] = self.from_addr
            message["To"] = to
            message["Subject"] = subject
            message.set_content(body_text)
            if body_html:
                message.add_alternative(body_html, subtype="html")
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.send_message(message)

        await asyncio.to_thread(_deliver)


def mailer_from_env() -> MailProvider:
    """Selection rule: SmtpMailSender ONLY when DEV_SMTP_HOST is set;
    LogMailSender otherwise (the default, no env needed)."""
    import os

    if os.environ.get("DEV_SMTP_HOST"):
        return SmtpMailSender.from_env()
    return LogMailSender()
