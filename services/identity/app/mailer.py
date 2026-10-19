"""Mailer singleton for the identity service.

Dev: LogMailSender (stdout = the dev mailbox, in-memory outbox for tests).
Prod: SEAVIX_MAILER selects a real implementation of the same port.
"""
from __future__ import annotations

from seavix_ports import MailProvider, mailer_from_env

_mailer: MailProvider | None = None


def get_mailer() -> MailProvider:
    global _mailer
    if _mailer is None:
        _mailer = mailer_from_env()
    return _mailer


def set_mailer(mailer: MailProvider | None) -> None:
    """Test hook."""
    global _mailer
    _mailer = mailer
