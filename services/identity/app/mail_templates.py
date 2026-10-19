"""SeaVix email templates — one pipeline, our own copy (no Firebase
Trigger Email extension). Text + minimal HTML variants per flow."""
from __future__ import annotations

import html as _html

SIGN_OFF = "— The SeaVix team"


def otp_login_email(code: str, ttl_minutes: int, purpose: str = "sign-in") -> tuple[str, str, str]:
    subject = f"Your SeaVix {purpose} code: {code}"
    text = (
        f"Your SeaVix {purpose} code is {code}.\n\n"
        f"It expires in {ttl_minutes} minutes and works once. If you did not "
        f"request it, ignore this email.\n\n"
        f"{SIGN_OFF}\n"
    )
    body_html = (
        f"<p>Your SeaVix {purpose} code is "
        f"<strong style=\"font-size:1.4em\">{_html.escape(code)}</strong>.</p>"
        f"<p>It expires in {ttl_minutes} minutes and works once. If you did "
        "not request it, ignore this email.</p>"
        f"<p>{SIGN_OFF}</p>"
    )
    return subject, text, body_html


def email_verification_email(link: str) -> tuple[str, str, str]:
    subject = "Verify your SeaVix email address"
    text = (
        "Welcome to SeaVix. Confirm this email address by opening the link "
        f"below:\n\n{link}\n\nThe link can be used once. If you did not "
        f"create a SeaVix account, ignore this email.\n\n{SIGN_OFF}\n"
    )
    body_html = (
        "<p>Welcome to SeaVix. Confirm this email address:</p>"
        f"<p><a href=\"{_html.escape(link)}\">Verify my email address</a></p>"
        "<p>The link can be used once. If you did not create a SeaVix "
        f"account, ignore this email.</p><p>{SIGN_OFF}</p>"
    )
    return subject, text, body_html


def password_reset_email(link: str) -> tuple[str, str, str]:
    subject = "Reset your SeaVix password"
    text = (
        "We received a request to reset your SeaVix password. Open the link "
        f"below to choose a new one:\n\n{link}\n\nThe link can be used once. "
        "If you did not request a reset, ignore this email and your password "
        f"stays unchanged.\n\n{SIGN_OFF}\n"
    )
    body_html = (
        "<p>We received a request to reset your SeaVix password:</p>"
        f"<p><a href=\"{_html.escape(link)}\">Choose a new password</a></p>"
        "<p>The link can be used once. If you did not request a reset, "
        f"ignore this email.</p><p>{SIGN_OFF}</p>"
    )
    return subject, text, body_html
