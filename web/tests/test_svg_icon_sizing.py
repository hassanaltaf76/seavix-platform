"""31o: inline SVG icon sizing — real-browser layout proof + owner screenshots.

Owner screenshot 23 Oct: the WhatsApp/phone/address icons and the info
sign rendered HUGE on the profile preview / public contact rows — the
31n inline SVGs shipped viewBox-only and inflated to their default
viewport size. The fix pins explicit width/height attributes on every
decorative SVG and backs them with per-context CSS rules (§4 sizes,
flex:none); web/tests/test_design_language.py locks that contract.

This test proves the fix in headless Chrome against the REAL style.css.
Identity + web run in-process (shared loop, real dev DB); the test org
gets a full 31m contact block and is set LIVE in-script. Every svg on
every surfaced page is measured:

  profile preview (blade):    rail 16 + contact rows 16
  profile editor (edit mode): rail 16 + info callout 20
  dashboard:                  rail 16 + skeleton cards 40
  landing (signed out):       feature cards 26
  /org/<slug> while draft:    sv-empty 40 (profile unavailable)
  /org/<slug> live:           contact rows 16

Screenshot proof lands in var/screenshots/31o-*.png. Skips cleanly
when Chrome is not installed.
"""
import re
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
from test_public_contact_details import STRUCTURED

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
WEB_ROOT = Path(__file__).resolve().parents[1]
SHOTS = WEB_ROOT.parent / "var" / "screenshots"

# inline script injected before </body>: runs after the linked stylesheet
# is applied, measures every rendered svg, and parks the verdict on <body>
# (see test_ugc_layout.py for the dump-dom measurement pattern).
_MEASURE = """
<script>
(function () {
  const out = [];
  document.querySelectorAll("svg").forEach((s) => {
    const r = s.getBoundingClientRect();
    out.push(Math.round(r.width) + "x" + Math.round(r.height));
  });
  document.body.setAttribute("data-svg-sizes", out.join(","));
})();
</script>
"""


def _render(html: str, tmp_path: Path, name: str, screenshot: bool = False) -> set:
    """Render fetched app HTML in headless Chrome with the real style.css
    and return the set of measured svg sizes, e.g. {(16, 16), (20, 20)}."""
    page = html.replace(
        'href="/static/style.css"',
        f'href="{(WEB_ROOT / "static" / "style.css").as_uri()}"',
    ).replace("</body>", _MEASURE + "\n</body>")
    f = tmp_path / f"{name}.html"
    f.write_text(page)
    out = subprocess.run(
        [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
         "--virtual-time-budget=3000", "--dump-dom", f.as_uri()],
        capture_output=True, text=True, timeout=60,
    ).stdout
    m = re.search(r'data-svg-sizes="([^"]*)"', out)
    assert m, f"{name}: measurement script did not run (dom tail): {out[-400:]}"
    sizes = set()
    for pair in filter(None, m.group(1).split(",")):
        w, h = pair.split("x")
        sizes.add((int(w), int(h)))
    if screenshot:
        SHOTS.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
             "--virtual-time-budget=3000",  # let the 220ms blade-in finish
             "--window-size=1280,1600",
             f"--screenshot={SHOTS / f'31o-{name}.png'}", f.as_uri()],
            capture_output=True, timeout=60,
        )
    return sizes


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_inline_svg_sizes_on_real_pages(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"svg-{u}@example.com"
            slug = f"svg-{u}"
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

                # profile preview (view mode): rail + 31t contact rows
                preview = await w.get(saved.headers["location"])
                assert preview.status_code == 200
                assert _render(preview.text, tmp_path, "profile-preview",
                               screenshot=True) == {(16, 16)}

                # editor (edit mode): rail + the 20px info-sign callout
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                assert _render(editor.text, tmp_path, "profile-editor",
                               screenshot=True) == {(16, 16), (20, 20)}

                # dashboard: rail + 40px awaiting-data skeleton icons
                dash = await w.get("/dashboard")
                assert dash.status_code == 200
                assert _render(dash.text, tmp_path, "dashboard",
                               screenshot=True) == {(16, 16), (40, 40)}

                # draft public page: 404 carrying the sv-empty icon at 40
                draft = await w.get(f"/org/{slug}")
                assert draft.status_code == 404
                assert "Profile not available" in draft.text
                assert _render(draft.text, tmp_path, "draft-public") == {(40, 40)}

                # set the org LIVE in-script, then the public page shows
                # the contact rows at 16px
                await _verify_email_and_go_live(idc, email, sid)
                live = await w.get(f"/org/{slug}")
                assert live.status_code == 200
                assert '<h2>Contact</h2>' in live.text
                assert _render(live.text, tmp_path, "org-live",
                               screenshot=True) == {(16, 16)}

    arun(scenario())

    # landing is signed-out-only chrome (31c) — fresh client, 26px feature cards
    async def landing():
        async with _web() as anon:
            page = await anon.get("/")
            assert page.status_code == 200
            assert _render(page.text, tmp_path, "landing",
                           screenshot=True) == {(26, 26)}

    arun(landing())
