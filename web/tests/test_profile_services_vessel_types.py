"""31q: profile services + vessel types end-to-end, and the edit-mode
preview-aside removal.

- Both fields round-trip through the web Save (chips combobox over the
  identity catalogs via /org/services/search + /org/vessel-types/search),
  render as chip sections on the view-mode preview AND the live public
  page ("Services" after the description, "Vessel types we serve" after
  Services, both before Contact — hidden until set), and the 31f
  byte-equality contract keeps holding through the shared partial.
- EDIT mode is now a FULL-WIDTH form with NO preview aside (view mode is
  unchanged — it still renders the WYSIWYG preview).
- Dirty-tracking: add/remove chip marks the form dirty in all three
  pickers; typing in a combobox search box alone does NOT.
- Catalog labels render escaped (UGC containment) even if a catalog row
  ever carries markup.

Identity + registry + web run in-process on the shared loop (see
web/tests/conftest.py); the registry carries the real UN/LOCODE import.
"""
import re
import uuid

import httpx

from webapp.auth import SESSION_COOKIE

from test_profile_preview_ports import (
    _pin_ports,
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)

SERVICES = ["draft_survey", "condition_survey", "bunker_quantity_survey"]
VESSEL_TYPES = ["oil_tanker", "bulk_carrier"]
LABELS = {
    "draft_survey": "Draft survey",
    "condition_survey": "Condition survey",
    "bunker_quantity_survey": "Bunker quantity survey",
    "oil_tanker": "Oil tanker",
    "bulk_carrier": "Bulk carrier",
}


def _article(html: str) -> str:
    match = re.search(r"<article[^>]*>.*?</article>", html, re.S)
    assert match, "no <article> rendered"
    return match.group(0)


def test_services_vessel_types_round_trip_and_preview(arun, id_asgi):
    async def scenario():
        await _pin_ports()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"sv-{u}@example.com"
            slug = f"svc-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)

                # absent-until-set: a fresh org renders neither section
                view0 = await w.get("/org/profile")
                assert "sv-org-services" not in view0.text
                assert "sv-org-vessel-types" not in view0.text
                assert "Vessel types we serve" not in view0.text

                # Save about + both fields + a port in one POST
                saved = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.",
                        "phone_cc": "+971",
                        "phone": "50 123 4567",
                        "services": SERVICES,
                        "vessel_types": VESSEL_TYPES,
                        "ports": ["AEFJR"],
                    },
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                view = await w.get(saved.headers["location"])
                assert view.status_code == 200
                # sections render on the preview, in spec order:
                # description < Services < Vessel types < Ports < Contact
                assert "sv-org-services" in view.text
                assert "sv-org-vessel-types" in view.text
                assert "sv-org-ports" in view.text
                order = (
                    view.text.index("sv-prose"),
                    view.text.index("sv-org-services"),
                    view.text.index("sv-org-vessel-types"),
                    view.text.index("sv-org-ports"),
                    view.text.index('sv-org-contact'),
                )
                assert order == tuple(sorted(order))
                for code in SERVICES + VESSEL_TYPES:
                    assert LABELS[code] in view.text

                # EDIT MODE seeds all three pickers with the persisted
                # selection (JSON in x-data) — and renders NO preview
                # aside (31q): the sv-chip occurrences are the three
                # chip TEMPLATES only, and no <article> exists at all
                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                assert "sv-editor-preview" not in editor.text
                assert "sv-preview-hint" not in editor.text
                assert "<article" not in editor.text
                assert 'class="card sv-preview"' not in editor.text
                # ... while VIEW mode keeps the WYSIWYG preview untouched
                assert 'class="card sv-preview"' in view.text
                assert "<article" in view.text
                # four pickers, four chip templates (32d: + survey types)
                assert editor.text.count('class="sv-chip-picker"') == 4
                assert editor.text.count('class="sv-chip"') == 4
                assert editor.text.count('class="sv-chips"') == 4
                for code in SERVICES:
                    assert f'"code": "{code}"' in editor.text
                for code in VESSEL_TYPES:
                    assert f'"code": "{code}"' in editor.text
                assert f'"code": "AEFJR"' in editor.text
                for code, label in LABELS.items():
                    assert f'"label": "{label}"' in editor.text
                assert editor.text.count('name="services"') == 1
                assert editor.text.count('name="vessel_types"') == 1
                assert editor.text.count('name="ports"') == 1

                # combobox backends (label substring + code substring)
                # 32d: entries carry origin for the dropdown badge
                by_label = await w.get("/org/services/search?q=draft")
                assert {"code": "draft_survey", "label": "Draft survey",
                        "origin": "system"} in by_label.json()["services"]
                by_code = await w.get("/org/vessel-types/search?q=BULK")
                assert {"code": "bulk_carrier", "label": "Bulk carrier",
                        "origin": "system"} in by_code.json()["vessel_types"]

                # chip removal round-trips: re-Save WITHOUT bulk_carrier
                saved2 = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.",
                        "phone_cc": "+971",
                        "phone": "50 123 4567",
                        "services": SERVICES,
                        "vessel_types": ["oil_tanker"],
                        "ports": ["AEFJR"],
                    },
                    follow_redirects=False,
                )
                assert saved2.status_code == 303
                view2 = await w.get(saved2.headers["location"])
                assert "Bulk carrier" not in view2.text
                assert "Oil tanker" in view2.text
                for code in SERVICES:
                    assert LABELS[code] in view2.text

                # clear both fields entirely -> sections vanish again
                saved3 = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.",
                        "phone_cc": "+971",
                        "phone": "50 123 4567",
                        "services": [],
                        "vessel_types": [],
                        "ports": ["AEFJR"],
                    },
                    follow_redirects=False,
                )
                assert saved3.status_code == 303
                view3 = await w.get(saved3.headers["location"])
                assert "sv-org-services" not in view3.text
                assert "sv-org-vessel-types" not in view3.text
                assert "sv-org-ports" in view3.text  # port still set

                # restore, go live, public page renders the SAME partial —
                # the articles must be identical (31f byte-equality holds)
                saved4 = await w.post(
                    "/org/profile",
                    data={
                        "slug": slug,
                        "about": "Independent marine surveyors.",
                        "phone_cc": "+971",
                        "phone": "50 123 4567",
                        "services": SERVICES,
                        "vessel_types": VESSEL_TYPES,
                        "ports": ["AEFJR"],
                    },
                    follow_redirects=False,
                )
                assert saved4.status_code == 303
                await _verify_email_and_go_live(idc, email, sid)
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert "sv-org-services" in pub.text
                assert "sv-org-vessel-types" in pub.text
                for code in SERVICES + VESSEL_TYPES:
                    assert LABELS[code] in pub.text
                blade = await w.get("/org/profile")
                assert _article(blade.text) == _article(pub.text)

    arun(scenario())


def test_unknown_service_code_rejected_inline(arun, id_asgi):
    """Identity validates against its own catalog; the web surfaces the
    422 next to the services picker and NOTHING is persisted (the
    previously saved selection is untouched)."""

    async def scenario():
        await _pin_ports()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"sx-{u}@example.com"
            slug = f"svx-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                ok = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "",
                          "services": ["draft_survey"]},
                    follow_redirects=False,
                )
                assert ok.status_code == 303

                bad = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "",
                          "services": ["no_such_service"]},
                )
                assert bad.status_code == 422
                assert "unknown service: no_such_service" in bad.text
                assert 'name="services"' in bad.text  # edit mode re-render
                pub = await idc.get(f"/api/orgs/{slug}")
                assert pub.json()["services"] == [
                    {"code": "draft_survey", "label": "Draft survey",
                     "origin": "system"}
                ]

                # mixed known + unknown on vessel types: whole save 422s
                mixed = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "",
                          "vessel_types": ["oil_tanker", "hovercraft"]},
                )
                assert mixed.status_code == 422
                assert "unknown vessel type: hovercraft" in mixed.text
                pub = await idc.get(f"/api/orgs/{slug}")
                assert pub.json()["vessel_types"] == []

    arun(scenario())


def test_catalog_comboboxes_require_session(arun):
    async def scenario():
        async with _web() as w:
            for path in ("/org/services/search?q=draft",
                         "/org/vessel-types/search?q=tank"):
                r = await w.get(path, follow_redirects=False)
                assert r.status_code == 303
                assert r.headers["location"] == "/login"

    arun(scenario())


def test_edit_mode_full_width_no_preview_aside(arun, id_asgi):
    """31q §6 amendment: edit mode = full-width form, no preview aside.
    View mode is unchanged (preview + Edit button + Live-page tab)."""
    async def scenario():
        await _pin_ports()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"fw-{u}@example.com"
            slug = f"fw-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "Full width."},
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                view = await w.get("/org/profile")
                assert view.status_code == 200
                assert 'class="card sv-preview"' in view.text
                assert '<article' in view.text
                assert 'href="/org/profile?view=edit"' in view.text

                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                assert "sv-editor-preview" not in editor.text
                assert "sv-editor-layout" not in editor.text
                assert "sv-preview-hint" not in editor.text
                assert "<article" not in editor.text
                # the form + all section cards are present, full width
                assert 'action="/org/profile"' in editor.text
                assert editor.text.count('class="sv-section-card"') == 4
                assert 'class="sv-save-bar"' in editor.text
                assert 'href="/org/profile">← Back to preview</a>' \
                    in editor.text

    arun(scenario())


def test_dirty_tracking_counts_all_pickers():
    """All four pickers' add()/remove() mark the form dirty; the
    debounced search functions never touch dirty (search alone is
    clean — the sticky Save bar must not appear). 32d: creating an
    entry adds a chip via the same addX(), so it is dirty too."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1]
            / "templates" / "org_profile.html").read_text()
    assert text.count("this.dirty = true") == 8  # 4 pickers x (add, remove)
    for fn in ("search()", "searchServices()", "searchVesselTypes()",
               "searchSurveyTypes()"):
        body = re.search(
            rf"async {re.escape(fn)} \{{(.*?)\n                  \}},", text,
            re.S,
        )
        assert body, fn
        assert "dirty" not in body.group(1), fn
    assert text.count("@input.debounce.300ms") == 4
    # 32d: the Create row exists on the three CATALOG pickers only —
    # ports are registry-owned UN/LOCODE reference data and never offer
    # creation (contract): sv-picker-create appears exactly 3 times and
    # never inside the ports picker block.
    assert text.count("sv-picker-create") == 3
    ports_block = text[text.index('id="port-search"'):]
    ports_block = ports_block[:ports_block.index("</div>", ports_block.index("sv-chips"))]
    assert "sv-picker-create" not in ports_block


def test_catalog_labels_render_escaped(arun, id_asgi):
    """UGC containment on catalog labels: if a catalog row ever carries
    markup (future admin-editing surface), it renders inert on the
    preview AND the public page."""
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async def scenario():
        await _pin_ports()
        async with SessionLocal() as s:
            await s.execute(
                sql(
                    "INSERT INTO service (service_code, label, sort_order) "
                    "VALUES ('xss_probe', :label, 99) "
                    "ON CONFLICT (service_code) DO UPDATE SET "
                    "label = EXCLUDED.label"
                ),
                {"label": "<script>alert('x')</script>Probe"},
            )
            await s.commit()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=id_asgi),
                base_url="http://id",
            ) as idc:
                u = uuid.uuid4().hex[:8]
                email = f"ug-{u}@example.com"
                slug = f"ug-{u}"
                async with _web() as w:
                    sid = await _register_and_login(idc, w, email, slug)
                    saved = await w.post(
                        "/org/profile",
                        data={"slug": slug, "about": "",
                              "services": ["xss_probe"]},
                        follow_redirects=False,
                    )
                    assert saved.status_code == 303
                    view = await w.get(saved.headers["location"])
                    assert "<script>alert" not in view.text
                    # Jinja interpolation escapes via markupsafe (&#39;),
                    # same as every other {{ }} render point
                    assert ("&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;Probe"
                            in view.text)
                    await _verify_email_and_go_live(idc, email, sid)
                    pub = await w.get(f"/org/{slug}")
                    assert "<script>alert" not in pub.text
                    assert ("&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;Probe"
                            in pub.text)
        finally:
            async with SessionLocal() as s:
                await s.execute(
                    sql("DELETE FROM service WHERE service_code = 'xss_probe'")
                )
                await s.commit()

    arun(scenario())
