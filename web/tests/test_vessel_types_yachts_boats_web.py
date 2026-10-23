"""31r: yachts and boats in the profile vessel-type picker — web level.

- /org/vessel-types/search finds the new rows by LABEL fragment
  ("yacht" -> the three yacht sizes, "boat" -> the three boat sizes).
- Save yacht_large + boat_small -> view-mode preview AND the live
  public page render the new chips; the 31f byte-equality contract
  holds through the shared partial.

Identity + web run in-process on the shared loop (web/tests/conftest.py).
"""
import re
import uuid

import httpx

from test_profile_preview_ports import (
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)


def _article(html: str) -> str:
    match = re.search(r"<article[^>]*>.*?</article>", html, re.S)
    assert match, "no <article> rendered"
    return match.group(0)


def test_yacht_boat_picker_search_by_label(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"yw-{u}@example.com"
            async with _web() as w:
                await _register_and_login(idc, w, email, f"yw-{u}")

                yachts = await w.get("/org/vessel-types/search?q=yacht")
                assert yachts.status_code == 200
                assert [v["code"] for v in yachts.json()["vessel_types"]] == [
                    "yacht_small", "yacht_medium", "yacht_large",
                ]
                assert yachts.json()["vessel_types"][0] == {
                    "code": "yacht_small", "label": "Yacht — small",
                }

                boats = await w.get("/org/vessel-types/search?q=boat")
                assert boats.status_code == 200
                assert [v["code"] for v in boats.json()["vessel_types"]] == [
                    "boat_small", "boat_medium", "boat_large",
                ]

    arun(scenario())


def test_yacht_boat_chips_render_preview_and_public(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"yc-{u}@example.com"
            slug = f"yc-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Yacht and small-craft surveys.",
                        "vessel_types": ["yacht_large", "boat_small"],
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                view = await w.get(saved.headers["location"])
                assert view.status_code == 200
                assert "sv-org-vessel-types" in view.text
                assert "Yacht — large" in view.text
                assert "Boat — small" in view.text
                # the picker seeds the persisted selection for re-edit
                # (tojson escapes the em dash as \u2014 inside x-data)
                editor = await w.get("/org/profile?view=edit")
                assert '"code": "yacht_large"' in editor.text
                assert '"label": "Yacht \\u2014 large"' in editor.text

                # live: the public page renders the same chips, and the
                # shared partial keeps preview == public byte-for-byte
                await _verify_email_and_go_live(idc, email, sid)
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert "Yacht — large" in pub.text
                assert "Boat — small" in pub.text
                blade = await w.get("/org/profile")
                assert _article(blade.text) == _article(pub.text)

    arun(scenario())
