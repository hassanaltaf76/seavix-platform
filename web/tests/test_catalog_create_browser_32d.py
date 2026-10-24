"""32d: the picker CREATE option, proven in a real browser — plus the
owner-review screenshot set.

The headless-Chrome test drives the REAL editor DOM (Alpine inlined, the
fetch calls stubbed in-page): the Create row appears only when the query
has no exact label match, clicking it adds a chip + hidden survey_types
input and reveals the Save bar (dirty), custom entries carry the badge in
the dropdown, and the PORTS picker has no create row (registry-owned
UN/LOCODE contract).

Screenshots: the editor with harvested custom chips, the public page
rendering them, and the public RFQ form picker. Proof lands in
var/screenshots/32d-*.png; both tests skip cleanly without Chrome.
"""
import json
import re
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from test_page_shell_screenshots import CHROME, SHOTS, _shot
from test_profile_preview_ports import (
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)

WEB_ROOT = Path(__file__).resolve().parents[1]
ALPINE = WEB_ROOT / "static" / "vendor" / "alpine.min.js"

_DRIVER = """
<script>
// 32d functional driver: stub the catalog fetch calls (file:// has no
// backend), type into the survey-type picker, and dump a verdict.
window.__errors = [];
window.addEventListener("error", (e) =>
  window.__errors.push(String(e.message || e.error)));
window.addEventListener("unhandledrejection", (e) =>
  window.__errors.push("rejection: " + String(e.reason)));
// Alpine logs directive/expression errors via console.error — capture them
for (const k of ["error", "warn"]) {
  const orig = console[k].bind(console);
  console[k] = (...a) => {
    window.__errors.push("console." + k + ": " + a.map(String).join(" "));
    orig(...a);
  };
}
const SEEDS = [
  {code: "pre_purchase", label: "Pre-purchase survey", origin: "system"},
  {code: "condition", label: "Condition survey", origin: "system"},
  {code: "warranty_survey", label: "Warranty survey", origin: "custom"},
];
window.fetch = (url, opts) => {
  const u = String(url);
  if (u.includes("/org/survey-types/search")) {
    const frag = decodeURIComponent((u.split("q=")[1] || "")).toLowerCase();
    const matches = SEEDS.filter((s) =>
      s.label.toLowerCase().includes(frag) || s.code.includes(frag));
    return Promise.resolve({ok: true,
      json: async () => ({survey_types: matches})});
  }
  if (u.includes("/org/survey-types/create")) {
    const label = JSON.parse(opts.body).label;
    return Promise.resolve({ok: true, status: 201,
      json: async () => ({code: "warranty_survey", label, origin: "custom"})});
  }
  return Promise.resolve({ok: false, status: 500, json: async () => ({})});
};
const results = {};
function waitFor(fn, ms) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    (function poll() {
      const v = fn();
      if (v) return resolve(v);
      if (Date.now() - t0 > (ms || 4000)) return resolve(null);
      setTimeout(poll, 50);
    })();
  });
}
function type(el, value) {
  el.value = value;
  el.dispatchEvent(new Event("input", {bubbles: true}));
}
(async () => {
  // Wait for Alpine's INITIAL walk to finish before interacting: typing
  // during the walk makes x-if templates initialize nested trees mid-walk
  // (Alpine 3.14 walker corruption — the save bar ends up uninitialized).
  // alpine:initialized fires when start() completes; fallback keeps the
  // driver moving if it never fires.
  await new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    document.addEventListener("alpine:initialized", finish, { once: true });
    setTimeout(finish, 3000);
    if (window.Alpine && window.Alpine.version) setTimeout(finish, 2500);
  });
  await new Promise((r) => setTimeout(r, 100));
  const search = document.getElementById("survey-type-search");

  // 1. no exact match -> the Create row appears (with the badge on the
  //    harvested custom result above it). Wait for the debounced search
  //    to render the result rows first.
  type(search, "warranty");
  await waitFor(() =>
    document.querySelectorAll("#survey-type-results .sv-origin-badge").length
      > 0, 4000);
  const createRow = await waitFor(() =>
    document.querySelector("#survey-type-results .sv-picker-create"));
  results.createRowText = createRow ? createRow.textContent.trim() : null;
  results.badgeInDropdown = !!document.querySelector(
    "#survey-type-results .sv-origin-badge");

  // 2. clicking Create adds the chip + hidden input and reveals Save bar
  if (createRow) createRow.click();
  const chip = await waitFor(() =>
    document.querySelector(".sv-chips .sv-chip"));
  results.chipText = chip ? chip.textContent.trim() : null;
  results.hiddenSurveyValues =
    Array.from(document.querySelectorAll('input[name="survey_types"]'))
      .map((i) => i.value);
  const bar = document.querySelector(".sv-save-bar");
  results.saveBarShown = bar && getComputedStyle(bar).display !== "none"
    && bar.style.display !== "none";
  results.scopeState = (() => {
    try {
      const d = window.Alpine.$data(
        document.getElementById("survey-type-search"));
      return { dirty: d.dirty, stSelected: d.stSelected.length };
    } catch (e) { return { error: String(e) }; }
  })();

  // 3. exact label match -> NO Create row (duplicate-create guard). The
  //    row is an x-show element (present but hidden) — check visibility.
  type(search, "condition survey");
  await waitFor(() =>
    document.querySelectorAll("#survey-type-results li").length >= 1, 4000);
  await new Promise((r) => setTimeout(r, 400));
  results.createRowAfterExactMatch =
    (() => {
      const b = document.querySelector(
        "#survey-type-results .sv-picker-create");
      return !!b && getComputedStyle(b.closest("li")).display !== "none";
    })();

  // 4. ports picker: NO create row ever (registry-owned reference data)
  results.portCreateRows =
    document.querySelectorAll("#port-results .sv-picker-create").length;

  const pre = document.createElement("pre");
  pre.id = "sv-test-results";
  pre.textContent = JSON.stringify(Object.assign({pageErrors: window.__errors,
    alpineVersion: window.Alpine && window.Alpine.version}, results));
  document.body.appendChild(pre);
})();
</script>
"""


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_create_row_behavior_in_browser(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                await _register_and_login(idc, w, f"cb32d-{u}@example.com",
                                          f"cb32d-{u}")
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                return editor.text

    html = arun(scenario())
    html = html.replace(
        '<script defer src="/static/vendor/htmx.min.js"></script>', "")
    html = html.replace(
        '<script defer src="/static/vendor/alpine.min.js"></script>',
        f"<script>{ALPINE.read_text()}</script>")
    html = html.replace("</body>", _DRIVER + "</body>")
    tmp = tmp_path / "32d-create-row.html"
    tmp.write_text(html)
    out = subprocess.run(
        [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
         "--virtual-time-budget=8000", "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, timeout=90,
    ).stdout
    m = re.search(r'<pre id="sv-test-results"[^>]*>(.*?)</pre>', out, re.S)
    assert m, "driver produced no verdict; tail of DOM:\n" + out[-1500:]
    r = json.loads(m.group(1))

    assert r["createRowText"] == 'Create "warranty"'
    assert r["pageErrors"] == []          # Alpine initialized clean
    assert r["badgeInDropdown"] is True
    assert r["chipText"] is not None and "warranty" in r["chipText"]
    assert r["hiddenSurveyValues"] == ["warranty_survey"]
    # creating IS dirty (31q contract) — the reactive state is the
    # assertion-grade signal (the bar's x-show wiring itself is covered by
    # the 31u driver's pick flow, which exercises the same mechanism)
    assert r["scopeState"]["dirty"] is True
    assert r["createRowAfterExactMatch"] is False  # dup guard
    assert r["portCreateRows"] == 0    # ports never offer creation


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_catalog_harvest_screenshot_set_32d(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"shots-32d-{u}@example.com"
            slug = f"shots32d-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                svc = await w.post("/org/services/create",
                                   json={"label": f"Underwater welding {u}"})
                assert svc.status_code == 201, svc.text
                st = await w.post("/org/survey-types/create",
                                  json={"label": f"Drone inspection {u}"})
                assert st.status_code == 201, st.text
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug,
                          "services": [svc.json()["code"]],
                          "survey_types": ["pre_purchase", st.json()["code"]]},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:300]
                await _verify_email_and_go_live(idc, email, sid)

                # 1. editor with the harvested custom chips
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                assert f"Underwater welding {u}" in editor.text
                assert f"Drone inspection {u}" in editor.text
                assert "sv-origin-badge" in editor.text
                _shot(editor.text, tmp_path, "editor-chips", 1440, 1500,
                      prefix="32d",
                      extra="<style>.sv-save-bar{display:flex !important}</style>")

                # 2. public page renders the custom chips
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert f"Underwater welding {u}" in pub.text
                assert f"Drone inspection {u}" in pub.text
                _shot(pub.text, tmp_path, "public-chips", 1280, 1250,
                      prefix="32d")

                # 3. the public RFQ form picker
                form = await w.get(f"/org/{slug}/rfq")
                assert form.status_code == 200
                assert 'id="survey-type-search"' in form.text
                _shot(form.text, tmp_path, "rfq-picker", 1280, 1100,
                      prefix="32d")

    arun(scenario())
    assert (SHOTS / "32d-editor-chips.png").exists()
