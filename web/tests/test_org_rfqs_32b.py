"""32b: two-channel RFQ — the web acceptance matrix.

- Public chain end to end: anonymous submit → 303 /register?next= →
  standard registration (org_type=client, verification LINK with next,
  login, OTP with next, trust-device box present) → redirect honors next
  (?ready=1 flash) → submit creates the RFQ (channel=public_page,
  requesting_org_id = the new client org) → confirmation with "Track
  your requests" → the CLIENT rail: RFQs section only, own data only.
- Tenant manual channel: linked-client RFQ + inline quick-create RFQ,
  channel/status badges, reference_no + vessel_name round-trip, client
  contact card with contact_person + designation, Quotes placeholder.
- RBAC matrix: owner/admin/ops see+manage; surveyor/viewer 404; the
  client role sees ONLY its own org's RFQs (never another client's —
  work-service org-scoping, page-level 404).
- SEO: title/meta description (about, ≤160)/OG tags on the public page.

Identity + work + web run in-process (web/tests/conftest.py seams).
"""
import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

from webapp.auth import SESSION_COOKIE

from test_profile_preview_ports import (
    _register_and_login,
    _verify_email,
    _verify_email_and_go_live,
    _web,
)

# ---------------------------------------------------------------- helpers

FORM = {
    "vessel_name": "MT Sea Falcon",
    "vessel_imo": "9404572",
    "survey_types": ["condition"],
    "location_type": "anchorage",
    "port_name": "Karachi anchorage",
    "preferred_date": "2026-11-01",
    "scope": "On-hire condition + bunker",
}


def _form(u: str) -> dict:
    return {
        "vessel_name": "MT Sea Falcon",
        "vessel_imo": "9404572",
        "survey_types": ["condition"],
        "location_type": "anchorage",
        "port_name": "Karachi anchorage",
        "preferred_date": "2026-11-01",
        "reference_no": f"PMS-QT-38-{u}",
        "scope": "On-hire condition + bunker",
    }


async def _sid_from_cookie(response) -> str:
    set_cookie = response.headers.get("set-cookie", "")
    return re.search(rf"{SESSION_COOKIE}=([^;]+)", set_cookie).group(1)


async def _login_web(idc: httpx.AsyncClient, w: httpx.AsyncClient,
                     email: str) -> None:
    """Verified-email login through the REAL web pages (password → OTP),
    leaving the session cookie on the web client."""
    start = await w.post("/login", data={"email": email, "password": "secret-123"})
    assert "Check your inbox" in start.text
    code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()["code"]
    ok = await w.post(
        "/login/otp", data={"email": email, "code": code},
        follow_redirects=False,
    )
    assert ok.status_code == 303, ok.text
    w.cookies.set(SESSION_COOKIE, await _sid_from_cookie(ok))


async def _live_tenant(idc, w, u: str, about: str = "Independent surveyors."):
    """A live survey company with a public slug; returns (email, slug)."""
    email = f"tnt-{u}@example.com"
    slug = f"tenant-{u}"
    sid = await _register_and_login(idc, w, email, slug)
    await _verify_email_and_go_live(idc, email, sid)
    saved = await w.post(
        "/org/profile",
        data={"slug": slug, "about": about},
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text[:300]
    return email, slug


async def _register_client_org(idc, w, u: str, tag: str):
    """A registered CLIENT org via the identity API + web login."""
    email = f"{tag}-{u}@example.com"
    reg = await idc.post("/api/register", json={
        "org_name": f"Client {tag} {u}", "org_type": "client",
        "email": email, "password": "secret-123",
        "confirm_password": "secret-123",
    })
    assert reg.status_code == 201, reg.text
    await _verify_email(email)
    await _login_web(idc, w, email)
    return email


def _last_verify_link(email: str) -> str:
    from app.mailer import get_mailer

    mail = next(m for m in reversed(get_mailer().outbox)
                if m.to == email and m.subject.startswith("Verify your SeaVix"))
    return next(l for l in mail.body_text.splitlines() if "oob_code=" in l).strip()


# ------------------------------------------------------ the public funnel

def test_public_funnel_end_to_end(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as wt:
                _, slug = await _live_tenant(idc, wt, u)

            async with _web() as w:
                # anonymous: the form renders, the submit guards to register
                form = await w.get(f"/org/{slug}/rfq")
                assert form.status_code == 200
                assert 'name="vessel_name"' in form.text
                assert 'name="port_name"' in form.text
                assert 'name="reference_no"' in form.text
                guard = await w.post(f"/org/{slug}/rfq", data=_form(u),
                                     follow_redirects=False)
                assert guard.status_code == 303
                assert guard.headers["location"] == f"/register?next=/org/{slug}/rfq"

                # register: the funnel preselects the ship-operator type and
                # threads `next` through every step
                reg_form = await w.get(f"/register?next=/org/{slug}/rfq")
                assert reg_form.status_code == 200
                assert 'name="next"' in reg_form.text
                assert 'value="client" selected' in reg_form.text
                email = f"funnel-{u}@example.com"
                step = await w.post(
                    "/register",
                    data={
                        "org_name": f"Dar Chartering {u}", "org_type": "client",
                        "email": email, "password": "secret-123",
                        "confirm_password": "secret-123",
                        "next": f"/org/{slug}/rfq",
                    },
                )
                assert "Check your inbox" in step.text
                assert 'name="next"' in step.text  # resend keeps it

                # the verification LINK carries next (the emailed deep link)
                link = _last_verify_link(email)
                qs = parse_qs(urlparse(link).query)
                assert qs["next"] == [f"/org/{slug}/rfq"]
                confirm_page = await w.get(
                    f"/register/verify/confirm?email={email}"
                    f"&oob_code={qs['oob_code'][0]}&next={qs['next'][0]}")
                assert confirm_page.status_code == 200
                assert 'name="next"' in confirm_page.text

                done = await w.post(
                    "/register/verify",
                    data={"email": email, "oob_code": qs["oob_code"][0],
                          "next": qs["next"][0]},
                )
                assert "verified" in done.text
                assert f"/login?next=/org/{slug}/rfq" in done.text

                # login → OTP; both carry next; the trust-device box exists
                otp_page = await w.post(
                    "/login", data={"email": email, "password": "secret-123",
                                    "next": f"/org/{slug}/rfq"})
                assert 'name="next"' in otp_page.text
                assert 'name="trust_device"' in otp_page.text
                code = (await idc.get("/api/dev/last-otp",
                                      params={"email": email})).json()["code"]
                landed = await w.post(
                    "/login/otp",
                    data={"email": email, "code": code,
                          "next": f"/org/{slug}/rfq"},
                    follow_redirects=False,
                )
                assert landed.status_code == 303
                assert landed.headers["location"] == f"/org/{slug}/rfq?ready=1"
                w.cookies.set(SESSION_COOKIE, await _sid_from_cookie(landed))

                # the form re-renders with the success flash; submit works
                back = await w.get(f"/org/{slug}/rfq?ready=1")
                assert back.status_code == 200
                assert "Account ready — submit your RFQ" in back.text

                from webapp import pages
                pages.limiter._hits.clear()
                thanks = await w.post(f"/org/{slug}/rfq", data=_form(u))
                assert thanks.status_code == 200
                assert "Thank you" in thanks.text
                assert "Track your requests" in thanks.text

                # the CLIENT rail: RFQs section only, own data only
                mine = await w.get("/org/rfqs")
                assert mine.status_code == 200
                assert f"PMS-QT-38-{u}" in mine.text
                assert "Awaiting quote" in mine.text
                rail = mine.text.split('class="sv-rail"')[1].split('class="sv-page"')[0]
                assert 'href="/org/rfqs"' in rail
                assert ">Dashboard</span>" not in rail
                assert 'href="/org/profile"' not in rail
                assert 'href="/org/clients"' not in rail

            # work row: channel + requesting org provenance
            from app.db import SessionLocal as IdSession
            from sqlalchemy import text as sql

            from work.db import SessionLocal

            async with IdSession() as ids:
                client_org = (await ids.execute(
                    sql("SELECT o.id FROM organization o "
                        "JOIN membership m ON m.org_id = o.id "
                        "JOIN app_user u ON u.id = m.user_id "
                        "WHERE u.email = :e"), {"e": email})).scalar_one()
            async with SessionLocal() as s:
                row = (await s.execute(
                    sql("SELECT channel, status, requesting_org_id, "
                        "vessel_name, reference_no, port_name "
                        "FROM rfq WHERE reference_no = :r"),
                    {"r": f"PMS-QT-38-{u}"})).mappings().one()
                assert row["channel"] == "public_page"
                assert row["status"] == "awaiting_quote"
                assert str(row["requesting_org_id"]) == str(client_org)
                assert row["vessel_name"] == "MT Sea Falcon"
                assert row["port_name"] == "Karachi anchorage"

    arun(scenario())


# --------------------------------------------------------- manual channel

CLIENT_FORM = {
    "name": "Dar Shipping",
    "relationship_type": "management",
    "email": "",
    "contact_person": "Capt. Amin Dar",
    "designation": "Managing Director",
    "contact_email": "captamindar@example.com",
}


def test_manual_channel_linked_client(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                await _register_and_login(idc, w, f"lnk-{u}@example.com",
                                          f"ops-{u}")

                # the client gains contact_person + designation (32a amend)
                saved = await w.post(
                    "/org/clients/new",
                    data={**CLIENT_FORM, "email": f"dar-{u}@example.com"},
                    follow_redirects=False,
                )
                assert saved.status_code == 303
                detail_url = saved.headers["location"]
                detail = await w.get(detail_url)
                assert "Capt. Amin Dar" in detail.text
                assert "Managing Director" in detail.text

                # empty pipeline renders the honest empty state
                listing = await w.get("/org/rfqs")
                assert listing.status_code == 200
                assert "No RFQs yet" in listing.text
                assert 'href="/org/rfqs/new"' in listing.text

                # raise form: channel radios + client picker + quick-create
                form = await w.get("/org/rfqs/new")
                assert form.status_code == 200
                assert 'value="manual_whatsapp"' in form.text
                assert 'value="manual_email"' in form.text
                assert "rfqClientPicker" in form.text
                assert 'name="client_mode"' in form.text
                assert 'name="new_name"' in form.text  # inline quick-create

                options = await w.get("/org/rfqs/clients.json")
                assert options.status_code == 200
                clients = options.json()["clients"]
                assert [c["name"] for c in clients] == ["Dar Shipping"]
                edge_id = clients[0]["id"]

                raised = await w.post(
                    "/org/rfqs/new",
                    data={
                        "channel": "manual_whatsapp",
                        "client_mode": "existing",
                        "client_id": edge_id,
                        "vessel_name": "MV Linked",
                        "vessel_imo": "9074729",
                        "survey_types": ["condition", "bunker_quantity"],
                        "location_type": "terminal",
                        "port_name": "Fujairah",
                        "preferred_date": "2026-12-01",
                        "reference_no": "PMS-QT-28-2026",
                        "scope": "Condition + bunker on hire",
                    },
                    follow_redirects=False,
                )
                assert raised.status_code == 303, raised.text[:300]
                rfq_url = raised.headers["location"]
                assert rfq_url.startswith("/org/rfqs/")

                # detail: fields + badges + client contact card + Quotes
                # section (32c: raise CTA + empty state, no rows yet)
                page = await w.get(rfq_url)
                assert page.status_code == 200
                assert "PMS-QT-28-2026" in page.text
                assert "MV Linked" in page.text
                assert "Awaiting quote" in page.text
                assert "WhatsApp" in page.text
                assert "Capt. Amin Dar" in page.text
                assert "Managing Director" in page.text
                assert "Pricing revisions for this RFQ." in page.text
                assert 'href="/org/quotes/raise?rfq_id=' in page.text
                assert "No quotes yet" in page.text
                assert "Condition survey" in page.text
                assert "Bunker quantity survey" in page.text

                # list: badges + channel + every column
                listing = await w.get("/org/rfqs")
                assert "PMS-QT-28-2026" in listing.text
                assert "Dar Shipping" in listing.text
                assert "WhatsApp" in listing.text
                assert "Awaiting quote" in listing.text
                assert "Fujairah" in listing.text

                # round-trip through the work read model
                rfq_id = rfq_url.split("/org/rfqs/")[1].split("?")[0]
                from sqlalchemy import text as sql

                from work.db import SessionLocal

                async with SessionLocal() as s:
                    row = (await s.execute(
                        sql("SELECT channel, client_id, vessel_name, "
                            "reference_no, port_name, status "
                            "FROM rfq WHERE id = :id"),
                        {"id": rfq_id})).mappings().one()
                    assert row["channel"] == "manual_whatsapp"
                    assert str(row["client_id"]) == edge_id
                    assert row["vessel_name"] == "MV Linked"
                    assert row["reference_no"] == "PMS-QT-28-2026"
                    assert row["port_name"] == "Fujairah"
                    assert row["status"] == "awaiting_quote"

                # cross-tenant edge ids are rejected by the work API (404)
                bad = await w.post(
                    "/org/rfqs/new",
                    data={
                        "channel": "manual_email", "client_mode": "existing",
                        "client_id": str(uuid.uuid4()),
                        "vessel_name": "X", "survey_types": ["condition"],
                        "location_type": "terminal",
                    },
                )
                assert bad.status_code == 404

    arun(scenario())


def test_manual_channel_inline_quick_create(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                await _register_and_login(idc, w, f"qc-{u}@example.com",
                                          f"qc-{u}")

                raised = await w.post(
                    "/org/rfqs/new",
                    data={
                        "channel": "manual_email", "client_mode": "new",
                        "new_name": "Vanguard Tech",
                        "new_relationship": "management",
                        "new_email": f"asif-{u}@example.com",
                        "vessel_name": "MV Quick",
                        "survey_types": ["pre_purchase"],
                        "location_type": "STS",
                        "reference_no": "VAN-1",
                    },
                    follow_redirects=False,
                )
                assert raised.status_code == 303, raised.text[:400]
                page = await w.get(raised.headers["location"])
                assert "Vanguard Tech" in page.text
                assert "VAN-1" in page.text
                assert "Email" in page.text  # contact card carries the email

                # unclaimed org + edge landed in the same save
                from sqlalchemy import text as sql

                from work.db import SessionLocal

                async with SessionLocal() as s:
                    edge = (await s.execute(
                        sql("SELECT client_org_id FROM client_directory "
                            "ORDER BY created_at DESC LIMIT 1"))).scalar_one()
                from app.db import SessionLocal as IdSession

                async with IdSession() as ids:
                    org = (await ids.execute(
                        sql("SELECT name, type, contact_email, contact_person "
                            "FROM organization WHERE id = :o"),
                        {"o": edge})).mappings().one()
                    assert org["name"] == "Vanguard Tech"
                    assert org["type"] == "client"
                    assert org["contact_email"] == f"asif-{u}@example.com"

                # validation: missing vessel / survey types re-renders 422
                bad = await w.post(
                    "/org/rfqs/new",
                    data={"channel": "manual_email", "client_mode": "new",
                          "new_name": "", "new_relationship": "",
                          "new_email": "", "vessel_name": "",
                          "survey_types": []},
                )
                assert bad.status_code == 422
                assert "vessel name is required" in bad.text

    arun(scenario())


# ----------------------------------------------------------------- matrix

async def _same_org_member(idc, org_id: str, role_name: str, email: str) -> str:
    """Add a SECOND user to an existing org directly in the identity DB
    (the claim-shaped seam the identity tests established) and return
    their session id. The org's system roles were seeded at registration.
    """
    import uuid as _uuid

    from sqlalchemy import text as sql

    from app.db import SessionLocal
    from app.rbac import role_id_by_name

    async with httpx.AsyncClient(base_url="http://localhost:9099") as fc:
        fb = await fc.post(
            "/identitytoolkit.googleapis.com/v1/accounts:signUp",
            params={"key": "demo-any-key"},
            json={"email": email, "password": "secret-123",
                  "returnSecureToken": True},
        )
    uid = str(_uuid.uuid4())
    async with SessionLocal() as sess:
        rid = await role_id_by_name(sess, org_id, role_name)
        await sess.execute(
            sql("INSERT INTO app_user (id, email, firebase_uid, "
                "email_verified) VALUES (:id, :email, :fb, true)"),
            {"id": uid, "email": email, "fb": fb.json()["localId"]},
        )
        await sess.execute(
            sql("INSERT INTO membership (user_id, org_id, role) "
                "VALUES (:uid, :org, :label)"),
            {"uid": uid, "org": org_id, "label": role_name},
        )
        await sess.execute(
            sql("INSERT INTO membership_role (user_id, org_id, role_id) "
                "VALUES (:uid, :org, :rid)"),
            {"uid": uid, "org": org_id, "rid": rid},
        )
        await sess.commit()
    start = await idc.post("/api/auth/login/start",
                           json={"email": email, "password": "secret-123"})
    assert start.status_code == 200, start.text
    code = (await idc.get("/api/dev/last-otp",
                          params={"email": email})).json()["code"]
    done = await idc.post("/api/auth/login/verify",
                          json={"email": email, "code": code})
    assert done.status_code == 200, done.text
    return done.json()["session_id"]


def test_rfq_pages_rbac_matrix(arun, id_asgi):
    """owner/admin/ops of the SAME org see + manage; surveyor/viewer of
    that org 404 everywhere; the raise form POST is gated the same way."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            org_email = f"matrix-{u}@example.com"
            reg = await idc.post("/api/register", json={
                "org_name": "Matrix Org", "org_type": "survey_company",
                "email": org_email, "password": "secret-123",
                "confirm_password": "secret-123",
            })
            assert reg.status_code == 201, reg.text
            org_id = reg.json()["org_id"]
            await _verify_email(org_email)

            start = await idc.post("/api/auth/login/start",
                                   json={"email": org_email,
                                         "password": "secret-123"})
            assert start.status_code == 200, start.text
            code = (await idc.get("/api/dev/last-otp",
                                  params={"email": org_email})).json()["code"]
            done = await idc.post("/api/auth/login/verify",
                                  json={"email": org_email, "code": code})
            assert done.status_code == 200, done.text
            owner_sid = done.json()["session_id"]
            async with _web() as w:
                w.cookies.set(SESSION_COOKIE, owner_sid)
                c = await w.post("/org/clients/new",
                                 data={**CLIENT_FORM,
                                       "email": f"mx-{u}@example.com"},
                                 follow_redirects=False)
                assert c.status_code == 303, c.text[:300]
                edge = c.headers["location"].rsplit("/", 1)[1].split("?")[0]
                made = await w.post(
                    "/org/rfqs/new",
                    data={"channel": "manual_email", "client_mode": "existing",
                          "client_id": edge, "vessel_name": "MV Matrix",
                          "survey_types": ["condition"],
                          "location_type": "terminal"},
                    follow_redirects=False,
                )
                assert made.status_code == 303, made.text[:400]
                rfq_id = made.headers["location"].split("/org/rfqs/")[1] \
                                                 .split("?")[0]

            sessions = {"owner": owner_sid}
            for role in ("admin", "ops", "surveyor", "viewer"):
                sessions[role] = await _same_org_member(
                    idc, org_id, role, f"matrix-{role}-{u}@example.com")

            for role, expect in (("owner", 200), ("admin", 200),
                                 ("ops", 200), ("surveyor", 404),
                                 ("viewer", 404)):
                async with _web() as w:
                    w.cookies.set(SESSION_COOKIE, sessions[role])
                    listing = await w.get("/org/rfqs")
                    assert listing.status_code == expect, role
                    new = await w.get("/org/rfqs/new")
                    assert new.status_code == expect, role
                    detail = await w.get(f"/org/rfqs/{rfq_id}")
                    assert detail.status_code == expect, role
                    if expect == 200:
                        assert "MV Matrix" in detail.text
                        rail = listing.text.split('class="sv-rail"')[1] \
                                   .split('class="sv-page"')[0]
                        assert 'href="/org/rfqs"' in rail
                    else:
                        post = await w.post(
                            "/org/rfqs/new",
                            data={"channel": "manual_email",
                                  "client_mode": "existing", "client_id": edge,
                                  "vessel_name": "X",
                                  "survey_types": ["condition"],
                                  "location_type": "terminal"},
                        )
                        assert post.status_code == 404, role

    arun(scenario())


def test_client_role_sees_only_its_own_rfqs(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as wt:
                tenant_email, slug = await _live_tenant(idc, wt, u)

            # client A raises its RFQ, then stays signed in
            async with _web() as wa:
                await _register_client_org(idc, wa, u, "a")
                from webapp import pages
                pages.limiter._hits.clear()
                ref_a = f"REF-A-{u}"
                thanks_a = await wa.post(
                    f"/org/{slug}/rfq",
                    data={**FORM, "reference_no": ref_a,
                          "vessel_name": "MV A"},
                )
                assert "Track your requests" in thanks_a.text

                # client B (separate browser) raises its own
                async with _web() as wb:
                    await _register_client_org(idc, wb, u, "b")
                    pages.limiter._hits.clear()
                    ref_b = f"REF-B-{u}"
                    thanks_b = await wb.post(
                        f"/org/{slug}/rfq",
                        data={**FORM, "reference_no": ref_b,
                              "vessel_name": "MV B"},
                    )
                    assert "Track your requests" in thanks_b.text

                from sqlalchemy import text as sql

                from work.db import SessionLocal

                async with SessionLocal() as s:
                    ids = {}
                    for key, ref in (("a", ref_a), ("b", ref_b)):
                        ids[key] = str((await s.execute(
                            sql("SELECT id FROM rfq WHERE reference_no = :r"),
                            {"r": ref})).scalar_one())
                rfq_a, rfq_b = ids["a"], ids["b"]

                # A: own list only; B's detail is a plain 404; the raise
                # form does not exist for the client role
                mine = await wa.get("/org/rfqs")
                assert mine.status_code == 200
                assert ref_a in mine.text
                assert ref_b not in mine.text
                assert "MV B" not in mine.text
                other = await wa.get(f"/org/rfqs/{rfq_b}")
                assert other.status_code == 404
                mine_detail = await wa.get(f"/org/rfqs/{rfq_a}")
                assert mine_detail.status_code == 200
                assert ref_a in mine_detail.text
                assert "Preview Org" in mine_detail.text  # the survey company
                raise_form = await wa.get("/org/rfqs/new")
                assert raise_form.status_code == 404

            # the tenant sees both, with channel + status badges
            async with _web() as w:
                await _login_web(idc, w, tenant_email)
                listing = await w.get("/org/rfqs")
                assert ref_a in listing.text
                assert ref_b in listing.text
                assert "Public page" in listing.text
                assert "Awaiting quote" in listing.text

    arun(scenario())


# -------------------------------------------------------------------- seo

def test_public_org_page_seo(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            about = ("Precision marine survey coordination across the Gulf, "
                     "Indian Ocean and beyond — pre-purchase, condition and "
                     "bunker surveys by certified attending surveyors.")
            async with _web() as w:
                email, slug = await _live_tenant(idc, w, u, about=about)
                page = await w.get(f"/org/{slug}")
                assert page.status_code == 200
                title = "Preview Org — Marine survey services | SeaVix"
                assert f"<title>{title}</title>" in page.text
                assert f'<meta name="description" content="{about[:160]}' \
                    in page.text or f'<meta name="description" content="{about}"' in page.text
                assert f'<meta property="og:title" content="{title}">' \
                    in page.text
                assert '<meta property="og:description"' in page.text
                assert f'<meta property="og:url" content="http://web/org/{slug}">' \
                    in page.text
                assert "og:image" not in page.text  # no logo yet

                # a long about is truncated to 160 for the snippet
                long_about = "word " * 60
                await w.post("/org/profile",
                             data={"slug": slug, "about": long_about},
                             follow_redirects=False)
                page = await w.get(f"/org/{slug}")
                import re
                m = re.search(r'<meta name="description" content="([^"]*)"',
                              page.text)
                assert len(m.group(1)) <= 160

                # logo -> og:image with the absolute media URL
                png = bytes.fromhex(
                    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c626001000000ffff030000060005"
                    "57bfabd40000000049454e44ae426082")
                up = await w.post("/org/profile/logo",
                                  files={"file": ("logo.png", png,
                                                  "image/png")},
                                  follow_redirects=False)
                assert up.status_code == 200, up.text[:300]
                page = await w.get(f"/org/{slug}")
                assert 'property="og:image"' in page.text
                assert f'content="http://web/media/logos/' in page.text

    arun(scenario())
