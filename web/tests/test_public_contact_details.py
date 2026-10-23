"""31m+31t+31u: public contact details on the org profile — end to end
through the web app and the identity service (shared loop, real dev DB).

31m: the public Contact section (present only when a field is set;
tel:/mailto:/wa.me links); preview == public byte-equality through the
shared partial; ZERO account-email leakage; per-field 422 inline errors;
clearing fields.

31t: structured address (line1/line2/city/state/postal/country) + dial
codes. Structured wins at render when ANY structured field is set; a
LEGACY-only profile (free-text address / cc-embedded numbers written
straight to the DB, as 31m-era data looks) still renders exactly as 31m
did — back-compat is pinned here.

31u: the three country fields are searchable flag comboboxes
(.sv-search-select over the vendored list — client-side filter, no
endpoint); the saved value pre-fills the hidden field + label; a leading
0 in the national number is kept as typed and dropped from the tel:/wa.me
hrefs when (and only when) a country code is set.
"""
import json
import re
import uuid

import httpx

from test_profile_preview_ports import (
    _article,
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)

# 31m-era LEGACY values (written straight to the DB in the back-compat
# test — they can no longer be SAVED through the new validation).
LEGACY_ADDRESS = "Suite 1404, Marina Plaza\nDubai Marina, Dubai, UAE"
LEGACY_PHONE = "+971 4 555 0100"
LEGACY_WHATSAPP = "+971 (50) 123-4567"

# 31t structured contact block
LINE1 = "Suite 1404, Marina Plaza"
LINE2 = "Marina Tower"
CITY = "Dubai"
POSTAL = "00000"
COUNTRY = "AE"
COUNTRY_NAME_UPPER = "UNITED ARAB EMIRATES"
PHONE_CC = "+971"
PHONE = "4 555 0100"
PHONE_DISPLAY = "+971 4 555 0100"
PHONE_TEL_HREF = "tel:+97145550100"
WA_CC = "+971"
WHATSAPP = "50 123 4567"
WA_DISPLAY = "+971 50 123 4567"
WA_DIGITS = "971501234567"  # cc + national, non-digits stripped

STRUCTURED = {
    "address_line1": LINE1,
    "address_line2": LINE2,
    "city": CITY,
    "postal_code": POSTAL,
    "country_code": COUNTRY,
    "phone_cc": PHONE_CC,
    "phone": PHONE,
    "whatsapp_cc": WA_CC,
    "whatsapp": WHATSAPP,
}


def _input_value(html: str, name: str) -> str:
    m = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    assert m, f"no value= on input {name}"
    return m.group(1)


def _combobox_value(html: str, name: str) -> str:
    """31u: the combobox's value rides a hidden input carrying the 31t
    field name — rendered with a static value (no-JS + tests) that Alpine
    keeps in sync."""
    m = re.search(
        rf'<input type="hidden" name="{name}"[^>]*?value="([^"]*)"', html
    )
    assert m, f"no hidden input {name}"
    return m.group(1)


def _sv_countries(html: str) -> list[dict]:
    """31u: the vendored countries list injected once for client-side
    filtering (flags included)."""
    m = re.search(r"window\.svCountries = (\[.*?\]);</script>", html, re.S)
    assert m, "no window.svCountries injection"
    return json.loads(m.group(1))


def test_structured_contact_block_round_trip_links_and_byte_equality(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"m-{u}@example.com"          # the ACCOUNT email
            public_email = f"ops-{u}@harbor.example"
            slug = f"contact-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)

                saved = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.",
                        "contact_email": public_email,
                        **STRUCTURED,
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                # DRAFT preview renders the structured contact block:
                # line1 / line2 / city, state postal / COUNTRY NAME
                view = await w.get(saved.headers["location"])
                assert view.status_code == 200
                assert '<h2>Contact</h2>' in view.text
                assert LINE1 in view.text
                assert LINE2 in view.text
                assert f"{CITY} {POSTAL}" in view.text  # city + postal
                assert COUNTRY_NAME_UPPER in view.text
                assert f'href="{PHONE_TEL_HREF}"' in view.text
                assert PHONE_DISPLAY in view.text
                assert f'href="mailto:{public_email}"' in view.text
                assert f'href="https://wa.me/{WA_DIGITS}"' in view.text
                assert "wa.me/+" not in view.text  # href is digits-only
                assert WA_DISPLAY in view.text      # original shown as text
                assert "Chat on WhatsApp" in view.text
                # ZERO account-email leakage on the public-content surface
                assert email not in view.text

                # go live: the public page renders the SAME article,
                # byte for byte (31f contract holds with 31t content)
                await _verify_email_and_go_live(idc, email, sid)
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert '<h2>Contact</h2>' in pub.text
                assert f'href="{PHONE_TEL_HREF}"' in pub.text
                assert f'href="https://wa.me/{WA_DIGITS}"' in pub.text
                assert email not in pub.text       # no account-email markup
                blade = await w.get("/org/profile")
                assert _article(blade.text) == _article(pub.text)

                # the public read model carries the structured fields
                model = (await idc.get(f"/api/orgs/{slug}")).json()
                for field, value in STRUCTURED.items():
                    assert model[field] == value, field
                assert model["address"] is None  # nothing legacy was sent

    arun(scenario())


def test_editor_comboboxes_render_all_countries_with_saved_value_selected(
    arun, id_asgi
):
    """31u: the Country / phone-cc / WhatsApp-cc <select>s are searchable
    single-select comboboxes over the vendored list (injected once, flags
    included); the saved value pre-fills the hidden field AND the label;
    legacy numbers pre-fill the split (cc picker + national input)."""
    from webapp.countries import COUNTRIES

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"sel-{u}@example.com"
            slug = f"selects-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "", **STRUCTURED},
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                # the injected list IS the vendored list: same length,
                # alpha-sorted, dial codes + regional-indicator flags
                items = _sv_countries(editor.text)
                assert len(items) == len(COUNTRIES)
                names = [c["name"] for c in items]
                assert names == [n for n, _, _ in COUNTRIES]
                ae = next(c for c in items if c["iso2"] == "AE")
                assert ae == {"name": "United Arab Emirates", "iso2": "AE",
                              "dial": "+971", "flag": "🇦🇪"}
                # every entry carries a two-codepoint regional-indicator flag
                for c in items:
                    assert len(c["flag"]) == 2
                # three comboboxes, all .sv-search-select over that list
                assert editor.text.count("svSearchSelect({") == 3
                for results_id in ("country-code-results", "phone-cc-results",
                                   "whatsapp-cc-results"):
                    assert f'id="{results_id}"' in editor.text
                # saved value pre-selected: hidden field + flag label
                assert _combobox_value(editor.text, "country_code") == COUNTRY
                assert _combobox_value(editor.text, "phone_cc") == PHONE_CC
                assert _combobox_value(editor.text, "whatsapp_cc") == WA_CC
                assert "🇦🇪 United Arab Emirates" in editor.text
                assert "🇦🇪 +971 — United Arab Emirates" in editor.text
                # the structured text fields reseed too
                assert _input_value(editor.text, "address_line1") == LINE1
                assert _input_value(editor.text, "city") == CITY
                assert _input_value(editor.text, "phone") == PHONE
                assert _input_value(editor.text, "whatsapp") == WHATSAPP

                # legacy prefill split: a 31m-era value with the dial
                # code embedded seeds cc + national separately; +44 maps
                # to the first alpha-sorted entry carrying it (Guernsey)
                from sqlalchemy import text as sql

                from app.db import SessionLocal

                async with SessionLocal() as s:
                    await s.execute(
                        sql("UPDATE organization SET whatsapp = :wa, "
                            "whatsapp_cc = NULL WHERE slug = :slug"),
                        {"wa": "+44 7700 900123", "slug": slug},
                    )
                    await s.commit()
                editor2 = await w.get("/org/profile?view=edit")
                assert _combobox_value(editor2.text, "whatsapp_cc") == "+44"
                assert _input_value(editor2.text, "whatsapp") == "7700 900123"
                assert "🇬🇬 +44 — Guernsey" in editor2.text

    arun(scenario())


def test_legacy_only_profile_renders_as_before_31m_back_compat(arun, id_asgi):
    """31m-era rows (free-text address, cc-embedded phone/whatsapp, no
    structured fields) render EXACTLY as they did before 31t — written
    straight to the identity DB, rendered on both surfaces."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"bc-{u}@example.com"
            slug = f"legacy-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": ""},
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                # plant 31m-era data directly (it can no longer be saved)
                from sqlalchemy import text as sql

                from app.db import SessionLocal

                async with SessionLocal() as s:
                    await s.execute(
                        sql(
                            "UPDATE organization SET address = :a, "
                            "phone = :p, whatsapp = :w WHERE slug = :slug"
                        ),
                        {"a": LEGACY_ADDRESS, "p": LEGACY_PHONE,
                         "w": LEGACY_WHATSAPP, "slug": slug},
                    )
                    await s.commit()

                await _verify_email_and_go_live(idc, email, sid)
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert "Suite 1404, Marina Plaza" in pub.text
                assert "Dubai Marina, Dubai, UAE" in pub.text
                assert f'href="tel:{LEGACY_PHONE}"' in pub.text
                assert f'href="https://wa.me/971501234567"' in pub.text
                assert LEGACY_WHATSAPP in pub.text
                # no structured rendering on a legacy-only profile
                assert "UNITED ARAB EMIRATES" not in pub.text
                blade = await w.get("/org/profile")
                assert _article(blade.text) == _article(pub.text)

                # and an UNRELATED save must preserve the legacy text:
                # the hidden address field posts it back verbatim
                kept = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "Still sailing.",
                          "address": LEGACY_ADDRESS},
                    follow_redirects=False,
                )
                assert kept.status_code == 303
                model = (await idc.get(f"/api/orgs/{slug}")).json()
                assert model["address"] == LEGACY_ADDRESS
                pub2 = await w.get(f"/org/{slug}")
                assert "Dubai Marina, Dubai, UAE" in pub2.text

    arun(scenario())


def test_structured_wins_over_legacy_when_both_set(arun, id_asgi):
    """31t render rule: ANY structured field set -> structured lines
    render and the legacy free-text address is NOT rendered."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"sw-{u}@example.com"
            slug = f"win-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "",
                          "address": LEGACY_ADDRESS,
                          "address_line1": LINE1, "city": CITY,
                          "country_code": COUNTRY},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                # the legacy text is preserved in the DB (hidden field)…
                model = (await idc.get(f"/api/orgs/{slug}")).json()
                assert model["address"] == LEGACY_ADDRESS
                # …but structured wins at render
                view = await w.get(saved.headers["location"])
                assert LINE1 in view.text
                assert COUNTRY_NAME_UPPER in view.text
                assert "Dubai Marina, Dubai, UAE" not in view.text

    arun(scenario())


def test_contact_section_absent_until_a_field_is_set(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"ca-{u}@example.com"
            slug = f"nocontact-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                # choose the public address first — going live publishes
                # whatever slug the org currently has
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": ""},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                await _verify_email_and_go_live(idc, email, sid)

                # nothing set -> NO contact markup at all, either surface
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert "sv-org-contact" not in pub.text
                assert "<h2>Contact</h2>" not in pub.text
                blade = await w.get("/org/profile")
                assert "sv-org-contact" not in blade.text

                # one structured field (country only) -> the section
                # appears with just that line
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "", "country_code": "AE"},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                pub = await w.get(f"/org/{slug}")
                assert '<h2>Contact</h2>' in pub.text
                assert COUNTRY_NAME_UPPER in pub.text
                article = _article(pub.text)
                assert "tel:" not in article
                assert "mailto:" not in article
                assert "wa.me" not in article

    arun(scenario())


def test_contact_email_never_prefilled_from_account_email(arun, id_asgi):
    """Surface separation: the editor's public contact fields start
    EMPTY — the account/login email is private state and never seeds
    public fields."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"sep-{u}@example.com"
            slug = f"separation-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "Surveyors."},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                assert _input_value(editor.text, "address") == ""
                for field in ("address_line1", "address_line2", "city",
                              "state", "postal_code", "phone", "whatsapp",
                              "contact_email"):
                    assert _input_value(editor.text, field) == "", field
                assert _combobox_value(editor.text, "country_code") == ""
                assert _combobox_value(editor.text, "phone_cc") == ""
                assert _combobox_value(editor.text, "whatsapp_cc") == ""
                # and nowhere on the whole editor blade either
                assert email not in editor.text

    arun(scenario())


def test_contact_validation_rejects_bad_values_inline(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"ve-{u}@example.com"
            slug = f"validate-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                good = {
                    "slug": slug,
                    "about": "",
                    "contact_email": f"ops-{u}@harbor.example",
                    **STRUCTURED,
                }

                ok = await w.post("/org/profile", data=good,
                                  follow_redirects=False)
                assert ok.status_code == 303

                # bad email -> 422 with the message next to the field,
                # and the submitted values stay in the form
                bad_email = await w.post(
                    "/org/profile", data={**good, "contact_email": "nope"}
                )
                assert bad_email.status_code == 422
                assert 'class="sv-field-error"' in bad_email.text
                assert "not a valid email" in bad_email.text
                assert _input_value(bad_email.text, "contact_email") == "nope"
                assert _input_value(bad_email.text, "whatsapp") == WHATSAPP

                # national number with letters -> 422; atomic, stored
                # value intact
                bad_wa = await w.post(
                    "/org/profile",
                    data={**good, "whatsapp": "call-me-now"},
                )
                assert bad_wa.status_code == 422
                assert "whatsapp" in bad_wa.text
                assert "country code has its own field" in bad_wa.text
                assert _input_value(bad_wa.text, "whatsapp") == "call-me-now"
                pub = await idc.get(f"/api/orgs/{slug}")
                assert pub.json()["whatsapp"] == WHATSAPP

                # a cc-embedded national number is rejected with guidance
                bad_phone = await w.post(
                    "/org/profile",
                    data={**good, "phone": "+971 4 555 0100"},
                )
                assert bad_phone.status_code == 422
                assert "country code has its own field" in bad_phone.text

                # both bad at once -> both errors rendered
                both = await w.post(
                    "/org/profile",
                    data={**good, "contact_email": "nope",
                          "whatsapp": "call-me-now"},
                )
                assert both.status_code == 422
                assert "not a valid email" in both.text
                assert "whatsapp" in both.text

                # public page is untouched by any of the rejected saves
                await _verify_email_and_go_live(idc, email, sid)
                pub_page = await w.get(f"/org/{slug}")
                assert WA_DISPLAY in pub_page.text

    arun(scenario())


def test_contact_fields_clear_when_emptied(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"cl-{u}@example.com"
            slug = f"clear-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug, "about": "",
                        "contact_email": f"ops-{u}@harbor.example",
                        **STRUCTURED,
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                cleared = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "",
                          "address_line1": "", "address_line2": "",
                          "city": "", "state": "", "postal_code": "",
                          "country_code": "", "phone_cc": "",
                          "whatsapp_cc": "", "phone": "",
                          "contact_email": "", "whatsapp": ""},
                    follow_redirects=False,
                )
                assert cleared.status_code == 303
                pub = await idc.get(f"/api/orgs/{slug}")
                for field in ("address", "address_line1", "address_line2",
                              "city", "state", "postal_code",
                              "country_code", "phone_cc", "whatsapp_cc",
                              "phone", "contact_email", "whatsapp"):
                    assert pub.json()[field] is None, field

                await _verify_email_and_go_live(idc, email, sid)
                pub_page = await w.get(f"/org/{slug}")
                assert "sv-org-contact" not in pub_page.text
                assert "<h2>Contact</h2>" not in pub_page.text

    arun(scenario())
