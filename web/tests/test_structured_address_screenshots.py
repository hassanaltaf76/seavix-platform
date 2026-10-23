"""31t: structured address + country dial codes — owner review shots.

Saves a full structured contact block (address lines, country, phone +
WhatsApp dial codes) through the editor Save, sets the org LIVE
in-script, and screenshots the editor's Contact details section and the
live /org/<slug> contact card. Proof lands in var/screenshots/31t-*.png.
Skips cleanly without Chrome.
"""
import re
import uuid
from pathlib import Path

import httpx
import pytest

from test_page_shell_screenshots import _shot
from test_profile_preview_ports import (
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)
from test_public_contact_details import STRUCTURED

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_structured_address_screenshot_set_31t(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"ad-{u}@example.com"
            slug = f"addr-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.",
                        "contact_email": f"ops-{u}@harbor.example",
                        **STRUCTURED,
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                await _verify_email_and_go_live(idc, email, sid)

                # 1. editor: structured address fields + dial-code comboboxes
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                assert 'name="address_line1"' in editor.text
                assert 'name="country_code"' in editor.text
                assert 'name="phone_cc"' in editor.text
                # 31u: the saved dial code pre-fills the hidden field and
                # the flag label ("🇦🇪 +971 — United Arab Emirates")
                assert re.search(r'name="phone_cc"[^>]*?value="\+971"',
                                 editor.text)
                assert "🇦🇪 +971 — United Arab Emirates" in editor.text
                assert "sv-search-select" in editor.text
                _shot(editor.text, tmp_path, "editor", 1440, 1650,
                      prefix="31t")

                # 2. live /org/<slug>: structured contact card
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert "UNITED ARAB EMIRATES" in pub.text
                assert "tel:+97145550100" in pub.text
                _shot(pub.text, tmp_path, "live", 1280, 1000, prefix="31t")

    arun(scenario())
