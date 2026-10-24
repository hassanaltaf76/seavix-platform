"""32c: quotations — the web acceptance matrix.

- RBAC matrix: owner/admin/ops of a survey company see the rail Quotes
  slot and reach list/raise/options; surveyor/viewer 404 everywhere with
  the rail item ABSENT; a registered-client org 404s on every quote page
  and never sees the rail item.
- List: a draft quote shows quote no / client / vessel / total+currency /
  'Draft'; after approve-issue the same row reads 'Issued'.
- Raise flow: prefill (client, vessel, reference, currency, org default
  terms, bank block from Commercial defaults) -> draft save -> 303
  ?created=1 -> the RFQ is UNTOUCHED (still awaiting_quote — a plain
  draft save never issues) -> the document page renders every block ->
  edit PUT round-trips and the server total recalculates.
- RFQ picker feed: only awaiting_quote/quoted RFQs targeted at the tenant.
- Approve & issue: the human gate page, issue -> RFQ quoted, a second
  POST comes back with the error flash, edit is not offered on an issued
  quote, a revision spawns a fresh draft while the prior shows
  'Superseded' and the RFQ stays quoted.
- Cross-tenant: another org's quote id is a plain 404.
- Client view: the RFQ detail shows quote status read-only — no /org/quotes
  links, no raise button.
- Commercial defaults: page round-trip (prefix/terms/bank), the prefix
  feeds the next quote number, a bad prefix re-renders with the field
  error; surveyor 404s.
- UGC: a 200-char unbroken client name and line description render inside
  .sv-ugc elements.

Identity + work + web run in-process (web/tests/conftest.py seams).
"""
import re
import uuid

import httpx
import pytest

from webapp.auth import SESSION_COOKIE

from test_org_rfqs_32b import (
    CLIENT_FORM,
    FORM,
    _live_tenant,
    _register_client_org,
    _same_org_member,
)
from test_profile_preview_ports import _verify_email, _web

# ---------------------------------------------------------------- blocker
# RESOLVED (task 32c): identity's /api/internal/auth-context top-level `uid`
# is the Firebase uid (28 chars) — not a UUID — and 0005 typed the
# quote.prepared_by / approved_by / issued_by stamps as UUID, so every real
# quote write died inside work. Migration 0006 widened the three columns to
# TEXT (the Firebase uid is the canonical cross-service user identity).

# ------------------------------------------------------------- constants

QUOTE_FORM = {
    "quote_date": "2026-10-24",
    "valid_until": "2026-11-23",
    "currency": "USD",
    "li_desc": ["On-hire condition survey attendance", "Travel expenses"],
    "li_amt": ["1500.00", "450"],
    "terms": ["50% advance payment is required before mobilization."],
    "bank_name": "Habib Bank", "branch": "Karachi",
    "account_name": "Pak Marine Surveys", "account_number": "PK36HBOK0001",
    "iban": "", "swift": "",
}


def _rail(html: str) -> str:
    return html.split('class="sv-rail"')[1].split('class="sv-page"')[0]


def _quote_id(redirect_location: str) -> str:
    # /org/quotes/<id>?created=1
    return redirect_location.split("/org/quotes/")[1].split("?")[0]


async def _tenant_with_client(idc, w, u: str):
    """A live tenant with one directory client; returns
    (tenant_email, slug, edge_id) — the 32b helper flow, reused here."""
    tenant_email, slug = await _live_tenant(idc, w, u)
    saved = await w.post(
        "/org/clients/new",
        data={**CLIENT_FORM, "email": f"q-{u}@example.com"},
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text[:300]
    options = await w.get("/org/rfqs/clients.json")
    edge = options.json()["clients"][0]["id"]
    return tenant_email, slug, edge


async def _raise_rfq(w, edge: str, ref: str, vessel: str = "MV Listed"):
    raised = await w.post(
        "/org/rfqs/new",
        data={"channel": "manual_email", "client_mode": "existing",
              "client_id": edge, "vessel_name": vessel,
              "vessel_imo": "9074729", "survey_types": ["condition"],
              "location_type": "terminal", "port_name": "Karachi",
              "preferred_date": "2026-11-01", "reference_no": ref,
              "scope": "On-hire condition survey."},
        follow_redirects=False,
    )
    assert raised.status_code == 303, raised.text[:400]
    return raised.headers["location"].split("/org/rfqs/")[1].split("?")[0]


async def _tenant_client_rfq(idc, w, u: str, ref: str,
                             vessel: str = "MV Listed"):
    """The common single-RFQ fixture: (tenant_email, slug, rfq_id, edge)."""
    tenant_email, slug, edge = await _tenant_with_client(idc, w, u)
    rfq_id = await _raise_rfq(w, edge, ref, vessel)
    return tenant_email, slug, rfq_id, edge


async def _raise_draft(w, rfq_id: str, form: dict | None = None):
    data = {**QUOTE_FORM, "rfq_id": rfq_id, **(form or {})}
    resp = await w.post("/org/quotes/raise", data=data,
                        follow_redirects=False)
    assert resp.status_code == 303, resp.text[:400]
    assert resp.headers["location"].endswith("?created=1")
    return _quote_id(resp.headers["location"])


async def _issue(w, quote_id: str):
    issued = await w.post(f"/org/quotes/{quote_id}/issue",
                          data={"confirm": "true"},
                          follow_redirects=False)
    assert issued.status_code == 303, issued.text[:300]
    assert issued.headers["location"].endswith("?issued=1")


async def _login(idc, w, email: str) -> str:
    """Identity-API login: returns the session id and leaves the web
    cookie set."""
    start = await idc.post("/api/auth/login/start",
                           json={"email": email, "password": "secret-123"})
    assert start.status_code == 200, start.text
    code = (await idc.get("/api/dev/last-otp",
                          params={"email": email})).json()["code"]
    done = await idc.post("/api/auth/login/verify",
                          json={"email": email, "code": code})
    assert done.status_code == 200, done.text
    sid = done.json()["session_id"]
    w.cookies.set(SESSION_COOKIE, sid)
    return sid


# ----------------------------------------------------------------- matrix

def test_quotes_pages_rbac_matrix(arun, id_asgi):
    """owner/admin/ops see + manage; surveyor/viewer 404 everywhere; the
    client role 404s on every quote page. The rail Quotes item renders
    ONLY for the permitted tenant roles."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            org_email = f"qmatrix-{u}@example.com"
            reg = await idc.post("/api/register", json={
                "org_name": "Quotes Matrix Org", "org_type": "survey_company",
                "email": org_email, "password": "secret-123",
                "confirm_password": "secret-123",
            })
            assert reg.status_code == 201, reg.text
            org_id = reg.json()["org_id"]
            await _verify_email(org_email)
            start = await idc.post("/api/auth/login/start",
                                   json={"email": org_email,
                                         "password": "secret-123"})
            code = (await idc.get("/api/dev/last-otp",
                                  params={"email": org_email})).json()["code"]
            done = await idc.post("/api/auth/login/verify",
                                  json={"email": org_email, "code": code})
            owner_sid = done.json()["session_id"]
            async with _web() as w:
                w.cookies.set(SESSION_COOKIE, owner_sid)
                c = await w.post("/org/clients/new",
                                 data={**CLIENT_FORM,
                                       "email": f"qmx-{u}@example.com"},
                                 follow_redirects=False)
                assert c.status_code == 303, c.text[:300]
                edge = c.headers["location"].rsplit("/", 1)[1].split("?")[0]
                rfq_id = await _raise_rfq(w, edge, f"Q-MX-{u}",
                                          vessel="MV Matrix")

            sessions = {"owner": owner_sid}
            for role in ("admin", "ops", "surveyor", "viewer"):
                sessions[role] = await _same_org_member(
                    idc, org_id, role, f"qmx-{role}-{u}@example.com")

            for role, expect in (("owner", 200), ("admin", 200),
                                 ("ops", 200), ("surveyor", 404),
                                 ("viewer", 404)):
                async with _web() as w:
                    w.cookies.set(SESSION_COOKIE, sessions[role])
                    listing = await w.get("/org/quotes")
                    assert listing.status_code == expect, role
                    raise_form = await w.get("/org/quotes/raise")
                    assert raise_form.status_code == expect, role
                    options = await w.get("/org/quotes/rfq-options.json")
                    assert options.status_code == expect, role
                    if expect == 200:
                        rail = _rail(listing.text)
                        assert 'href="/org/quotes"' in rail, role
                        assert ">Quotes</span>" in rail, role
                        feed = options.json()
                        assert [r["id"] for r in feed["rfqs"]] == [rfq_id]
                    else:
                        assert 'href="/org/quotes"' not in \
                            _rail(listing.text), role
                        post = await w.post(
                            "/org/quotes/raise",
                            data={**QUOTE_FORM, "rfq_id": rfq_id})
                        assert post.status_code == 404, role
                        edit = await w.get(f"/org/quotes/{uuid.uuid4()}/edit")
                        assert edit.status_code == 404, role

            # the registered-client role: 404 + rail item absent
            async with _web() as wc:
                await _register_client_org(idc, wc, u, "qcl")
                listing = await wc.get("/org/quotes")
                assert listing.status_code == 404
                assert 'href="/org/quotes"' not in _rail(listing.text)
                gate = await wc.get(f"/org/quotes/{uuid.uuid4()}/issue")
                assert gate.status_code == 404

    arun(scenario())


# -------------------------------------------------------------------- list

def test_quotes_list_draft_and_issued(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                _, _, rfq_id, _ = await _tenant_client_rfq(
                    idc, w, u, ref=f"Q-LIST-{u}")
                quote_id = await _raise_draft(w, rfq_id)

                # draft row on the list
                listing = await w.get("/org/quotes")
                assert listing.status_code == 200
                detail = await w.get(f"/org/quotes/{quote_id}")
                assert detail.status_code == 200
                m = re.search(r'<h1><span class="sv-mono sv-ugc">([^<]+)'
                              r'</span></h1>', detail.text)
                quote_no = m.group(1)
                assert quote_no in listing.text
                assert "Dar Shipping" in listing.text
                assert "MV Listed" in listing.text
                assert "1,950.00 USD" in listing.text
                assert 'sv-status--draft' in listing.text
                assert ">Draft</span>" in listing.text

                # issue it — the list now reads "Issued"
                await _issue(w, quote_id)
                listing = await w.get("/org/quotes")
                assert 'sv-status--sent' in listing.text
                assert ">Issued</span>" in listing.text
                assert 'sv-status--draft' not in listing.text

    arun(scenario())


# ------------------------------------------------------------- raise flow

def test_raise_flow_end_to_end(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                _, _, rfq_id, _ = await _tenant_client_rfq(
                    idc, w, u, ref=f"Q-RAISE-{u}")

                # commercial defaults first: they prefill the raise form
                saved = await w.post(
                    "/org/commercial",
                    data={"quote_prefix": "PMX",
                          "terms": ["Custom term one", "", "Custom term two"],
                          "bank_name": "Habib Bank", "branch": "Karachi",
                          "account_name": "Pak Marine Surveys",
                          "account_number": "PK36HBOK0001"},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:300]
                page = await w.get("/org/commercial?saved=1")
                assert "Commercial defaults saved." in page.text
                assert 'value="PMX"' in page.text
                assert "Custom term one" in page.text

                # the raise form: RFQ summary + prefill
                form = await w.get(f"/org/quotes/raise?rfq_id={rfq_id}")
                assert form.status_code == 200
                assert "Dar Shipping" in form.text
                assert "MV Listed" in form.text
                assert "Q-RAISE-" in form.text
                assert 'name="rfq_id"' in form.text
                assert 'value="USD"' in form.text
                assert 'name="quote_date"' in form.text
                assert 'name="valid_until"' in form.text
                assert "Custom term one" in form.text  # org default terms
                assert 'value="Habib Bank"' in form.text  # org payment block
                assert "/org/commercial" in form.text

                # save the draft — the editor keeps the prefilled custom terms
                quote_id = await _raise_draft(
                    w, rfq_id,
                    form={"terms": ["Custom term one", "Custom term two"]})
                detail = await w.get(f"/org/quotes/{quote_id}?created=1")
                assert detail.status_code == 200
                assert "Quote saved as draft." in detail.text

                # the document: every block renders
                assert 'class="sv-doc"' in detail.text
                assert "QUOTATION" in detail.text
                assert "PMX-" in detail.text  # the org prefix fed the number
                assert "Revision 1" in detail.text
                assert "On-hire condition survey attendance" in detail.text
                assert "1,950.00 USD" in detail.text
                assert "Custom term one" in detail.text
                assert "Habib Bank" in detail.text
                assert "PK36HBOK0001" in detail.text
                assert "Prepared by" in detail.text
                assert 'sv-status--draft' in detail.text
                assert ">Draft</span>" in detail.text
                assert "Q-RAISE-" in detail.text

                # a plain draft save NEVER touches the RFQ
                from sqlalchemy import text as sql

                from work.db import SessionLocal

                async with SessionLocal() as s:
                    status = (await s.execute(
                        sql("SELECT status FROM rfq WHERE id = :id"),
                        {"id": rfq_id})).scalar_one()
                assert status == "awaiting_quote"
                rfq_page = await w.get(f"/org/rfqs/{rfq_id}")
                assert "Awaiting quote" in rfq_page.text
                assert "Pricing revisions for this RFQ." in rfq_page.text
                assert ">Draft</span>" in rfq_page.text

                # edit: add a line, the server total recalculates
                edit = await w.get(f"/org/quotes/{quote_id}/edit")
                assert edit.status_code == 200
                assert "1,950.00" not in edit.text  # totals live server-side
                saved = await w.post(
                    f"/org/quotes/{quote_id}/edit",
                    data={**QUOTE_FORM,
                          "li_desc": QUOTE_FORM["li_desc"] + ["Report fee"],
                          "li_amt": QUOTE_FORM["li_amt"] + ["200"]},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:400]
                assert saved.headers["location"].endswith("?saved=1")
                detail = await w.get(f"/org/quotes/{quote_id}?saved=1")
                assert "Quote updated." in detail.text
                assert "Report fee" in detail.text
                assert "2,150.00 USD" in detail.text

    arun(scenario())


# ----------------------------------------------------------- picker feed

def test_rfq_options_feed(arun, id_asgi):
    """The raise form's feed lists only awaiting_quote/quoted RFQs targeted
    at the caller — never another tenant's."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                tenant_email, _, rfq_a, edge = await _tenant_client_rfq(
                    idc, w, u, ref=f"Q-OPT-A-{u}")
                rfq_b = await _raise_rfq(w, edge, f"Q-OPT-B-{u}",
                                         vessel="MV Second")

            async with _web() as w2:
                await _tenant_client_rfq(idc, w2, f"oth{u}",
                                         ref=f"Q-OTHER-{u}")

            async with _web() as w:
                await _login(idc, w, tenant_email)
                feed = await w.get("/org/quotes/rfq-options.json")
                assert feed.status_code == 200
                ids = [r["id"] for r in feed.json()["rfqs"]]
                assert rfq_a in ids and rfq_b in ids
                labels = {r["id"]: r for r in feed.json()["rfqs"]}
                assert labels[rfq_a]["client"] == "Dar Shipping"
                assert labels[rfq_a]["status"] == "awaiting_quote"

                # a quoted RFQ stays listed; q filters by reference
                quote_id = await _raise_draft(w, rfq_a)
                await _issue(w, quote_id)
                feed = await w.get("/org/quotes/rfq-options.json")
                ids = [r["id"] for r in feed.json()["rfqs"]]
                assert rfq_a in ids and rfq_b in ids
                assert {r["id"]: r["status"] for r in feed.json()["rfqs"]} \
                    [rfq_a] == "quoted"
                feed = await w.get("/org/quotes/rfq-options.json?q=OPT-B")
                assert [r["id"] for r in feed.json()["rfqs"]] == [rfq_b]

    arun(scenario())


# -------------------------------------------------------- approve & issue

def test_approve_issue_and_revision(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                _, _, rfq_id, _ = await _tenant_client_rfq(
                    idc, w, u, ref=f"Q-ISS-{u}")
                quote_id = await _raise_draft(w, rfq_id)

                # the explicit human gate
                gate = await w.get(f"/org/quotes/{quote_id}/issue")
                assert gate.status_code == 200
                assert "Confirm issue" in gate.text
                assert "final" in gate.text
                assert "1,950.00 USD" in gate.text
                assert "2026-11-23" in gate.text
                assert 'name="confirm" value="true"' in gate.text

                # issue -> the RFQ is quoted
                await _issue(w, quote_id)
                detail = await w.get(f"/org/quotes/{quote_id}?issued=1")
                assert "Quote issued" in detail.text
                assert 'sv-status--sent' in detail.text
                assert ">Issued</span>" in detail.text
                assert "Approved &amp; issued on" in detail.text
                assert 'class="sv-doc-approved"' in detail.text
                rfq_page = await w.get(f"/org/rfqs/{rfq_id}")
                assert "Quoted" in rfq_page.text

                # a second POST hits work's 409 and returns with an error
                again = await w.post(f"/org/quotes/{quote_id}/issue",
                                     data={"confirm": "true"},
                                     follow_redirects=False)
                assert again.status_code == 303
                assert again.headers["location"].endswith("?issue_error=1")
                detail = await w.get(again.headers["location"])
                assert "Could not issue" in detail.text

                # edit is not offered on an issued quote
                edit = await w.get(f"/org/quotes/{quote_id}/edit",
                                   follow_redirects=False)
                assert edit.status_code == 303
                assert edit.headers["location"] == f"/org/quotes/{quote_id}"
                detail = await w.get(f"/org/quotes/{quote_id}")
                assert f"/org/quotes/{quote_id}/edit" not in detail.text
                assert "Create revision" in detail.text

                # revision: fresh draft, prior superseded, RFQ stays quoted
                rev = await w.post(f"/org/quotes/{quote_id}/revision",
                                   follow_redirects=False)
                assert rev.status_code == 303, rev.text[:300]
                new_id = _quote_id(rev.headers["location"])
                editor = await w.get(rev.headers["location"])
                assert editor.status_code == 200
                assert "Edit quote" in editor.text
                assert "Quote saved as draft." in editor.text
                assert new_id != quote_id
                rfq_page = await w.get(f"/org/rfqs/{rfq_id}")
                assert ">Superseded</span>" in rfq_page.text
                assert 'sv-status--superseded' in rfq_page.text
                assert "Quoted" in rfq_page.text
                from sqlalchemy import text as sql

                from work.db import SessionLocal

                async with SessionLocal() as s:
                    status = (await s.execute(
                        sql("SELECT status FROM rfq WHERE id = :id"),
                        {"id": rfq_id})).scalar_one()
                assert status == "quoted"

    arun(scenario())


# ----------------------------------------------------------- cross-tenant

def test_quote_cross_tenant_is_404(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                _, _, rfq_id, _ = await _tenant_client_rfq(
                    idc, w, u, ref=f"Q-X-{u}")
                quote_id = await _raise_draft(w, rfq_id)

            async with _web() as w2:
                await _live_tenant(idc, w2, f"x{u}")
                other = await w2.get(f"/org/quotes/{quote_id}")
                assert other.status_code == 404
                assert "Page not found" in other.text
                edit = await w2.get(f"/org/quotes/{quote_id}/edit",
                                    follow_redirects=False)
                assert edit.status_code == 404
                gate = await w2.get(f"/org/quotes/{quote_id}/issue")
                assert gate.status_code == 404
                rev = await w2.post(f"/org/quotes/{quote_id}/revision",
                                    follow_redirects=False)
                assert rev.status_code == 404

    arun(scenario())


# ------------------------------------------------------------ client view

def test_client_role_sees_quote_status_readonly(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as wt:
                tenant_email, slug = await _live_tenant(idc, wt, u)

            async with _web() as wa:
                await _register_client_org(idc, wa, u, "qro")
                from webapp import pages
                pages.limiter._hits.clear()
                ref = f"Q-RO-{u}"
                thanks = await wa.post(
                    f"/org/{slug}/rfq",
                    data={**FORM, "reference_no": ref, "vessel_name": "MV RO"},
                )
                assert "Track your requests" in thanks.text

                from sqlalchemy import text as sql

                from work.db import SessionLocal

                async with SessionLocal() as s:
                    rfq_id = str((await s.execute(
                        sql("SELECT id FROM rfq WHERE reference_no = :r"),
                        {"r": ref})).scalar_one())

            # the tenant prices it and issues
            async with _web() as w:
                await _login(idc, w, tenant_email)
                quote_id = await _raise_draft(w, rfq_id)
                await _issue(w, quote_id)
                detail = await w.get(f"/org/quotes/{quote_id}")
                m = re.search(r'<h1><span class="sv-mono sv-ugc">([^<]+)'
                              r'</span></h1>', detail.text)
                quote_no = m.group(1)

            # the client: status read-only, no quote links, no raise button
            async with _web() as wa:
                await _login(idc, wa, f"qro-{u}@example.com")
                page = await wa.get(f"/org/rfqs/{rfq_id}")
                assert page.status_code == 200
                assert quote_no in page.text
                assert ">Issued</span>" in page.text
                assert "1,950.00 USD" in page.text
                assert 'href="/org/quotes' not in page.text
                assert "Raise quote" not in page.text
                assert "Create revision" not in page.text
                listing = await wa.get("/org/quotes")
                assert listing.status_code == 404
                assert 'href="/org/quotes"' not in _rail(listing.text)

    arun(scenario())


# --------------------------------------------------------------- defaults

def test_commercial_defaults_page(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            org_email = f"qcom-{u}@example.com"
            reg = await idc.post("/api/register", json={
                "org_name": "Comm Org", "org_type": "survey_company",
                "email": org_email, "password": "secret-123",
                "confirm_password": "secret-123",
            })
            assert reg.status_code == 201, reg.text
            org_id = reg.json()["org_id"]
            await _verify_email(org_email)
            start = await idc.post("/api/auth/login/start",
                                   json={"email": org_email,
                                         "password": "secret-123"})
            code = (await idc.get("/api/dev/last-otp",
                                  params={"email": org_email})).json()["code"]
            done = await idc.post("/api/auth/login/verify",
                                  json={"email": org_email, "code": code})
            owner_sid = done.json()["session_id"]

            async with _web() as w:
                w.cookies.set(SESSION_COOKIE, owner_sid)
                page = await w.get("/org/commercial")
                assert page.status_code == 200
                assert 'name="quote_prefix"' in page.text
                assert "Default quotation terms" in page.text
                assert "Bank / payment details" in page.text
                assert 'class="sv-save-bar"' in page.text

                # save: prefix + terms (blank row dropped) + bank block
                saved = await w.post(
                    "/org/commercial",
                    data={"quote_prefix": "pms",  # stored upper-cased
                          "terms": ["Payment due in 07 days.", "",
                                    "Travel at actuals."],
                          "bank_name": "Habib Bank", "branch": "Karachi",
                          "account_name": "Comm Org",
                          "account_number": "ACC-001", "swift": "HBPKPKKA"},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:300]
                assert saved.headers["location"] == "/org/commercial?saved=1"
                page = await w.get("/org/commercial?saved=1")
                assert "Commercial defaults saved." in page.text
                assert 'value="PMS"' in page.text
                assert "Payment due in 07 days." in page.text
                assert 'value="ACC-001"' in page.text
                assert 'value="HBPKPKKA"' in page.text

                # identity read-back agrees
                sid = await _login(idc, w, org_email)
                me = await idc.get("/api/orgs/me/commercial",
                                   headers={"X-Seavix-Session": sid})
                assert me.status_code == 200
                body = me.json()
                assert body["quote_prefix"] == "PMS"
                assert body["default_terms"] == ["Payment due in 07 days.",
                                                 "Travel at actuals."]
                assert body["payment_details"]["account_number"] == "ACC-001"

                # a bad prefix re-renders with the field error inline
                bad = await w.post("/org/commercial",
                                   data={"quote_prefix": "a b!"})
                assert bad.status_code == 422
                assert "quote prefix must be 2-12 chars" in bad.text

            # surveyor (no org.profile.view) gets the same 404 as the
            # profile page
            sid = await _same_org_member(
                idc, org_id, "surveyor", f"qcom-surv-{u}@example.com")
            async with _web() as w:
                w.cookies.set(SESSION_COOKIE, sid)
                page = await w.get("/org/commercial")
                assert page.status_code == 404

            # a client org is bounced to the dashboard (profile-page rule)
            async with _web() as wc:
                await _register_client_org(idc, wc, u, "qcomm")
                page = await wc.get("/org/commercial",
                                    follow_redirects=False)
                assert page.status_code == 303
                assert page.headers["location"] == "/dashboard"

    arun(scenario())


# -------------------------------------------------------------------- ugc

def test_quote_detail_wraps_ugc(arun, id_asgi):
    """A 200-char unbroken client name and line description render inside
    .sv-ugc elements — nowhere on the page can free text overflow."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            long_name = "X" * 200
            long_desc = "Y" * 200
            async with _web() as w:
                tenant_email, _, edge = await _tenant_with_client(idc, w, u)
                saved = await w.post(
                    "/org/clients/new",
                    data={**CLIENT_FORM, "name": long_name,
                          "email": f"qu-{u}@example.com"},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:300]
                edge2 = saved.headers["location"].rsplit("/", 1)[1] \
                                                 .split("?")[0]
                rfq_id = await _raise_rfq(w, edge2, f"Q-UGC-{u}",
                                          vessel="MV UGC")
                quote_id = await _raise_draft(
                    w, rfq_id, form={"li_desc": [long_desc], "li_amt": ["10"]})
                detail = await w.get(f"/org/quotes/{quote_id}")
                assert detail.status_code == 200
                assert long_name in detail.text
                assert long_desc in detail.text
                for needle in (long_name, long_desc):
                    assert re.search(
                        r'<\w+[^>]*class="[^"]*sv-ugc[^"]*"[^>]*>[^<]*'
                        + re.escape(needle), detail.text), needle
                # the list row wraps the client name too
                listing = await w.get("/org/quotes")
                assert re.search(
                    r'<\w+[^>]*class="[^"]*sv-ugc[^"]*"[^>]*>[^<]*'
                    + re.escape(long_name), listing.text)

    arun(scenario())
