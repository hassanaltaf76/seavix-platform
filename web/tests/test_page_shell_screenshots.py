"""31p: sidebar-and-page shell — the six-shot owner review set.

§9 (owner 23 Oct): the blade stack is retired; the shell is the fixed
sidebar (§5) + full-page content. This test renders the six review shots
in headless Chrome against the REAL style.css and marks up the page-shell
contract on the way through (page header, section cards, tabs, zero blade
chrome). Identity + web run in-process (shared loop, real dev DB); the
test org carries a full 31m contact block and is set LIVE in-script so
the public-page regression shot answers on /org/<slug>.

Screenshot proof lands in var/screenshots/31p-*.png. Skips cleanly when
Chrome is not installed.
"""
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from test_profile_preview_ports import (
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)
from test_public_contact_details import ADDRESS, PHONE, WHATSAPP

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
WEB_ROOT = Path(__file__).resolve().parents[1]
SHOTS = WEB_ROOT.parent / "var" / "screenshots"

# The Save bar is x-show="dirty" — Alpine only reveals it once the form is
# dirty. A screenshot cannot type, so the shot forces it visible the same
# way the dirty state does (author !important beats x-show's inline hide).
_FORCE_SAVE_BAR = "<style>.sv-save-bar{display:flex !important}</style>"


def _shot(html: str, tmp_path: Path, name: str, width: int, height: int,
          extra: str = "", prefix: str = "31p") -> None:
    """Render fetched app HTML with the real stylesheet AND scripts (Alpine
    actually runs — the mobile rail collapse and x-cloak are real) and
    screenshot it."""
    page = html.replace(
        'href="/static/style.css"',
        f'href="{(WEB_ROOT / "static" / "style.css").as_uri()}"',
    ).replace(
        'src="/static/',
        f'src="{(WEB_ROOT / "static").as_uri()}/',
    )
    if extra:
        page = page.replace("</body>", extra + "\n</body>")
    f = tmp_path / f"{prefix}-{name}.html"
    f.write_text(page)
    SHOTS.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
         "--virtual-time-budget=3000",
         f"--window-size={width},{height}",
         f"--screenshot={SHOTS / f'{prefix}-{name}.png'}", f.as_uri()],
        capture_output=True, timeout=60,
    )


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_page_shell_screenshot_set_31p(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"pg-{u}@example.com"
            slug = f"page-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.",
                        "address": ADDRESS,
                        "phone": PHONE,
                        "contact_email": f"ops-{u}@harbor.example",
                        "whatsapp": WHATSAPP,
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                # set the org LIVE in-script — the public page must answer
                await _verify_email_and_go_live(idc, email, sid)

                # 1. dashboard page
                dash = await w.get("/dashboard")
                assert dash.status_code == 200
                assert 'class="sv-page-header"' in dash.text
                assert "sv-blade" not in dash.text
                _shot(dash.text, tmp_path, "dashboard", 1280, 1000)

                # 2. profile editor — FULL-WIDTH form (31q: the preview
                #    aside is retired from edit mode; the form owns the
                #    whole content column)
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                assert "sv-editor-preview" not in editor.text
                assert editor.text.count('class="sv-section-card"') >= 4
                _shot(editor.text, tmp_path, "profile-settings", 1440, 1250)

                # 3. underline page-tabs component (view mode)
                view = await w.get("/org/profile")
                assert view.status_code == 200
                assert 'class="sv-page-tabs"' in view.text
                _shot(view.text, tmp_path, "tabs", 1280, 1100)

                # 4. public page — unchanged regression (still the 720px card)
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert "sv-org-card" in pub.text
                assert '<h2>Contact</h2>' in pub.text
                _shot(pub.text, tmp_path, "public-org", 1280, 1250)

                # 5. 390px mobile width check (dashboard page)
                _shot(dash.text, tmp_path, "mobile-390", 390, 844)

                # 6. edit Save bar (forced visible; Alpine reveals on dirty).
                #    Tall window: the bar sits at the bottom of the form.
                assert 'class="sv-save-bar"' in editor.text
                _shot(editor.text, tmp_path, "save-bar", 1280, 2700,
                      extra=_FORCE_SAVE_BAR)

    arun(scenario())
