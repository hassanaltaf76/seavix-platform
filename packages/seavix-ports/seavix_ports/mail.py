"""Mail pipeline — ONE provider surface for every outbound email.

OTP codes, password-reset links and email-verification links all leave the
platform through MailProvider.send (dev: LogMailSender → uniform stdout
delivery + an in-memory outbox tests can assert on). No Firebase Trigger
Email extension — SeaVix owns its templates end to end.

Prod wiring (human follow-up, documented in the task report): choose a
provider (Sendinblue/Brevo, SES, Postmark…), set SEAVIX_MAILER=smtp (or a
provider impl), supply env vars, and complete SPF/DKIM/DMARC for the
sending domain before go-live.
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
    kept in an in-memory outbox for tests. Prod: an SMTP/API implementation
    of the same protocol."""

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


def mailer_from_env() -> MailProvider:
    """SEAVIX_MAILER selects the implementation; 'log' is the dev default."""
    import os

    kind = os.environ.get("SEAVIX_MAILER", "log")
    if kind != "log":
        raise NotImplementedError(
            f"SEAVIX_MAILER={kind!r} has no implementation yet — wire the "
            "prod provider (see task report human follow-ups)"
        )
    return LogMailSender()
