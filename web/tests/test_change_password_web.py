"""32b FEATURE 3 (owner decision 24 Oct): Account → Change password in the
web app — the "Your account" page (/org/home) gains the Change password
card and POST /account/password proxies to identity's
/api/auth/change-password (the signed-in session is the reauthentication;
no OTP, no email).

Covered through the whole web app + identity in-process stack:
- the card + the three fields render for the signed-in user; anonymous
  visitors are redirected to /login (GET and POST)
- wrong current password re-renders the account page with the inline
  error (no redirect)
- mismatched confirm re-renders with the inline error
- success: 303 to /org/home?password=changed, the flash renders, and
  this browser's session cookie still works on the next request
"""
import uuid

import httpx
import pytest

from webapp.auth import SESSION_COOKIE

from test_profile_preview_ports import (
    _register_and_login,
    _web,
)

OLD = "secret-123"
NEW = "brand-new-secret-9"


def test_change_password_card_authenticated_only(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"wc-{uuid.uuid4().hex[:8]}@example.com"
            async with _web() as w:
                await _register_and_login(idc, w, email, "unused")

                home = await w.get("/org/home")
                assert home.status_code == 200
                assert 'id="change-password"' in home.text
                assert "Change password" in home.text
                assert 'action="/account/password"' in home.text
                assert 'name="current_password"' in home.text
                assert 'name="new_password"' in home.text
                assert 'name="confirm_password"' in home.text

    arun(scenario())


def test_change_password_anonymous_redirected_to_login(arun, id_asgi):
    async def scenario():
        async with _web() as w:
            get = await w.get("/org/home", follow_redirects=False)
            assert get.status_code == 303
            assert get.headers["location"] == "/login"
            post = await w.post(
                "/account/password",
                data={"current_password": OLD, "new_password": NEW,
                      "confirm_password": NEW},
                follow_redirects=False,
            )
            assert post.status_code == 303
            assert post.headers["location"] == "/login"

    arun(scenario())


async def _post_change(w, **fields):
    data = {"current_password": OLD, "new_password": NEW,
            "confirm_password": NEW}
    data.update(fields)
    return await w.post("/account/password", data=data,
                        follow_redirects=False)


def test_wrong_current_password_renders_inline_error(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"wc-{uuid.uuid4().hex[:8]}@example.com"
            async with _web() as w:
                await _register_and_login(idc, w, email, "unused")

                resp = await _post_change(w, current_password="not-it-9")
                assert resp.status_code == 422, resp.text[:300]
                assert "current password is incorrect" in resp.text
                # re-rendered inline, NOT redirected with a flash
                assert "notice success" not in resp.text
                assert 'id="change-password"' in resp.text

                # a fresh GET shows no stale error
                home = await w.get("/org/home")
                assert "current password is incorrect" not in home.text

    arun(scenario())


def test_mismatched_confirm_renders_inline_error(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"wc-{uuid.uuid4().hex[:8]}@example.com"
            async with _web() as w:
                await _register_and_login(idc, w, email, "unused")

                resp = await _post_change(w, confirm_password="different-9")
                assert resp.status_code == 422, resp.text[:300]
                assert "passwords do not match" in resp.text
                assert "notice success" not in resp.text

    arun(scenario())


def test_success_redirects_flash_and_session_survives(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"wc-{uuid.uuid4().hex[:8]}@example.com"
            async with _web() as w:
                await _register_and_login(idc, w, email, "unused")

                resp = await _post_change(w)
                assert resp.status_code == 303, resp.text[:300]
                assert resp.headers["location"] == "/org/home?password=changed"

                # the flash renders on the redirected page
                home = await w.get("/org/home?password=changed")
                assert home.status_code == 200
                assert "Password changed" in home.text

                # this browser's session survived the rotation
                dash = await w.get("/dashboard")
                assert dash.status_code == 200

    arun(scenario())
