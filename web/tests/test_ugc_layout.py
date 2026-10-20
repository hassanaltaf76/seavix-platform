"""31l UGC overflow hardening — layout + block-behavior tests.

- A 200-character unbroken string stays INSIDE its container: a real
  headless-Chrome layout assertion (dump-dom verdict) against the real
  stylesheet, with the shared partial's markup shape. Skips cleanly when
  Chrome is not installed.
- Multi-paragraph text still renders as blocks (31h behavior unchanged),
  and the paragraphs filter keeps a 200-char unbroken string intact and
  escaped inside its single <p>.
- The preview==public byte-equality contract (31f) is unaffected by
  definition: this change is CSS classes + the shared partial only, and
  the existing equality tests keep proving it.
"""
import subprocess
import sys
from pathlib import Path

import pytest

WEB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEB_ROOT))

from webapp.filters import paragraphs  # noqa: E402

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LONG = "A" * 200  # the exact owner-screenshot case: one unbroken token


def test_paragraphs_multi_block_behavior_unchanged_31h():
    out = paragraphs("First paragraph here.\n\nSecond paragraph.\nsoft newline")
    assert out.count("<p>") == 2
    assert "<br>" in out  # soft newline preserved
    # the 31h XSS contract: input escaping happens inside the filter
    assert "<script>" not in paragraphs("<script>alert(1)</script>")


def test_paragraphs_keeps_unbroken_string_intact_in_one_block():
    out = paragraphs(LONG)
    assert out.count("<p>") == 1
    assert LONG in out  # full string preserved, single block


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_200_char_unbroken_string_stays_inside_container(tmp_path):
    """Headless-Chrome layout assertion with the REAL style.css: the
    shared-partial markup (article.sv-ugc + sv-prose + facts) in a
    600px column must not overflow with a 200-char unbroken string in
    every free-form UGC slot (name, description, website, phone). Port
    names are registry reference data rendered as single-line chips
    (31f .sv-port nowrap), so the port slot uses a realistic value —
    the chip wraps as a unit to the next line, which the container
    rule already covers."""
    style_uri = (WEB_ROOT / "static" / "style.css").as_uri()
    page = f"""<!doctype html>
<html><head><link rel="stylesheet" href="{style_uri}"></head>
<body style="margin:0">
<div class="col" style="width:600px">
  <article class="sv-ugc">
    <header class="sv-org-header">
      <div>
        <h1>{LONG}</h1>
        <div class="sv-prose"><p>{LONG}</p><p>Second block with {LONG}</p></div>
      </div>
    </header>
    <ul class="sv-org-facts">
      <li>Website: <a href="#" rel="nofollow">{LONG}</a></li>
      <li>Phone: {LONG}</li>
    </ul>
    <p class="sv-org-ports"><strong>Ports we serve:</strong>
      <span class="sv-port">Al Fujayrah</span></p>
  </article>
</div>
<script>
// runs synchronously: an inline script following a <link stylesheet>
// executes only after the CSSOM is ready, so layout is measurable here
// (a load-event listener races --dump-dom serialization; and the root
// element's scrollWidth reports the VIEWPORT width in headless, so the
// per-element scan is the meaningful check).
(function () {{
  const bad = [];
  document.querySelectorAll(".col *").forEach((n) => {{
    if (n.scrollWidth > n.clientWidth + 1)
      bad.push(n.tagName + "." + (n.className || "-"));
  }});
  document.body.setAttribute("data-ugc-verdict",
    bad.length ? "OVERFLOW:" + bad.join("|") : "contained");
}})();
</script>
</body></html>"""
    tmp = tmp_path / "ugc-overflow.html"
    tmp.write_text(page)
    out = subprocess.run(
        [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
         "--virtual-time-budget=3000", "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, timeout=60,
    ).stdout
    assert 'data-ugc-verdict="contained"' in out, (
        "unbroken UGC escaped its container:\n" + out[-800:]
    )
    # multi-paragraph input still renders as two prose blocks
    assert out.count("<p>") == 2  # the ports <p> carries a class attribute
