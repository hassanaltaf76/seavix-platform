"""31u: country flags, filterable country pickers, leading-zero tolerance.

Owner feedback (23 Oct): "country code should have flag, I should be
able to filter the country, and on mobile a leading 0 before the
national number should be allowed".

- flag(iso2): regional-indicator emoji from the alpha-2 letters (pure
  chr math — no assets, no CDN), registered as a Jinja global.
- strip_trunk: the E.164 trunk-prefix drop for the cc-set tel:/wa.me
  hrefs — exactly ONE leading 0, only when a country code is set.
- The three country fields are .sv-search-select comboboxes (single-
  select chip-picker variant) over the vendored countries list, injected
  once with flags; the value rides a hidden input (31t field names).
- A headless-Chrome functional test drives the REAL editor DOM through
  Alpine: filter by name ("uni"), filter by dial code ("971", "44"),
  click/enter/keyboard pick, × clear, saved value pre-selected, and the
  dirty contract (typing alone is NOT dirty; pick/clear are).
"""
import json
import re
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from test_profile_preview_ports import _register_and_login, _web
from test_public_contact_details import _combobox_value

WEB_ROOT = Path(__file__).resolve().parents[1]
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
ALPINE = WEB_ROOT / "static" / "vendor" / "alpine.min.js"

# the owner's exact cases: GB mobile number with the leading 0 a user
# types on a phone, and a cc-less number where the 0 must survive
LEADZERO_CC = "+44"
LEADZERO_NATIONAL = "07418004800"
LEADZERO_TEL_HREF = "tel:+447418004800"      # trunk 0 dropped
LEADZERO_WA_HREF = "https://wa.me/447418004800"


def test_flag_is_regional_indicator_math():
    from webapp.filters import flag

    assert flag("AE") == "🇦🇪"
    assert flag("ae") == "🇦🇪"           # case-insensitive
    assert flag("GB") == "🇬🇧"
    assert flag("BR") == "🇧🇷"
    assert len(flag("AE")) == 2          # two regional-indicator chars
    # non alpha-2 input yields NO flag rather than garbage codepoints
    assert flag("") == ""
    assert flag(None) == ""
    assert flag("XX") == "🇽🇽"           # valid letters, unknown country
    assert flag("A") == ""
    assert flag("A1") == ""
    assert flag("AEE") == ""
    assert flag("éé") == ""


def test_strip_trunk_drops_exactly_one_leading_zero():
    from webapp.filters import strip_trunk

    assert strip_trunk("07418004800") == "7418004800"
    assert strip_trunk("007418004800") == "07418004800"  # ONE zero only
    assert strip_trunk("50 123 4567") == "501234567"     # non-digits stripped
    assert strip_trunk("7418004800") == "7418004800"     # no leading 0
    assert strip_trunk("") == ""


def test_leading_zero_cc_set_links_drop_trunk(arun, id_asgi):
    """The exact acceptance case: "07418004800" + cc "+44" is ACCEPTED
    and stored as typed, displayed with the 0, and the tel:/wa.me hrefs
    carry +44 7418004800."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"lz-{u}@example.com"
            slug = f"leadzero-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "",
                          "country_code": "GB",
                          "phone_cc": LEADZERO_CC,
                          "phone": LEADZERO_NATIONAL,
                          "whatsapp_cc": LEADZERO_CC,
                          "whatsapp": LEADZERO_NATIONAL},
                    follow_redirects=False,
                )
                assert saved.status_code == 303  # accepted, not rejected

                view = await w.get(saved.headers["location"])
                assert view.status_code == 200
                # displayed text keeps the user's original
                assert f"{LEADZERO_CC} {LEADZERO_NATIONAL}" in view.text
                assert LEADZERO_NATIONAL in view.text
                # hrefs drop the trunk 0 against the country code
                assert f'href="{LEADZERO_TEL_HREF}"' in view.text
                assert f'href="{LEADZERO_WA_HREF}"' in view.text

                # stored EXACTLY as typed (leading 0 preserved)
                model = (await idc.get(f"/api/orgs/{slug}")).json()
                assert model["phone"] == LEADZERO_NATIONAL
                assert model["whatsapp"] == LEADZERO_NATIONAL
                assert model["phone_cc"] == LEADZERO_CC

                # the editor pre-fills value + national, 0 intact
                editor = await w.get("/org/profile?view=edit")
                assert _combobox_value(editor.text, "phone_cc") == LEADZERO_CC
                m = re.search(r'name="phone"[^>]*value="([^"]*)"',
                              editor.text)
                assert m and m.group(1) == LEADZERO_NATIONAL

    arun(scenario())


def test_leading_zero_cc_less_links_keep_trunk(arun, id_asgi):
    """No country code -> nothing to normalize against: the href keeps
    the leading 0 (31t cc-less behavior, unchanged)."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"ln-{u}@example.com"
            slug = f"noleadzero-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "",
                          "phone": LEADZERO_NATIONAL,
                          "whatsapp": LEADZERO_NATIONAL},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                view = await w.get(saved.headers["location"])
                # cc-less tel: is the raw value (31m/31t behavior); the
                # wa.me digits keep the 0
                assert f'href="tel:{LEADZERO_NATIONAL}"' in view.text
                assert f'href="https://wa.me/0{LEADZERO_NATIONAL[1:]}"' \
                    in view.text  # "07418004800", 0 intact

    arun(scenario())


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_combobox_filter_pick_clear_dirty_in_browser(arun, id_asgi, tmp_path):
    """Headless-Chrome functional test of the REAL editor DOM: the
    comboboxes filter the injected countries list (name AND dial code),
    flags render, click/enter/keyboard pick updates the hidden input,
    × clears, the saved value pre-selects, and the 31q dirty contract
    holds — typing alone is NOT dirty, picking/clearing IS."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"cb-{u}@example.com"
            slug = f"combo-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "", "country_code": "AE",
                          "phone_cc": "+971", "phone": "50 123 4567",
                          "whatsapp_cc": "+971", "whatsapp": "50 123 4567"},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                return editor.text

    html = arun(scenario())
    # vendored scripts are same-origin URLs — inline Alpine so the page
    # boots from file:// (htmx is not needed for this interaction)
    html = html.replace(
        '<script defer src="/static/vendor/htmx.min.js"></script>', "")
    html = html.replace(
        '<script defer src="/static/vendor/alpine.min.js"></script>',
        f"<script>{ALPINE.read_text()}</script>")
    assert "svSearchSelect" in html and "window.svCountries" in html
    html = html.replace("</body>", _DRIVER + "</body>")
    tmp = tmp_path / "31u-combobox.html"
    tmp.write_text(html)
    out = subprocess.run(
        [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
         "--virtual-time-budget=8000", "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, timeout=90,
    ).stdout
    m = re.search(
        r'<pre id="sv-test-results"[^>]*>(.*?)</pre>', out, re.S)
    assert m, "driver produced no verdict; tail of DOM:\n" + out[-1500:]
    r = json.loads(m.group(1))

    # flags render in the injected options; the saved value pre-selects
    assert r["countryOptions"] == 243
    assert r["phoneOptions"] == 243
    assert "🇦🇫 Afghanistan" in r["firstCountryOption"]  # flag + name
    assert r["initialValues"] == {"country": "AE", "phone": "+971",
                                  "whatsapp": "+971"}
    assert r["initialLabels"]["country"] == "🇦🇪 United Arab Emirates"
    assert r["initialLabels"]["phone"] == "🇦🇪 +971 — United Arab Emirates"

    # "uni" finds United Arab Emirates + United Kingdom, flags included
    # (other name matches like Réunion / United States may also hit)
    assert "🇦🇪 United Arab Emirates" in r["uniOptions"]
    assert "🇬🇧 United Kingdom" in r["uniOptions"]
    assert len(r["uniOptions"]) < 10  # it FILTERED the 243-entry list
    # ... and typing alone is NOT dirty
    assert r["barHiddenWhileTyping"] is True
    # Escape closes and restores the label
    assert r["labelAfterEscape"] == "🇦🇪 United Arab Emirates"

    # "971" finds the dial code; keyboard ArrowDown+Enter picks it
    assert r["dial971"] == ["🇦🇪 +971 — United Arab Emirates"]
    assert r["phoneValueAfterEnter"] == "+971"
    assert r["barVisibleAfterPick"] is True

    # "44" matches +44 exactly and as a substring (+244, +1441) — all
    # four +44 entries are findable; clicking the exact one picks it
    assert len(r["dial44Options"]) == 6
    for entry in ("🇬🇬 +44 — Guernsey", "🇮🇲 +44 — Isle of Man",
                  "🇯🇪 +44 — Jersey", "🇬🇧 +44 — United Kingdom"):
        assert entry in r["dial44Options"]
    assert r["waValueAfterClick"] == "+44"
    assert r["waLabelAfterClick"] == "🇬🇬 +44 — Guernsey"

    # × clears (hidden input empties); Enter with a query picks the top
    # hit ("king" -> United Kingdom)
    assert r["countryValueAfterClear"] == ""
    assert r["countryValueAfterKingEnter"] == "GB"


_DRIVER = r"""<script>
window.addEventListener('load', () => {
  const tick = (ms) => new Promise((res) => setTimeout(res, ms || 60));
  const results = {};
  (async () => {
    await tick(150);  // Alpine initializes on DOMContentLoaded
    const q = (sel) => document.querySelector(sel);
    const box = {
      country: document.getElementById('country_code'),
      phone: document.getElementById('phone_cc'),
      whatsapp: document.getElementById('whatsapp_cc'),
    };
    const hidden = (name) => q('input[name="' + name + '"]').value;
    const bar = q('.sv-save-bar');
    const barVisible = () => bar && getComputedStyle(bar).display !== 'none';
    const list = (id) => {
      const ul = document.getElementById(id);
      return {
        open: getComputedStyle(ul).display !== 'none',
        texts: [...ul.querySelectorAll('li button')].map((b) => b.textContent),
      };
    };
    const type = async (el, text) => {
      el.focus();
      el.value = text;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      await tick(250);  // 150ms debounce + render
    };
    const key = (el, k) => el.dispatchEvent(
      new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));

    results.countryOptions = list('country-code-results').texts.length;
    results.phoneOptions = list('phone-cc-results').texts.length;
    results.firstCountryOption = list('country-code-results').texts[0];
    results.initialValues = {
      country: hidden('country_code'), phone: hidden('phone_cc'),
      whatsapp: hidden('whatsapp_cc'),
    };
    results.initialLabels = {
      country: box.country.value, phone: box.phone.value,
    };
    results.barHiddenInitially = !barVisible();

    // filter by NAME: "uni" -> UAE + UK only; typing alone is not dirty
    await type(box.country, 'uni');
    results.uniOptions = list('country-code-results').texts;
    results.barHiddenWhileTyping = !barVisible();
    // Escape closes and restores the selected label
    key(box.country, 'Escape');
    await tick(80);
    results.labelAfterEscape = box.country.value;

    // filter by DIAL CODE: "971" -> the one +971 entry; keyboard pick
    await type(box.phone, '971');
    results.dial971 = list('phone-cc-results').texts;
    key(box.phone, 'ArrowDown');
    await tick(80);
    key(box.phone, 'Enter');
    await tick(80);
    results.phoneValueAfterEnter = hidden('phone_cc');
    results.phoneLabelAfterEnter = box.phone.value;
    results.barVisibleAfterPick = barVisible();

    // "44" matches +44 exactly AND as a substring (+244, +1441);
    // clicking the exact +44 entry picks it
    await type(box.whatsapp, '44');
    results.dial44Options = list('whatsapp-cc-results').texts;
    const exact44 = [...document.querySelectorAll(
      '#whatsapp-cc-results li button')]
      .find((b) => b.textContent.includes('+44 —'));
    exact44.click();
    await tick(80);
    results.waValueAfterClick = hidden('whatsapp_cc');
    results.waLabelAfterClick = box.whatsapp.value;

    // × clears the country picker
    q('button[aria-label="Clear country"]').click();
    await tick(80);
    results.countryValueAfterClear = hidden('country_code');

    // Enter with a query (no arrow navigation) picks the top hit
    await type(box.country, 'king');
    key(box.country, 'Enter');
    await tick(80);
    results.countryValueAfterKingEnter = hidden('country_code');

    const pre = document.createElement('pre');
    pre.id = 'sv-test-results';
    pre.style.display = 'none';
    pre.textContent = JSON.stringify(results);
    document.body.appendChild(pre);
  })();
});
</script>"""
