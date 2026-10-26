"""32f: owner-rejection corrections — the acceptance matrix.

The owner's live review of 32e rejected five things; this suite pins the
corrections:

- NAVIGATION (authenticated, click-through — not template strings): an
  owner-equivalent rail carries BOTH first-level sections with links that
  are visible, clickable, and open the correct routes; exactly one
  aria-current per page. The defect behind the rejection was a stale
  seeded-permission snapshot (system roles seeded once at registration,
  never refreshed) — identity now converges system roles to the
  code-defined catalog at authentication, so a pre-catalog-extension org
  (Pak Marines: no quote.view/client.*) self-heals at login/session time.
- HEADINGS: /org/rfqs renders H1 "View RFQ" with NO "Request pipeline"
  section head and NO "All channels and statuses, newest first — filter
  to narrow." helper; /org/quotes carries no equivalent marketing copy.
- WIDE DATA PAGES: the two indexes override the §9 1120px cap via
  sv-page-inner--data and a full-width sv-data-card; ordinary
  forms/settings pages stay capped (the design contract is untouched).
- FILTERS: the filter bar is a deliberate minmax() grid; RFQ adds the
  service/survey-type filter (blank = every type, each catalog type
  filters, composes with client/status/port/dates, Clear/Reset, unknown
  codes render the friendly no-results state — never a 500). View Quotes
  keeps client/port/status/inclusive-date filters + vessel/reference in
  the More-filters disclosure.

Identity + work + web run in-process (web/tests/conftest.py seams); every
row is created through the real pages or the work spine the pages proxy.
"""
import re
import uuid
from datetime import date, timedelta

import httpx
import pytest

from webapp.auth import SESSION_COOKIE

from test_org_rfqs_32b import _live_tenant, _login_web
from test_profile_preview_ports import _web
from test_rfq_quotes_32e import (
    _add_client,
    _fixture,
    _rail,
    _sid,
    _work_api,
    _client_session_of,
)

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()

WEB_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ helpers

async def _raise_typed(w, edge: str, ref: str, vessel: str, port: str,
                       survey_types: list[str],
                       channel: str = "manual_whatsapp") -> str:
    """A manual RFQ with an explicit survey-type selection (the 32e helper
    hardcodes 'condition')."""
    raised = await w.post(
        "/org/rfqs/new",
        data={"channel": channel, "client_mode": "existing",
              "client_id": edge, "vessel_name": vessel,
              "vessel_imo": "9074729", "survey_types": survey_types,
              "location_type": "terminal", "port_name": port,
              "preferred_date": "2026-11-01", "reference_no": ref,
              "scope": "32f typed survey selection."},
        follow_redirects=False,
    )
    assert raised.status_code == 303, raised.text[:400]
    return raised.headers["location"].split("/org/rfqs/")[1].split("?")[0]


async def _typed_fixture(idc, w, u: str):
    """Four RFQs across three survey types, one multi-type."""
    tenant_email, slug = await _live_tenant(idc, w, u)
    edge_dar = await _add_client(w, u, "dar", "Dar Shipping")
    edge_gulf = await _add_client(w, u, "gulf", "Gulf Chartering")
    fx = {"tenant_email": tenant_email, "slug": slug,
          "ref_a": f"TA-{u}", "ref_b": f"TB-{u}",
          "ref_c": f"TC-{u}", "ref_d": f"TD-{u}"}
    fx["rfq_a"] = await _raise_typed(
        w, edge_dar, fx["ref_a"], "MV Alpha Trader", "Karachi", ["condition"])
    fx["rfq_b"] = await _raise_typed(
        w, edge_gulf, fx["ref_b"], "MV Beta Star", "Fujairah",
        ["pre_purchase"], channel="manual_email")
    fx["rfq_c"] = await _raise_typed(
        w, edge_dar, fx["ref_c"], "MV Gamma Dawn", "Karachi anchorage",
        ["bunker_quantity"])
    fx["rfq_d"] = await _raise_typed(
        w, edge_gulf, fx["ref_d"], "MV Delta Queen", "Rotterdam",
        ["condition", "bunker_quantity"])
    return fx


# ------------------------------------------------- authenticated navigation

def test_owner_equivalent_navigation_links_clickable(arun, id_asgi):
    """The owner-equivalent rail: both first-level sections, every link
    VISIBLE in the rail markup and CLICKABLE — each opens the correct route
    with exactly one aria-current on the landing page."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                await _live_tenant(idc, w, u)
                landing = await w.get("/dashboard")
                assert landing.status_code == 200
                rail = _rail(landing.text)

                # both sections are first-level group labels
                assert "<h2>Requests for Quotation</h2>" in rail
                assert "<h2>Quotations</h2>" in rail
                assert rail.index("Requests for Quotation") \
                    < rail.index("Quotations")

                # the four links, in section order, each href visible once
                expected = [
                    ("Create RFQ", "/org/rfqs/new"),
                    ("View RFQ", "/org/rfqs"),
                    ("Create Quote", "/org/quotes/raise"),
                    ("View Quotes", "/org/quotes"),
                ]
                for label, href in expected:
                    assert f'href="{href}"' in rail, (label, href)
                    assert f">{label}</span>" in rail, label

                # clickable + correct route + one active item per page
                for href in (h for _, h in expected):
                    page = await w.get(href)
                    assert page.status_code == 200, href
                    current = re.findall(
                        r'class="sv-rail-item"[^>]*aria-current="page"',
                        page.text)
                    assert len(current) == 1, href
                    assert page.text.count(f'href="{href}"') >= 1

    arun(scenario())


def test_stale_system_role_grants_converge(arun, id_asgi):
    """Root-cause proof for the missing Quotations nav: an org whose
    system roles were seeded BEFORE a catalog extension (owner role
    without quote.view/client.* — exactly Pak Marines' snapshot) gets the
    code-derived grants back at the next authentication, so View Quotes
    appears and /org/quotes stops 404ing. No migration involved."""
    from sqlalchemy import text as sql

    from app.db import SessionLocal
    from app.rbac import reset_converged

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                _, slug = await _live_tenant(idc, w, u)
                org_id = (await idc.get(
                    "/api/internal/whoami",
                    headers={"X-Seavix-Session": await _sid(w)})).json()
                oid = org_id["org_id"]

                # stage the pre-32a snapshot in the identity DB
                async with SessionLocal() as s:
                    await s.execute(sql(
                        "UPDATE role SET permissions = (SELECT array_agg(p) "
                        "FROM unnest(permissions) t(p) WHERE p NOT IN "
                        "('quote.view','client.view','client.create',"
                        "'client.edit')) WHERE org_id = :o AND name = 'owner'"
                    ), {"o": oid})
                    await s.commit()
                reset_converged()

                # the stale state: View Quotes is denied...
                async with SessionLocal() as s:
                    row = (await s.execute(sql(
                        "SELECT permissions::text FROM role WHERE org_id=:o "
                        "AND name='owner'"), {"o": oid})).first()
                assert "quote.view" not in row[0], row[0]

                # ...and the very next page load heals it (session-time
                # convergence through the middleware ctx path)
                page = await w.get("/org/rfqs")
                assert page.status_code == 200
                rail = _rail(page.text)
                assert "<h2>Quotations</h2>" in rail
                assert 'href="/org/quotes"' in rail
                quotes = await w.get("/org/quotes")
                assert quotes.status_code == 200, quotes.status_code

                async with SessionLocal() as s:
                    row = (await s.execute(sql(
                        "SELECT permissions::text FROM role WHERE org_id=:o "
                        "AND name='owner'"), {"o": oid})).first()
                assert "quote.view" in row[0] and "client.view" in row[0], row[0]

    arun(scenario())


# --------------------------------------------------------- headings + width

def test_view_rfq_heading_contract(arun, id_asgi):
    """/org/rfqs: H1 'View RFQ'; the 'Request pipeline' section head and
    the 'All channels and statuses, newest first — filter to narrow.'
    helper are GONE (and no replacement marketing copy was added)."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                await _live_tenant(idc, w, u)
                page = await w.get("/org/rfqs")
                assert page.status_code == 200
                assert "<h1>View RFQ</h1>" in page.text
                assert "Request pipeline" not in page.text
                assert "All channels and statuses" not in page.text
                assert "filter to narrow" not in page.text
                assert "sv-section-head" not in page.text
                quotes = await w.get("/org/quotes")
                assert "<h1>View Quotes</h1>" in quotes.text
                assert "sv-section-head" not in quotes.text
                assert "filter to narrow" not in quotes.text

    arun(scenario())


def test_wide_data_variant_scoping():
    """The wide shell applies to the two data indexes ONLY: the §9 1120px
    cap is untouched for ordinary forms/settings/document pages."""
    rfqs = (WEB_ROOT / "templates" / "org_rfqs.html").read_text()
    quotes = (WEB_ROOT / "templates" / "org_quotes.html").read_text()
    for tpl in (rfqs, quotes):
        assert "sv-page-inner--data" in tpl
        assert "sv-data-card" in tpl
    ordinary = ("org_profile.html", "org_rfq_new.html", "org_quote_form.html",
                "org_clients.html", "org_client_new.html",
                "org_commercial.html", "org_rfq_detail.html",
                "org_quote_detail.html")
    for name in ordinary:
        text = (WEB_ROOT / "templates" / name).read_text()
        assert "sv-page-inner--data" not in text, name

    style = (WEB_ROOT / "static" / "style.css").read_text()
    inner = re.search(r"\.sv-page-inner\s*\{(.*?)\}", style, re.S)
    assert inner and "var(--page-max)" in inner.group(1), \
        "ordinary pages must keep the capped shell"
    wide = re.search(r"\.sv-page-inner--data\s*\{(.*?)\}", style, re.S)
    assert wide and "max-width: none" in wide.group(1)
    assert "--page-max: 1120px" in style  # design contract value intact
    assert ".sv-data-card" in style
    assert "minmax(" in style  # the deliberate grid tracks


def test_filter_bar_grid_contract(arun, id_asgi):
    """The primary filter row carries exactly the owner's field set in
    deliberate order; vessel/reference sit in the More-filters disclosure
    (which opens server-side when one of them is applied)."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                await _live_tenant(idc, w, u)
                page = await w.get("/org/rfqs")
                form = re.search(
                    r'<form method="get" action="/org/rfqs"(.*?)</form>',
                    page.text, re.S).group(1)
                assert 'class="sv-filter-bar sv-filter-bar--rfq"' in page.text
                # primary order: client, status, port, service type, dates
                primary = form.split("sv-filter-more")[0]
                for needle, nxt in (
                        ('id="f-client"', 'id="f-status"'),
                        ('id="f-status"', 'id="f-port"'),
                        ('id="f-port"', 'id="f-stype"'),
                        ('id="f-stype"', 'id="f-from"'),
                        ('id="f-from"', 'id="f-to"'),
                        ('id="f-to"', ">Apply</button>")):
                    assert primary.index(needle) < primary.index(nxt), \
                        (needle, nxt)
                # service/survey type is a select fed by the catalog
                stype = re.search(
                    r'<select id="f-stype".*?</select>', form, re.S).group(0)
                assert "<option value=\"\">All</option>" in stype
                assert "<option value=\"condition\"" in stype
                assert "<option value=\"pre_purchase\"" in stype
                # vessel + reference in the disclosure
                more = re.search(
                    r'<details class="sv-filter-more"(.*?)</details>',
                    form, re.S).group(1)
                assert 'name="vessel"' in more and 'name="ref"' in more

                # the disclosure opens when a more-filter is applied
                vessel_page = await w.get("/org/rfqs?vessel=MV")
                assert '<details class="sv-filter-more" open>' \
                    in vessel_page.text

                quotes = await w.get("/org/quotes")
                qform = re.search(
                    r'<form method="get" action="/org/quotes"(.*?)</form>',
                    quotes.text, re.S).group(1)
                assert 'class="sv-filter-bar sv-filter-bar--quotes"' \
                    in quotes.text
                qprimary = qform.split("sv-filter-more")[0]
                for needle, nxt in (
                        ('id="q-client"', 'id="q-status"'),
                        ('id="q-status"', 'id="q-port"'),
                        ('id="q-port"', 'id="q-from"'),
                        ('id="q-from"', 'id="q-to"'),
                        ('id="q-to"', ">Apply</button>")):
                    assert qprimary.index(needle) < qprimary.index(nxt), \
                        (needle, nxt)
                qmore = re.search(
                    r'<details class="sv-filter-more"(.*?)</details>',
                    qform, re.S).group(1)
                assert 'name="vessel"' in qmore and 'name="ref"' in qmore

    arun(scenario())


# ------------------------------------------------------- service/type filter

def test_view_rfq_survey_type_filter(arun, id_asgi):
    """The 32e handoff never proved the service/survey-type filter — it
    did not exist. Blank/All returns every type; each available type
    filters correctly (a multi-type RFQ matches EACH of its types); it
    composes with client/status/port/dates; Clear/Reset restores the
    complete list; an unknown code renders the friendly no-results state,
    never a 500."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _typed_fixture(idc, w, u)
                A, B, C, D = fx["ref_a"], fx["ref_b"], fx["ref_c"], fx["ref_d"]

                # blank/All -> the complete default-all list
                all_page = await w.get("/org/rfqs")
                for ref in (A, B, C, D):
                    assert ref in all_page.text, ref

                # each available type filters correctly
                cond = await w.get("/org/rfqs", params={"survey_type": "condition"})
                assert A in cond.text and D in cond.text
                assert B not in cond.text and C not in cond.text
                # the applied select value is retained
                assert re.search(r'value="condition"[^>]*selected',
                                 cond.text)

                pre = await w.get("/org/rfqs",
                                  params={"survey_type": "pre_purchase"})
                assert B in pre.text
                assert A not in pre.text and C not in pre.text \
                    and D not in pre.text

                bunker = await w.get("/org/rfqs",
                                     params={"survey_type": "bunker_quantity"})
                assert C in bunker.text and D in bunker.text
                assert A not in bunker.text and B not in bunker.text

                # composes with client, status, port and dates
                combo = await w.get("/org/rfqs", params={
                    "survey_type": "condition", "client": "gulf"})
                assert D in combo.text and A not in combo.text
                combo2 = await w.get("/org/rfqs", params={
                    "survey_type": "bunker_quantity", "port": "karachi",
                    "status": "awaiting_quote"})
                assert C in combo2.text and D not in combo2.text
                combo3 = await w.get("/org/rfqs", params={
                    "survey_type": "condition",
                    "date_from": TODAY, "date_to": TODAY})
                assert A in combo3.text and D in combo3.text
                assert B not in combo3.text and C not in combo3.text

                # unknown code -> friendly empty state, never a 500
                bogus = await w.get("/org/rfqs",
                                    params={"survey_type": "no_such_type"})
                assert bogus.status_code == 200
                assert "No RFQs match these filters" in bogus.text
                assert "No RFQs yet" not in bogus.text

                # Clear/Reset restores the complete default-all table
                cleared = await w.get("/org/rfqs")
                for ref in (A, B, C, D):
                    assert ref in cleared.text, ref

                # the work spine applies tenant scoping before the type
                # filter: the second tenant never sees these rows
                async with _web() as w2:
                    _, slug2 = await _live_tenant(idc, w2, u + "x")
                    other = await w2.get("/org/rfqs",
                                         params={"survey_type": "condition"})
                    for ref in (A, B, C, D):
                        assert ref not in other.text, ref

    arun(scenario())


# ---------------------------------------------- quotes filters (owner list)

def test_view_quotes_owner_filter_matrix(arun, id_asgi):
    """The owner's explicit View-Quotes list — client / port / status /
    inclusive date-range filters, individually and combined, on a
    populated table spanning every status; 'sent' reads 'Issued';
    Clear/Reset restores default-all."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _fixture(idc, w, u)

                default = await w.get("/org/quotes")
                assert default.status_code == 200
                # every quote status vocabulary incl. Issued
                for label in ("Draft", "Issued", "Accepted", "Declined",
                              "Superseded"):
                    assert f">{label}</span>" in default.text, label
                # full column set
                for col in ("Quote no", "Client", "Vessel", "RFQ reference",
                            "Port/location", "Quote date", "Valid until",
                            "Total", "Status", "Actions"):
                    assert f"<th scope=\"col\">{col}</th>" in default.text, col

                # client — case-insensitive partial
                by_client = await w.get("/org/quotes", params={"client": "gulf"})
                assert fx["ref_b"] in by_client.text
                assert fx["ref_a"] not in by_client.text

                # port
                by_port = await w.get("/org/quotes", params={"port": "fujairah"})
                assert fx["ref_b"] in by_port.text
                assert fx["ref_a"] not in by_port.text

                # status (stored 'sent' renders 'Issued')
                by_status = await w.get("/org/quotes",
                                        params={"status": "accepted"})
                assert fx["ref_c"] in by_status.text
                assert fx["ref_b"] not in by_status.text

                # inclusive date range on quote_date
                inc = await w.get("/org/quotes", params={
                    "date_from": TODAY, "date_to": TODAY})
                for ref in (fx["ref_a"], fx["ref_b"], fx["ref_c"], fx["ref_d"]):
                    assert ref in inc.text, ref
                before = await w.get("/org/quotes",
                                     params={"date_to": YESTERDAY})
                assert fx["ref_a"] not in before.text
                assert "No quotes match these filters" in before.text

                # combined: client + status narrows precisely
                combo = await w.get("/org/quotes", params={
                    "client": "gulf", "status": "sent"})
                assert fx["ref_b"] in combo.text
                assert fx["ref_a"] not in combo.text
                assert fx["ref_d"] not in combo.text

                # reversed dates: clean no-results, never a 5xx
                rev = await w.get("/org/quotes", params={
                    "date_from": TOMORROW, "date_to": TODAY})
                assert rev.status_code == 200
                assert "No quotes match these filters" in rev.text
                bad = await w.get("/org/quotes",
                                  params={"date_from": "not-a-date"})
                assert bad.status_code == 200
                assert "invalid date filter was ignored" in bad.text
                assert fx["ref_a"] in bad.text

                # Clear restores the complete list
                cleared = await w.get("/org/quotes")
                for ref in (fx["ref_a"], fx["ref_b"], fx["ref_c"], fx["ref_d"]):
                    assert ref in cleared.text, ref

    arun(scenario())


# -------------------------------------------- row actions stay (regression)

def test_awaiting_quote_row_action_regression(arun, id_asgi):
    """32f touched the RFQ index template — the Awaiting Quote -> Quote
    (preselected Generate Quote) flow and the Quote Submitted no-duplicate
    rule must be exactly as 32e left them."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _typed_fixture(idc, w, u)
                listing = await w.get("/org/rfqs")
                assert "Awaiting Quote" in listing.text
                link = f'/org/quotes/raise?rfq_id={fx["rfq_a"]}'
                assert link in listing.text
                form = await w.get(link)
                assert form.status_code == 200
                assert f'value="{fx["rfq_a"]}"' in form.text

                # quoted rows: label + no duplicate Quote action
                async with _web() as wt:
                    await _login_web(idc, wt, fx["tenant_email"])
                    from test_org_quotes_32c import _issue, _raise_draft
                    issued = await _raise_draft(
                        wt, fx["rfq_b"],
                        form={"quote_date": TODAY, "valid_until": TOMORROW})
                    await _issue(wt, issued)
                quoted = await w.get("/org/rfqs?status=quoted")
                assert fx["ref_b"] in quoted.text
                assert "Quote Submitted" in quoted.text
                assert f'/org/quotes/raise?rfq_id={fx["rfq_b"]}' \
                    not in quoted.text

    arun(scenario())
