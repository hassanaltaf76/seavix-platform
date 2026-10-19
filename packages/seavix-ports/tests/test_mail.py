"""Mail pipeline tests.

Default path: LogMailSender, selected with NO env set — the whole
automated suite is hermetic. The live SmtpMailSender test is optional: it
runs only when DEV_SMTP_HOST is present (manual QA), and skips otherwise.
"""
import os

import pytest

from seavix_ports import (
    LogMailSender,
    MailMessage,
    SmtpMailSender,
    mailer_from_env,
)

SMTP_ENV_VARS = (
    "DEV_SMTP_HOST",
    "DEV_SMTP_PORT",
    "DEV_SMTP_USER",
    "DEV_SMTP_PASSWORD",
    "DEV_SMTP_FROM",
)


@pytest.fixture()
def no_smtp_env(monkeypatch):
    for var in SMTP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_default_mailer_is_log_sender_with_no_env(no_smtp_env):
    mailer = mailer_from_env()
    assert isinstance(mailer, LogMailSender)


def test_log_sender_outbox_and_delivery(no_smtp_env):
    async def scenario():
        mailer = LogMailSender()
        await mailer.send("a@b.c", "Subject", "hello\n")
        await mailer.send("a@b.c", "HTML", "text", "<p>html</p>")
        assert mailer.outbox == [
            MailMessage(to="a@b.c", subject="Subject", body_text="hello\n",
                        body_html=None),
            MailMessage(to="a@b.c", subject="HTML", body_text="text",
                        body_html="<p>html</p>"),
        ]

    import asyncio

    asyncio.run(scenario())


def test_smtp_selected_only_when_host_set(monkeypatch):
    monkeypatch.setenv("DEV_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("DEV_SMTP_USER", "someone@example.test")
    monkeypatch.setenv("DEV_SMTP_PASSWORD", "app-password")
    mailer = mailer_from_env()
    assert isinstance(mailer, SmtpMailSender)
    assert mailer.host == "smtp.example.test"
    assert mailer.port == 587  # default
    assert mailer.from_addr == "someone@example.test"  # defaults to user


def test_smtp_from_must_equal_user_for_outlook(monkeypatch):
    monkeypatch.setenv("DEV_SMTP_HOST", "smtp-mail.outlook.com")
    monkeypatch.setenv("DEV_SMTP_USER", "account@example.test")
    monkeypatch.setenv("DEV_SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("DEV_SMTP_FROM", "other@elsewhere.test")
    with pytest.raises(ValueError, match="must equal DEV_SMTP_USER"):
        SmtpMailSender.from_env()


def test_smtp_from_may_differ_on_relay_hosts(monkeypatch, caplog):
    """Relay setups (Brevo et al.) legitimately send From a provider-
    verified address that differs from the relay login — warning, not error.
    """
    import logging

    monkeypatch.setenv("DEV_SMTP_HOST", "smtp-brevo.com")
    monkeypatch.setenv("DEV_SMTP_USER", "login@smtp-brevo.com")
    monkeypatch.setenv("DEV_SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("DEV_SMTP_FROM", "hassanaltaf@hotmail.com")
    with caplog.at_level(logging.WARNING, logger="seavix.ports.mail"):
        mailer = SmtpMailSender.from_env()
    assert mailer.from_addr == "hassanaltaf@hotmail.com"
    assert any("differs from DEV_SMTP_USER" in r.message for r in caplog.records)


def test_smtp_requires_credentials(monkeypatch):
    monkeypatch.setenv("DEV_SMTP_HOST", "smtp.example.test")
    monkeypatch.delenv("DEV_SMTP_USER", raising=False)
    monkeypatch.delenv("DEV_SMTP_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="required when DEV_SMTP_HOST"):
        SmtpMailSender.from_env()


def test_smtp_explicit_port(monkeypatch):
    monkeypatch.setenv("DEV_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("DEV_SMTP_PORT", "2525")
    monkeypatch.setenv("DEV_SMTP_USER", "account@example.test")
    monkeypatch.setenv("DEV_SMTP_PASSWORD", "app-password")
    mailer = SmtpMailSender.from_env()
    assert mailer.port == 2525


@pytest.mark.smtp
@pytest.mark.skipif(
    not os.environ.get("DEV_SMTP_HOST"),
    reason="DEV_SMTP_HOST not set — manual-QA only; the default suite "
           "runs hermetic on LogMailSender",
)
def test_smtp_live_send_manual_qa():
    """Send a real email through DEV_SMTP_* so a human can eyeball the
    branded template in a real inbox. Never part of CI."""
    import asyncio

    async def scenario():
        mailer = SmtpMailSender.from_env()
        to = os.environ.get("DEV_SMTP_TO", mailer.from_addr)
        await mailer.send(
            to,
            "SeaVix SMTP QA — SeaVix mail pipeline",
            "This is the SeaVix mail pipeline speaking.\n\n"
            "If you can read this in your inbox, SmtpMailSender works.\n",
            "<p>This is the <strong>SeaVix</strong> mail pipeline speaking.</p>"
            "<p>If you can read this in your inbox, SmtpMailSender works.</p>",
        )

    asyncio.get_event_loop().run_until_complete(scenario())
