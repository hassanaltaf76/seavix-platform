"""Account-access + 32b acceptance fixes: the owner-review screenshot set.

1. preview before publish (draft profile, "Publish / Go live" action)
2. successful publish — the confirm page (and the persisted-live flash)
3. the live public profile (signed out)
4. anonymous register interstitial (entry-time gate 303 target)
5. the authenticated RFQ form
6. Account → Change password card
7. change-password validation error (wrong current password)
8. successful change (flash, still signed in)

Identity + work + web run in-process; every row is created through the
REAL pages. Proof lands in var/screenshots/acc-*.png; skips cleanly
without Chrome.
"""
import uuid

import httpx
import pytest

from test_org_rfqs_32b import (
    CLIENT_FORM,
    _live_tenant,
    _register_client_org,
    _sid_from_cookie,
)
from test_page_shell_screenshots import CHROME, _shot
from test_profile_preview_ports import _register_and_login, _verify_email, _web
from webapp.auth import SESSION_COOKIE

FORCE_SAVE_BAR = "<style>.sv-save-bar{display:flex !important}</style>"


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_account_access_screenshot_set(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"acc-{u}@example.com"
            slug = f"acc-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                # a directory client + a manual RFQ so the tenant pages
                # have real rows
                saved = await w.post(
                    "/org/clients/new",
                    data={**CLIENT_FORM, "email": f"acc-c-{u}@example.com"},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:300]
                options = await w.get("/org/rfqs/clients.json")
                edge = options.json()["clients"][0]["id"]
                raised = await w.post(
                    "/org/rfqs/new",
                    data={
                        "channel": "manual_whatsapp", "client_mode": "existing",
                        "client_id": edge, "vessel_name": "MT Sea Falcon",
                        "survey_types": ["condition"],
                        "location_type": "anchorage",
                        "port_name": "Karachi anchorage",
                        "preferred_date": "2026-11-01",
                        "reference_no": f"ACC-{u}",
                        "scope": "On-hire condition survey.",
                    },
                    follow_redirects=False,
                )
                assert raised.status_code == 303, raised.text[:300]

                # 1. preview before publish (draft)
                draft = await w.post(
                    "/org/profile",
                    data={"slug": slug,
                          "about": "Attending marine surveys across the Gulf."},
                    follow_redirects=False,
                )
                assert draft.status_code == 303, draft.text[:300]
                preview = await w.get("/org/profile")
                assert "Publish / Go live" in preview.text
                _shot(preview.text, tmp_path, "preview-before-publish",
                      1280, 1250, prefix="acc")

                # 2. the publish confirm page + the persisted-live flash
                confirm = await w.get("/org/profile/publish")
                assert confirm.status_code == 200
                assert 'value="live"' in confirm.text
                _shot(confirm.text, tmp_path, "publish-confirm", 1280, 900,
                      prefix="acc")
                done = await w.post("/org/profile/publish",
                                    data={"action": "live"},
                                    follow_redirects=False)
                assert done.status_code == 303
                flash = await w.get("/org/profile?published=1")
                assert "is public now" in flash.text
                _shot(flash.text, tmp_path, "published-save", 1280, 1250,
                      prefix="acc")

            # 3. the live public profile, signed out
            async with _web() as anon:
                pub = await anon.get(f"/org/{slug}")
                assert pub.status_code == 200
                _shot(pub.text, tmp_path, "live-public-profile", 1280, 1250,
                      prefix="acc")

                # 4. anonymous register interstitial (the entry-time gate)
                gate = await anon.get(f"/org/{slug}/rfq",
                                      follow_redirects=False)
                assert gate.status_code == 303
                interstitial = await anon.get(gate.headers["location"])
                assert interstitial.status_code == 200
                assert 'value="client" selected' in interstitial.text
                _shot(interstitial.text, tmp_path, "register-interstitial",
                      1280, 1000, prefix="acc")

            async with _web() as wc:
                await _register_client_org(idc, wc, u, "accclient")
                # 5. the authenticated RFQ form
                form = await wc.get(f"/org/{slug}/rfq")
                assert form.status_code == 200
                assert 'name="vessel_name"' in form.text
                _shot(form.text, tmp_path, "rfq-form-authed", 1280, 1400,
                      prefix="acc")

            async with _web() as wa:
                await _login_web_only(idc, wa, email)
                # 6. Account → Change password
                account = await wa.get("/org/home")
                assert account.status_code == 200
                assert "Change password" in account.text
                assert 'name="current_password"' in account.text
                _shot(account.text, tmp_path, "account-change-password",
                      1280, 1250, prefix="acc")

                # 7. validation error — wrong current password
                wrong = await wa.post(
                    "/account/password",
                    data={"current_password": "not-the-password",
                          "new_password": "secret-1234",
                          "confirm_password": "secret-1234"},
                )
                assert wrong.status_code == 422
                assert "current password is incorrect" in wrong.text
                _shot(wrong.text, tmp_path, "change-password-error", 1280,
                      1250, prefix="acc")

                # 8. successful change — flash + still signed in
                ok = await wa.post(
                    "/account/password",
                    data={"current_password": "secret-123",
                          "new_password": "secret-1234",
                          "confirm_password": "secret-1234"},
                    follow_redirects=False,
                )
                assert ok.status_code == 303, ok.text[:300]
                assert ok.headers["location"] == "/org/home?password=changed"
                changed = await wa.get("/org/home?password=changed")
                assert "Password changed" in changed.text
                _shot(changed.text, tmp_path, "change-password-success",
                      1280, 1250, prefix="acc")
                still = await wa.get("/org/quotes")
                assert still.status_code == 200

    arun(scenario())


async def _login_web_only(idc, w, email: str) -> None:
    """Login through the real web pages for an already-registered user."""
    from test_org_rfqs_32b import _login_web
    await _login_web(idc, w, email)
