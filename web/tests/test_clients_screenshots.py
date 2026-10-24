"""32a: the clients owner-review set — directory table, the add-client
form (all three 31t comboboxes + the sticky Save bar), and the client
detail page. Proof lands in var/screenshots/32a-*.png; skips cleanly when
Chrome is not installed.

Identity + work + web run in-process on the shared loop; the client row
is created through the REAL pages (POST /org/clients/new), so the shots
show persisted data end to end.
"""
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from test_page_shell_screenshots import CHROME, _shot
from test_profile_preview_ports import _register_and_login, _web

FORM = {
    "name": "Harbor Shipmanagement",
    "relationship_type": "port_agent",
    "email": "",
    "contact_email": "ops@harbor.example",
    "phone_cc": "+971",
    "phone": "50 123 4567",
    "whatsapp_cc": "+971",
    "whatsapp": "50 765 4321",
    "address_line1": "Pier 7",
    "address_line2": "",
    "city": "Dubai",
    "state": "",
    "postal_code": "",
    "country_code": "AE",
}


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_clients_screenshot_set_32a(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                sid = await _register_and_login(
                    idc, w, f"shots-32a-{u}@example.com", f"shots-{u}"
                )
                saved = await w.post(
                    "/org/clients/new",
                    data={**FORM, "email": f"harbor-{u}@example.com"},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:300]

                # 1. the directory table (name, badge, email, phone, city)
                listing = await w.get("/org/clients")
                assert listing.status_code == 200
                assert 'class="sv-table"' in listing.text
                assert "Harbor Shipmanagement" in listing.text
                _shot(listing.text, tmp_path, "clients-table", 1280, 900,
                      prefix="32a")

                # 2. the add-client form — country + dial-code comboboxes
                new = await w.get("/org/clients/new")
                assert new.status_code == 200
                assert new.text.count("sv-search-select-box") == 3
                _shot(new.text, tmp_path, "client-new", 1440, 1500,
                      prefix="32a")
                # 2b. with the sticky Save bar forced visible
                _shot(new.text, tmp_path, "client-new-save-bar", 1440, 1500,
                      prefix="32a",
                      extra="<style>.sv-save-bar{display:flex !important}</style>")

                # 3. the client detail page (contact card + 32b placeholder)
                detail = await w.get(saved.headers["location"])
                assert detail.status_code == 200
                assert "Pier 7" in detail.text
                assert "task 32b" in detail.text
                _shot(detail.text, tmp_path, "client-detail", 1280, 1000,
                      prefix="32a")

    arun(scenario())
