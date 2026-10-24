"""32b: the owner-review screenshot set — six shots covering both
channels end to end (updated by the 24 Oct entry-time gate decision):

1. the register interstitial — the anonymous entry now 303s straight to
   ?next= /register, so this IS what a visitor sees on Raise an RFQ
2. the authenticated RFQ form (new field set: vessel, port, reference)
3. the client rail (RFQs section only, own request with status badge)
4. the tenant RFQ list (public + manual rows, channel/status badges)
5. the manual Raise form (channel radios + client picker)
6. the RFQ detail (fields, client contact card, Quotes placeholder)

Identity + work + web run in-process; every row is created through the
REAL pages. Proof lands in var/screenshots/32b-*.png; skips cleanly
without Chrome.
"""
import uuid
from pathlib import Path

import httpx
import pytest

from webapp.auth import SESSION_COOKIE

from test_page_shell_screenshots import CHROME, _shot
from test_profile_preview_ports import _web
from test_org_rfqs_32b import (
    CLIENT_FORM,
    FORM,
    _live_tenant,
    _register_client_org,
)

FORCE_SAVE_BAR = "<style>.sv-save-bar{display:flex !important}</style>"


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_rfq_screenshot_set_32b(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as wt:
                tenant_email, slug = await _live_tenant(
                    idc, wt, u,
                    about=("Attending marine surveys across the Gulf and "
                           "the Indian Ocean — pre-purchase, condition, "
                           "bunker and cargo work by certified surveyors."))

                # tenant: directory client + a manual RFQ
                w = wt
                saved = await w.post(
                    "/org/clients/new",
                    data={**CLIENT_FORM, "email": f"shot-{u}@example.com"},
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
                        "vessel_imo": "9404572",
                        "survey_types": ["condition", "bunker_quantity"],
                        "location_type": "anchorage",
                        "port_name": "Karachi anchorage",
                        "preferred_date": "2026-11-01",
                        "reference_no": "PMS-QT-38-2026",
                        "scope": "On-hire condition survey + bunker figures.",
                    },
                    follow_redirects=False,
                )
                assert raised.status_code == 303, raised.text[:400]
                manual_url = raised.headers["location"]

            # 1+2 render signed-OUT / signed-IN chrome — the entry-time
            # gate 303s an anonymous Raise click to ?next=/register; the
            # form itself renders for the signed-in client.
            async with _web() as anon:
                gate = await anon.get(f"/org/{slug}/rfq",
                                      follow_redirects=False)
                assert gate.status_code == 303
                assert gate.headers["location"] == f"/register?next=/org/{slug}/rfq"

                # 1. the register interstitial
                interstitial = await anon.get(gate.headers["location"])
                assert interstitial.status_code == 200
                assert 'value="client" selected' in interstitial.text
                _shot(interstitial.text, tmp_path, "register-interstitial",
                      1280, 1000, prefix="32b")

            async with _web() as wf:
                await _register_client_org(idc, wf, u, "shotform")
                # 2. the RFQ form, signed in
                public = await wf.get(f"/org/{slug}/rfq")
                assert public.status_code == 200
                assert 'name="vessel_name"' in public.text
                _shot(public.text, tmp_path, "rfq-form", 1280, 1400,
                      prefix="32b")

            # client submits through the public funnel
            async with _web() as wc:
                await _register_client_org(idc, wc, u, "shot")
                from webapp import pages
                pages.limiter._hits.clear()
                thanks = await wc.post(
                    f"/org/{slug}/rfq",
                    data={**FORM, "reference_no": f"SEA-{u}",
                          "vessel_name": "MV Ocean Pearl"},
                )
                assert "Track your requests" in thanks.text

                # 3. the client rail — RFQs section only
                mine = await wc.get("/org/rfqs")
                assert mine.status_code == 200
                assert f"SEA-{u}" in mine.text
                _shot(mine.text, tmp_path, "client-rail", 1280, 800,
                      prefix="32b")

            async with _web() as w:
                w.cookies.set(SESSION_COOKIE,
                              (await _tenant_session(idc, tenant_email)))
                listing = await w.get("/org/rfqs")
                assert "PMS-QT-38-2026" in listing.text
                assert f"SEA-{u}" in listing.text
                assert "WhatsApp" in listing.text
                assert "Public page" in listing.text
                _shot(listing.text, tmp_path, "tenant-rfqs", 1280, 800,
                      prefix="32b")

                # 5. the manual raise form (Save bar forced visible)
                form = await w.get("/org/rfqs/new")
                assert form.status_code == 200
                assert "rfqClientPicker" in form.text
                _shot(form.text, tmp_path, "rfq-raise", 1280, 1500,
                      extra=FORCE_SAVE_BAR, prefix="32b")

                # 6. the RFQ detail
                detail = await w.get(manual_url)
                assert detail.status_code == 200
                assert "Capt. Amin Dar" in detail.text
                _shot(detail.text, tmp_path, "rfq-detail", 1280, 1200,
                      prefix="32b")

    arun(scenario())


async def _tenant_session(idc, email: str) -> str:
    start = await idc.post("/api/auth/login/start",
                           json={"email": email, "password": "secret-123"})
    code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
    done = await idc.post("/api/auth/login/verify",
                          json={"email": email, "code": code})
    return done.json()["session_id"]
