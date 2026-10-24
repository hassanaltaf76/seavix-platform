"""31q: the three-shot owner-review set — profile editor refinements.

- editor (full-width form, THREE chip pickers seeded with persisted
  chips — services, vessel types, ports — and no preview aside)
- view-mode preview (Services + Vessel types we serve chip sections)
- the live /org/<slug> public page (test org set LIVE in-script)

Screenshot proof lands in var/screenshots/31q-*.png. Skips cleanly when
Chrome is not installed.
"""
import uuid
from pathlib import Path

import httpx
import pytest

from test_page_shell_screenshots import _shot
from test_profile_preview_ports import (
    _pin_ports,
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_profile_services_screenshot_set_31q(arun, id_asgi, tmp_path):
    async def scenario():
        await _pin_ports()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"sq-{u}@example.com"
            slug = f"svc-shot-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.\n"
                                 "Serving the Gulf and beyond.",
                        "services": ["draft_survey", "condition_survey",
                                     "bunker_quantity_survey"],
                        "vessel_types": ["oil_tanker", "bulk_carrier"],
                        "ports": ["AEFJR", "NLRTM"],
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                # set the org LIVE in-script — the public page must answer
                await _verify_email_and_go_live(idc, email, sid)

                # 1. editor: full-width form, four pickers with chips,
                #    NO preview aside (32d: + survey types)
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                assert editor.text.count('class="sv-chip-picker"') == 4
                assert editor.text.count('class="sv-chip"') == 4
                assert "sv-editor-preview" not in editor.text
                _shot(editor.text, tmp_path, "editor", 1440, 1500, prefix="31q")

                # 2. view-mode preview: both chip sections + ports
                view = await w.get("/org/profile")
                assert view.status_code == 200
                assert "sv-org-services" in view.text
                assert "sv-org-vessel-types" in view.text
                assert "sv-org-ports" in view.text
                _shot(view.text, tmp_path, "preview", 1280, 1200, prefix="31q")

                # 3. live public page
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert "sv-org-services" in pub.text
                assert "sv-org-vessel-types" in pub.text
                _shot(pub.text, tmp_path, "public", 1280, 1200, prefix="31q")

    arun(scenario())
