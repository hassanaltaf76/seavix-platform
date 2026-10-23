"""31m: public contact details on the org profile — end to end through
the web app and the identity service (shared loop, real dev DB).

Covers: editor Save round-trip of address/phone/contact_email/whatsapp;
the public Contact section (present only when a field is set; address as
paragraph blocks; tel:/mailto:/wa.me links with digits stripped for the
href); preview == public byte-equality through the shared partial; ZERO
account-email leakage on both surfaces (contact_email is the public
business contact — never prefilled from, or linked to, the account
email); per-field 422 inline errors; clearing fields back to empty.
"""
import re
import uuid

import httpx

from test_profile_preview_ports import (
    _article,
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)

ADDRESS = "Suite 1404, Marina Plaza\nDubai Marina, Dubai, UAE"
PHONE = "+971 4 555 0100"
WHATSAPP = "+971 (50) 123-4567"
WA_DIGITS = "971501234567"  # non-digits stripped for the wa.me href


def _input_value(html: str, name: str) -> str:
    m = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    assert m, f"no value= on input {name}"
    return m.group(1)


def _textarea_value(html: str, name: str) -> str:
    m = re.search(
        rf'<textarea[^>]*name="{name}"[^>]*>(.*?)</textarea>', html, re.S
    )
    assert m, f"no textarea {name}"
    return m.group(1)


def test_contact_block_round_trip_links_and_byte_equality(arun, id_asgi):
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
                        "address": ADDRESS,
                        "phone": PHONE,
                        "contact_email": public_email,
                        "whatsapp": WHATSAPP,
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                # DRAFT preview already renders the whole contact block
                view = await w.get(saved.headers["location"])
                assert view.status_code == 200
                assert '<h2>Contact</h2>' in view.text
                assert "Suite 1404, Marina Plaza" in view.text
                assert "Dubai Marina, Dubai, UAE" in view.text
                assert f'href="tel:{PHONE}"' in view.text
                assert f'href="mailto:{public_email}"' in view.text
                assert f'href="https://wa.me/{WA_DIGITS}"' in view.text
                assert "wa.me/+" not in view.text  # href is digits-only
                assert WHATSAPP in view.text        # original shown as text
                assert "Chat on WhatsApp" in view.text
                # ZERO account-email leakage on the public-content surface
                assert email not in view.text

                # go live: the public page renders the SAME article,
                # byte for byte (31f contract holds with 31m content)
                await _verify_email_and_go_live(idc, email, sid)
                pub = await w.get(f"/{slug}")
                assert pub.status_code == 200
                assert '<h2>Contact</h2>' in pub.text
                assert f'href="tel:{PHONE}"' in pub.text
                assert f'href="mailto:{public_email}"' in pub.text
                assert f'href="https://wa.me/{WA_DIGITS}"' in pub.text
                assert "Chat on WhatsApp" in pub.text
                assert email not in pub.text       # no account-email markup
                blade = await w.get("/org/profile")
                assert _article(blade.text) == _article(pub.text)

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
                pub = await w.get(f"/{slug}")
                assert pub.status_code == 200
                assert "sv-org-contact" not in pub.text
                assert "<h2>Contact</h2>" not in pub.text
                blade = await w.get("/org/profile")
                assert "sv-org-contact" not in blade.text

                # one field set -> the section appears with only that field
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "", "address": ADDRESS},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                pub = await w.get(f"/{slug}")
                assert '<h2>Contact</h2>' in pub.text
                assert "Suite 1404, Marina Plaza" in pub.text
                article = _article(pub.text)
                assert "tel:" not in article
                assert "mailto:" not in article
                assert "wa.me" not in article

    arun(scenario())


def test_contact_email_never_prefilled_from_account_email(arun, id_asgi):
    """Surface separation: the editor's contact_email input starts EMPTY
    — the account/login email is private state and never seeds public
    fields."""
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
                assert _textarea_value(editor.text, "address") == ""
                for field in ("phone", "contact_email", "whatsapp"):
                    assert _input_value(editor.text, field) == "", field
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
                    "address": ADDRESS,
                    "phone": PHONE,
                    "contact_email": f"ops-{u}@harbor.example",
                    "whatsapp": WHATSAPP,
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

                # whatsapp with letters -> 422; atomic, stored value intact
                bad_wa = await w.post(
                    "/org/profile",
                    data={**good, "whatsapp": "call-me-now"},
                )
                assert bad_wa.status_code == 422
                assert "whatsapp" in bad_wa.text
                assert _input_value(bad_wa.text, "whatsapp") == "call-me-now"
                pub = await idc.get(f"/api/orgs/{slug}")
                assert pub.json()["whatsapp"] == WHATSAPP

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
                pub_page = await w.get(f"/{slug}")
                assert WHATSAPP in pub_page.text

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
                        "address": ADDRESS, "phone": PHONE,
                        "contact_email": f"ops-{u}@harbor.example",
                        "whatsapp": WHATSAPP,
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                cleared = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "", "address": "",
                          "phone": "", "contact_email": "", "whatsapp": ""},
                    follow_redirects=False,
                )
                assert cleared.status_code == 303
                pub = await idc.get(f"/api/orgs/{slug}")
                for field in ("address", "phone", "contact_email", "whatsapp"):
                    assert pub.json()[field] is None

                await _verify_email_and_go_live(idc, email, sid)
                pub_page = await w.get(f"/{slug}")
                assert "sv-org-contact" not in pub_page.text
                assert "<h2>Contact</h2>" not in pub_page.text

    arun(scenario())
