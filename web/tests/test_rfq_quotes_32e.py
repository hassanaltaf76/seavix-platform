"""32e: RFQ + Quotation navigation, tables and filters — the acceptance
matrix for the owner UI-refinement slice.

- NAVIGATION: the tenant rail carries two new permission-gated groups —
  "Requests for Quotation" (Create RFQ / View RFQ) and "Quotations"
  (Create Quote / View Quotes) — with the exact owner-specified labels,
  section headings that are group labels (never links), one active state
  per view, and the client role seeing NONE of it (its own Workspace
  RFQs item stays).
- OWN-PROFILE PREVIEW: a signed-in member viewing the LIVE public profile
  of their OWN org gets a disabled, non-navigating Raise-an-RFQ control
  with an accessible explanation; signed-out visitors and external
  signed-in users keep the live CTA and the registration entry gate.
- VIEW RFQ: default-ALL (every channel + status, newest first), composing
  filters (client / status / port / vessel / reference / inclusive
  created-date range), invalid + reversed dates handled cleanly,
  Clear/Reset, explicit no-results state, "Awaiting Quote" row -> Quote
  action opening the raise form with THAT RFQ preselected, "Quote
  Submitted" label and no duplicate-draft action on quoted rows.
- VIEW QUOTES: default-ALL across every status (sent renders "Issued"),
  client / port / vessel / reference / status / inclusive quote-date
  filters, Clear/Reset, no-results state.
- ISOLATION: a second tenant's filtered lists contain none of tenant 1's
  rows and its row actions 404.
- LAYOUT: the tables sit in .sv-table-wrap (deliberate horizontal
  containment) — a real-Chrome 390px assertion proves no page-level
  overflow with hostile UGC in the cells.

Identity + work + web run in-process (web/tests/conftest.py seams); every
row is created through the real pages or the work spine the pages proxy.
"""
import re
import subprocess
import uuid
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from webapp.auth import SESSION_COOKIE

from test_org_quotes_32c import QUOTE_FORM, _issue, _raise_draft
from test_org_rfqs_32b import (
    CLIENT_FORM,
    _live_tenant,
    _register_client_org,
    _same_org_member,
)
from test_profile_preview_ports import _verify_email, _web

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


# ------------------------------------------------------------------ helpers

def _rail(html: str) -> str:
    return html.split('class="sv-rail"')[1].split('class="sv-page"')[0]


def _rfq_row_id(html: str, ref: str) -> str:
    """The detail/quote href for a reference's row."""
    m = re.search(rf'href="/org/rfqs/([0-9a-f-]+)"[^>]*>\s*{re.escape(ref)}',
                  html)
    assert m, f"no row link for {ref}"
    return m.group(1)


async def _add_client(w, u: str, tag: str, name: str) -> str:
    saved = await w.post(
        "/org/clients/new",
        data={**CLIENT_FORM, "name": name, "email": f"{tag}-{u}@example.com"},
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text[:300]
    return saved.headers["location"].rsplit("/", 1)[1].split("?")[0]


async def _raise_manual(w, edge: str, ref: str, vessel: str, port: str,
                        channel: str) -> str:
    raised = await w.post(
        "/org/rfqs/new",
        data={"channel": channel, "client_mode": "existing",
              "client_id": edge, "vessel_name": vessel,
              "vessel_imo": "9074729", "survey_types": ["condition"],
              "location_type": "terminal", "port_name": port,
              "preferred_date": "2026-11-01", "reference_no": ref,
              "scope": "On-hire condition survey."},
        follow_redirects=False,
    )
    assert raised.status_code == 303, raised.text[:400]
    return raised.headers["location"].split("/org/rfqs/")[1].split("?")[0]


async def _public_rfq_as_client(idc, w, u: str, slug: str, ref: str,
                                vessel: str, port: str) -> tuple[str, str]:
    """A registered-client org submitting through the real public funnel;
    returns (email, rfq_id)."""
    email = await _register_client_org(idc, w, u, ref.split("-")[0].lower())
    from webapp import pages
    pages.limiter._hits.clear()
    thanks = await w.post(
        f"/org/{slug}/rfq",
        data={"vessel_name": vessel, "vessel_imo": "9404572",
              "survey_types": ["condition"], "location_type": "anchorage",
              "port_name": port, "preferred_date": "2026-11-01",
              "reference_no": ref, "scope": "Condition survey"},
    )
    assert "Track your requests" in thanks.text
    from sqlalchemy import text as sql

    from work.db import SessionLocal

    async with SessionLocal() as s:
        rfq_id = str((await s.execute(
            sql("SELECT id FROM rfq WHERE reference_no = :r"),
            {"r": ref})).scalar_one())
    return email, rfq_id


async def _work_api(sid: str, method: str, path: str, **kw) -> httpx.Response:
    """Direct work-spine call carrying the caller's session (accept and
    decline have no web page yet — the spine IS the contract)."""
    from work.main import app as work_app

    headers = kw.pop("headers", {})
    headers.setdefault("X-Seavix-Session", sid)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=work_app), base_url="http://work"
    ) as c:
        return await c.request(method, path, headers=headers, **kw)


async def _client_session_of(email: str) -> str:
    """The identity session id behind a logged-in web client (the web
    cookie IS the identity session)."""
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        row = (await s.execute(
            sql("SELECT s.id FROM auth_session s JOIN app_user u "
                "ON u.id = s.user_id WHERE u.email = :e "
                "ORDER BY s.created_at DESC LIMIT 1"),
            {"e": email})).first()
    return str(row[0])


async def _fixture(idc, w, u: str):
    """The populated tenant: every channel, every RFQ status, every quote
    status. Returns a dict of the interesting ids/refs.

    Session discipline: the public funnels log their client orgs in on
    their OWN web clients, and the tenant re-authenticates on a fresh
    client afterwards — a funnel must never clobber the tenant cookie."""
    from test_org_rfqs_32b import _login_web

    tenant_email, slug = await _live_tenant(idc, w, u)
    edge_dar = await _add_client(w, u, "dar", "Dar Shipping")
    edge_gulf = await _add_client(w, u, "gulf", "Gulf Chartering")

    fx = {"tenant_email": tenant_email, "slug": slug,
          "ref_a": f"AA-{u}", "ref_b": f"BB-{u}",
          "ref_c": f"CC-{u}", "ref_d": f"DD-{u}"}
    # manual channels — Dar via WhatsApp (awaiting), Gulf via email (quoted)
    fx["rfq_a"] = await _raise_manual(
        w, edge_dar, fx["ref_a"], "MV Alpha Trader", "Karachi",
        "manual_whatsapp")
    fx["rfq_b"] = await _raise_manual(
        w, edge_gulf, fx["ref_b"], "MV Beta Star", "Fujairah",
        "manual_email")

    # public channel from two registered clients (real funnel each, on
    # their own clients)
    async with _web() as wc:
        email_c, fx["rfq_c"] = await _public_rfq_as_client(
            idc, wc, u, slug, fx["ref_c"], "MV Gamma Dawn", "Karachi")
    async with _web() as wd:
        email_d, fx["rfq_d"] = await _public_rfq_as_client(
            idc, wd, u + "x", slug, fx["ref_d"], "MV Delta Queen", "Rotterdam")

    # quotes in every status — as the tenant on a fresh session:
    #  draft        on A (the RFQ stays awaiting_quote)
    #  issued       on B (the RFQ goes quoted)
    #  accepted     on C (by the requesting client; RFQ accepted)
    #  superseded+declined on D (a second quote supersedes the first at
    #               creation; the second is issued then declined)
    async with _web() as wt:
        await _login_web(idc, wt, tenant_email)
        fx["draft_id"] = await _raise_draft(wt, fx["rfq_a"],
                                            form={"quote_date": TODAY,
                                                  "valid_until": TOMORROW})
        fx["issued_id"] = await _raise_draft(wt, fx["rfq_b"],
                                             form={"quote_date": TODAY,
                                                   "valid_until": TOMORROW})
        await _issue(wt, fx["issued_id"])
        fx["accepted_id"] = await _raise_draft(wt, fx["rfq_c"],
                                               form={"quote_date": TODAY,
                                                     "valid_until": TOMORROW})
        await _issue(wt, fx["accepted_id"])
        r = await _work_api(await _client_session_of(email_c), "POST",
                            f"/api/quotes/{fx['accepted_id']}/accept")
        assert r.status_code == 201, r.text[:300]

        d1 = await _raise_draft(wt, fx["rfq_d"],
                                form={"quote_date": TODAY,
                                      "valid_until": TOMORROW})
        await _issue(wt, d1)
        d2 = await _raise_draft(wt, fx["rfq_d"],
                                form={"quote_date": TODAY,
                                      "valid_until": TOMORROW})
        fx["superseded_id"] = d1
        fx["declined_id"] = d2
        await _issue(wt, d2)
        r = await _work_api(await _client_session_of(email_d), "POST",
                            f"/api/quotes/{d2}/decline")
        assert r.status_code == 200, r.text[:300]
    return fx


# -------------------------------------------------------------- navigation

def test_tenant_rail_rfq_quote_sections(arun, id_asgi):
    """The exact owner labels, grouped under permission-gated section
    headings that are group labels (not links), with one active state
    per view; the client role gets NONE of the tenant groups."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                _, slug = await _live_tenant(idc, w, u)
                listing = await w.get("/org/rfqs")
                assert listing.status_code == 200
                rail = _rail(listing.text)
                # the two new groups, exact spelling
                assert "<h2>Requests for Quotation</h2>" in rail
                assert "<h2>Quotations</h2>" in rail
                for label in ("Create RFQ", "View RFQ", "Create Quote",
                              "View Quotes"):
                    assert f">{label}</span>" in rail, label
                # headings are group labels — never wrapped in a link
                assert re.search(r"<a[^>]*>\s*<h2", rail) is None
                # grouped: Create/View RFQ live under the RFQ heading,
                # the quote items under Quotations, before the page end
                at_rfq = rail.index("Requests for Quotation")
                at_q = rail.index("Quotations")
                assert rail.index('href="/org/rfqs/new"') > at_rfq
                assert rail.index('href="/org/rfqs"') > at_rfq
                assert rail.index('href="/org/quotes/raise"') > at_q
                assert rail.index('href="/org/quotes"') > at_q

                # active states — exactly one aria-current per view
                for path, item in (
                        ("/org/rfqs", '"rfqs"'),
                        ("/org/rfqs/new", '"rfq-new"'),
                        ("/org/quotes", '"quotes"'),
                        ("/org/quotes/raise", '"quote-new"')):
                    page = await w.get(path)
                    assert page.status_code == 200, path
                    assert page.text.count("sv-rail-item\" href=") >= 1
                    current = re.findall(
                        r'class="sv-rail-item"[^>]*aria-current="page"',
                        page.text)
                    assert len(current) == 1, path

                # Create Quote must retain the mandatory RFQ picker
                raise_form = await w.get("/org/quotes/raise")
                assert 'name="rfq_id"' in raise_form.text

                # surveyor/viewer: neither group, no quote links at all
                org_id = (await idc.get(
                    "/api/internal/whoami",
                    headers={"X-Seavix-Session": await _sid(w)})).json()
                for role in ("surveyor", "viewer"):
                    sid = await _same_org_member(
                        idc, org_id["org_id"], role,
                        f"nav-{role}-{u}@example.com")
                    async with _web() as wr:
                        wr.cookies.set(SESSION_COOKIE, sid)
                        home = await wr.get("/dashboard")
                        assert home.status_code == 200
                        rail = _rail(home.text)
                        assert "Requests for Quotation" not in rail
                        assert "Quotations" not in rail
                        assert 'href="/org/rfqs/new"' not in rail
                        assert 'href="/org/quotes"' not in rail

            # the client role: its own Workspace RFQs item only — no
            # tenant RFQ group, no Quotations group, no quote pages
            async with _web() as wc:
                email = await _register_client_org(idc, wc, u, "nvc")
                mine = await wc.get("/org/rfqs")
                assert mine.status_code == 200
                rail = _rail(mine.text)
                assert 'href="/org/rfqs"' in rail
                assert "Requests for Quotation" not in rail
                assert "Quotations" not in rail
                assert 'href="/org/quotes"' not in rail
                quotes_page = await wc.get("/org/quotes")
                assert quotes_page.status_code == 404
                raise_page = await wc.get("/org/quotes/raise")
                assert raise_page.status_code == 404

    arun(scenario())


async def _sid(w) -> str:
    # repeated logins on one client leave several session cookies in the
    # jar (dict access then raises CookieConflict) — take the newest
    matches = [c for c in w.cookies.jar if c.name == SESSION_COOKIE]
    return matches[-1].value


# ---------------------------------------------------- own-profile preview

def test_own_org_live_profile_cta_is_a_preview(arun, id_asgi):
    """The signed-in owner viewing the LIVE public profile of their OWN
    org gets a disabled, non-navigating Raise-an-RFQ with an accessible
    explanation; the settings preview renders the same; signed-out
    visitors and external signed-in users keep the live CTA."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                _, slug = await _live_tenant(idc, w, u)

                # owner on the live public page: preview treatment
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert 'sv-rfq-cta" type="button" disabled' in pub.text
                assert "You are previewing your public profile" in pub.text
                assert 'aria-describedby="sv-rfq-preview-note"' in pub.text
                assert f'href="/org/{slug}/rfq"' not in pub.text

                # the settings WYSIWYG preview renders the same control
                blade = await w.get("/org/profile")
                assert 'sv-rfq-cta" type="button" disabled' in blade.text
                assert f'href="/org/{slug}/rfq"' not in blade.text

                # a signed-in user of ANOTHER org keeps the live CTA
                async with _web() as w2:
                    await _register_client_org(idc, w2, u, "oth")
                    other = await w2.get(f"/org/{slug}")
                    assert other.status_code == 200
                    assert f'href="/org/{slug}/rfq"' in other.text
                    assert "sv-rfq-preview-note" not in other.text

            # signed-out: the live CTA and the entry gate stay untouched
            async with _web() as wa:
                anon = await wa.get(f"/org/{slug}")
                assert anon.status_code == 200
                assert f'href="/org/{slug}/rfq"' in anon.text
                gate = await wa.get(f"/org/{slug}/rfq",
                                    follow_redirects=False)
                assert gate.status_code == 303
                assert gate.headers["location"] == \
                    f"/register?next=/org/{slug}/rfq"

    arun(scenario())


def test_anonymous_rfq_entry_gate_regressions(arun, id_asgi):
    """Anonymous GET and POST of the public RFQ form redirect to
    registration immediately and persist nothing (the 32e partial/CTA
    work must not weaken the entry-time gate)."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as wt:
                _, slug = await _live_tenant(idc, wt, u)
            async with _web() as w:
                ref = f"GATE-{u}"
                get = await w.get(f"/org/{slug}/rfq", follow_redirects=False)
                assert get.status_code == 303
                assert get.headers["location"] == f"/register?next=/org/{slug}/rfq"
                post = await w.post(
                    f"/org/{slug}/rfq",
                    data={"vessel_name": "MV Gate", "vessel_imo": "9404572",
                          "survey_types": ["condition"],
                          "location_type": "terminal", "port_name": "Karachi",
                          "preferred_date": "2026-11-01",
                          "reference_no": ref, "scope": "gate check"},
                    follow_redirects=False)
                assert post.status_code == 303
                assert post.headers["location"] == \
                    f"/register?next=/org/{slug}/rfq"
                from sqlalchemy import text as sql

                from work.db import SessionLocal

                async with SessionLocal() as s:
                    n = (await s.execute(
                        sql("SELECT count(*) FROM rfq WHERE reference_no = :r"),
                        {"r": ref})).scalar_one()
                assert n == 0

    arun(scenario())


# ---------------------------------------------------------------- view RFQ

def test_view_rfq_default_all_and_ordering(arun, id_asgi):
    """No query parameters = EVERY RFQ of the tenant across all three
    channels and all statuses, deterministic newest first."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _fixture(idc, w, u)
                page = await w.get("/org/rfqs")
                assert page.status_code == 200
                # all channels
                for label in ("WhatsApp", "Email", "Public page"):
                    assert label in page.text, label
                # every RFQ status vocabulary renders
                assert "Awaiting Quote" in page.text
                assert "Quote Submitted" in page.text
                assert "Accepted" in page.text
                # all four rows visible, newest first
                for ref in (fx["ref_a"], fx["ref_b"], fx["ref_c"],
                            fx["ref_d"]):
                    assert ref in page.text, ref
                newest_first = (page.text.index(fx["ref_d"])
                                < page.text.index(fx["ref_c"])
                                < page.text.index(fx["ref_b"])
                                < page.text.index(fx["ref_a"]))
                assert newest_first
                # the unfiltered list shows no Clear affordance
                assert "Clear filters" not in page.text

    arun(scenario())


def test_view_rfq_status_filter_and_row_actions(arun, id_asgi):
    """Status filter; Awaiting Quote rows carry the Quote action opening
    the raise form with THAT RFQ preselected; Quote Submitted rows carry
    no duplicate new-draft action."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _fixture(idc, w, u)

                awaiting = await w.get("/org/rfqs?status=awaiting_quote")
                assert awaiting.status_code == 200
                assert fx["ref_a"] in awaiting.text
                assert fx["ref_b"] not in awaiting.text
                # the row action links to the raise form with that RFQ
                link = (f'/org/quotes/raise?rfq_id={fx["rfq_a"]}')
                assert link in awaiting.text
                # quoted rows: "Quote Submitted", no Quote action
                quoted = await w.get("/org/rfqs?status=quoted")
                assert quoted.status_code == 200
                assert fx["ref_b"] in quoted.text
                assert "Quote Submitted" in quoted.text
                assert f'/org/quotes/raise?rfq_id={fx["rfq_b"]}' \
                    not in quoted.text
                assert f'/org/quotes/raise?rfq_id={fx["rfq_d"]}' \
                    not in quoted.text
                # accepted vocabulary preserved
                accepted = await w.get("/org/rfqs?status=accepted")
                assert fx["ref_c"] in accepted.text
                assert "Accepted" in accepted.text

                # the Quote action opens the form preselected on that RFQ
                form = await w.get(link)
                assert form.status_code == 200
                assert f'value="{fx["rfq_a"]}"' in form.text
                assert "MV Alpha Trader" in form.text
                assert fx["ref_a"] in form.text
                assert "Dar Shipping" in form.text

    arun(scenario())


def test_view_rfq_individual_filters(arun, id_asgi):
    """Each filter individually: client (case-insensitive partial), port,
    vessel, reference, inclusive created-date range."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _fixture(idc, w, u)

                by_client = await w.get("/org/rfqs?client=gulf")
                assert fx["ref_b"] in by_client.text
                assert fx["ref_a"] not in by_client.text

                by_port = await w.get("/org/rfqs?port=karachi")
                assert fx["ref_a"] in by_port.text      # Karachi
                assert fx["ref_c"] in by_port.text      # Karachi anchorage
                assert fx["ref_b"] not in by_port.text  # Fujairah
                assert fx["ref_d"] not in by_port.text  # Rotterdam

                by_vessel = await w.get("/org/rfqs?vessel=beta")
                assert fx["ref_b"] in by_vessel.text
                assert fx["ref_a"] not in by_vessel.text

                by_ref = await w.get(f"/org/rfqs?ref={fx['ref_a']}")
                assert fx["ref_a"] in by_ref.text
                assert fx["ref_b"] not in by_ref.text

                # created today: inclusive both directions
                inc = await w.get(f"/org/rfqs?date_from={TODAY}"
                                  f"&date_to={TODAY}")
                for ref in (fx["ref_a"], fx["ref_b"], fx["ref_c"],
                            fx["ref_d"]):
                    assert ref in inc.text, ref
                before = await w.get(f"/org/rfqs?date_to={YESTERDAY}")
                assert fx["ref_a"] not in before.text
                after = await w.get(f"/org/rfqs?date_from={TOMORROW}")
                assert fx["ref_a"] not in after.text

    arun(scenario())


def test_view_rfq_combined_clear_and_edge_dates(arun, id_asgi):
    """Filters compose; Clear/Reset returns to the complete list;
    reversed and invalid dates render the friendly states, never 5xx."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _fixture(idc, w, u)

                # compose: client + status narrows to one row
                combo = await w.get("/org/rfqs?client=gulf&status=quoted")
                assert fx["ref_b"] in combo.text
                assert fx["ref_a"] not in combo.text
                assert fx["ref_d"] not in combo.text
                over = await w.get("/org/rfqs?client=gulf&status=awaiting_quote")
                assert "No RFQs match these filters" in over.text
                assert 'href="/org/rfqs"' in over.text  # Clear filters CTA

                # an impossible combination -> explicit empty state
                none = await w.get("/org/rfqs?client=zzz-no-such")
                assert none.status_code == 200
                assert "No RFQs match these filters" in none.text
                assert "No RFQs yet" not in none.text

                # Clear restores the default-all list
                cleared = await w.get("/org/rfqs")
                for ref in (fx["ref_a"], fx["ref_b"], fx["ref_c"],
                            fx["ref_d"]):
                    assert ref in cleared.text

                # reversed range: clean no-results, no server error
                rev = await w.get(f"/org/rfqs?date_from={TODAY}"
                                  f"&date_to={YESTERDAY}")
                assert rev.status_code == 200
                assert "No RFQs match these filters" in rev.text

                # invalid dates: dropped with a notice, list intact
                bad = await w.get("/org/rfqs?date_from=not-a-date")
                assert bad.status_code == 200
                assert "invalid date filter was ignored" in bad.text
                assert fx["ref_a"] in bad.text

    arun(scenario())


# --------------------------------------------------------------- view quotes

def test_view_quotes_default_all_and_ordering(arun, id_asgi):
    """Default-ALL quotes across every status — drafts, Issued (sent),
    Accepted, Declined, Superseded — newest first, full column set."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _fixture(idc, w, u)
                page = await w.get("/org/quotes")
                assert page.status_code == 200
                for label in (">Draft</span>", ">Issued</span>",
                              ">Accepted</span>", ">Declined</span>",
                              ">Superseded</span>"):
                    assert label in page.text, label
                # column headers — port/location rides the RFQ echo
                for header in ("Quote no", "Client", "Vessel",
                               "RFQ reference", "Port/location",
                               "Quote date", "Valid until", "Total",
                               "Status", "Actions"):
                    assert f">{header}</th>" in page.text, header
                # the issued quote's money renders with currency
                assert "USD" in page.text
                # deterministic newest first (created_at DESC):
                # the declined revision is the newest row
                idx = {k: page.text.index(f'/org/quotes/{v}"')
                       for k, v in (("draft", fx["draft_id"]),
                                    ("issued", fx["issued_id"]),
                                    ("accepted", fx["accepted_id"]),
                                    ("superseded", fx["superseded_id"]),
                                    ("declined", fx["declined_id"]))}
                assert idx["declined"] < idx["superseded"]
                assert idx["superseded"] < idx["accepted"]
                assert idx["accepted"] < idx["issued"]
                assert idx["issued"] < idx["draft"]

    arun(scenario())


def test_view_quotes_filters(arun, id_asgi):
    """Client / port / status / vessel / reference filters, inclusive
    quote-date range, composition, Clear/Reset, no-results state."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _fixture(idc, w, u)

                drafts = await w.get("/org/quotes?status=draft")
                assert ">Draft</span>" in drafts.text
                assert ">Issued</span>" not in drafts.text
                assert f'/org/quotes/{fx["draft_id"]}' in drafts.text
                # drafts carry the Edit row action
                assert f'/org/quotes/{fx["draft_id"]}/edit' in drafts.text
                issued = await w.get("/org/quotes?status=sent")
                assert ">Issued</span>" in issued.text

                by_client = await w.get("/org/quotes?client=gulf")
                assert f'/org/quotes/{fx["issued_id"]}' in by_client.text
                assert f'/org/quotes/{fx["draft_id"]}' not in by_client.text

                by_port = await w.get("/org/quotes?port=rotterdam")
                assert f'/org/quotes/{fx["declined_id"]}' in by_port.text
                assert f'/org/quotes/{fx["issued_id"]}' not in by_port.text

                by_ref = await w.get(f"/org/quotes?ref={fx['ref_b']}")
                assert f'/org/quotes/{fx["issued_id"]}' in by_ref.text
                assert f'/org/quotes/{fx["draft_id"]}' not in by_ref.text

                by_vessel = await w.get("/org/quotes?vessel=alpha")
                assert f'/org/quotes/{fx["draft_id"]}' in by_vessel.text
                assert f'/org/quotes/{fx["issued_id"]}' not in by_vessel.text

                # inclusive quote-date range: all five were quoted today
                inc = await w.get(f"/org/quotes?date_from={TODAY}"
                                  f"&date_to={TODAY}")
                for q in ("draft_id", "issued_id", "accepted_id",
                          "superseded_id", "declined_id"):
                    assert f'/org/quotes/{fx[q]}"' in inc.text, q
                before = await w.get(f"/org/quotes?date_to={YESTERDAY}")
                assert "No quotes match these filters" in before.text
                rev = await w.get(f"/org/quotes?date_from={TODAY}"
                                  f"&date_to={YESTERDAY}")
                assert rev.status_code == 200
                assert "No quotes match these filters" in rev.text
                bad = await w.get("/org/quotes?date_from=garbage")
                assert bad.status_code == 200
                assert "invalid date filter was ignored" in bad.text

                # composition + clear
                combo = await w.get("/org/quotes?client=gulf&status=sent")
                assert f'/org/quotes/{fx["issued_id"]}' in combo.text
                assert f'/org/quotes/{fx["declined_id"]}' not in combo.text
                cleared = await w.get("/org/quotes")
                assert ">Superseded</span>" in cleared.text

    arun(scenario())


# -------------------------------------------------------------- isolation

def test_cross_tenant_lists_and_row_actions_404(arun, id_asgi):
    """Tenant 2's lists (unfiltered AND filtered) contain none of tenant
    1's rows; tenant 1's detail and row-action targets 404 for tenant 2."""
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w1:
                fx = await _fixture(idc, w1, u)
            # tenant 2 on its own client (a logged-in client cookie would
            # redirect /login straight home)
            async with _web() as w2:
                email2, slug2 = await _live_tenant(idc, w2, u + "iso")

                # its unfiltered lists are clean of tenant 1
                rfqs = await w2.get("/org/rfqs")
                assert rfqs.status_code == 200
                for ref in (fx["ref_a"], fx["ref_b"], fx["ref_c"],
                            fx["ref_d"]):
                    assert ref not in rfqs.text, ref
                quotes = await w2.get("/org/quotes")
                assert quotes.status_code == 200
                for q in ("draft_id", "issued_id", "accepted_id"):
                    assert f'/org/quotes/{fx[q]}"' not in quotes.text
                # filtered queries do not leak either
                leaked = await w2.get("/org/rfqs?client=gulf")
                assert fx["ref_b"] not in leaked.text
                leaked_q = await w2.get("/org/quotes?port=rotterdam")
                assert f'/org/quotes/{fx["declined_id"]}' not in leaked_q.text

                # row actions + details are plain 404s
                detail = await w2.get(f"/org/rfqs/{fx['rfq_a']}")
                assert detail.status_code == 404
                quote_page = await w2.get(f"/org/quotes/{fx['issued_id']}")
                assert quote_page.status_code == 404
                edit = await w2.get(f"/org/quotes/{fx['draft_id']}/edit")
                assert edit.status_code == 404

    arun(scenario())


# --------------------------------------------------------- mobile overflow

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
WEB_ROOT = Path(__file__).resolve().parents[1]
LONG = "A" * 120  # hostile unbroken UGC


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_list_tables_contained_at_390px(tmp_path):
    """The 32e tables sit in .sv-table-wrap (deliberate horizontal
    containment): at 390px the wrap scrolls sideways while the page
    itself never overflows — even with hostile UGC in every free-text
    cell."""
    style_uri = (WEB_ROOT / "static" / "style.css").as_uri()
    cells = "".join(
        f'<td class="sv-ugc">{LONG}</td>' for _ in range(10))
    rows = f"<tr>{cells}</tr>" * 3
    page = f"""<!doctype html>
<html><head><link rel="stylesheet" href="{style_uri}"></head>
<body style="margin:0">
<div class="col" style="width:390px">
  <form class="sv-filter-bar" method="get">
    <div class="sv-filter-field"><label for="x">Client</label>
      <input id="x" type="text" value="{LONG}"></div>
    <div class="sv-filter-actions"><button class="button" type="submit">Filter</button></div>
  </form>
  <div class="sv-table-wrap">
    <table class="sv-table"><thead><tr>{
        "".join(f"<th>Header{i}</th>" for i in range(10))
    }</tr></thead><tbody>{rows}</tbody></table>
  </div>
</div>
<script>
(function () {{
  const col = document.querySelector(".col");
  const wrap = document.querySelector(".sv-table-wrap");
  const table = document.querySelector(".sv-table");
  const bad = [];
  // page-level overflow is the failure. The wrap is the sanctioned
  // scroller (its clipped contents legitimately extend past it), so its
  // subtree is excluded; INPUT/TEXTAREA scroll natively inside
  // themselves, which is not layout overflow either. Everything else
  // must stay within the 390px column's right edge.
  const colRight = col.getBoundingClientRect().right;
  col.querySelectorAll("*").forEach((n) => {{
    if (wrap.contains(n)) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(n.tagName)) return;
    const r = n.getBoundingClientRect();
    if (r.right > colRight + 1 || n.scrollWidth > n.clientWidth + 1)
      bad.push(n.tagName + "." + (n.className || "-"));
  }});
  document.body.setAttribute("data-verdict", JSON.stringify({{
    colWidth: col.clientWidth,
    bad: bad.join("|"),
    wrapped: wrap.clientWidth < table.scrollWidth,
  }}));
}})();
</script>
</body></html>"""
    tmp = tmp_path / "lists-390.html"
    tmp.write_text(page)
    out = subprocess.run(
        [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
         "--virtual-time-budget=3000", "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, timeout=60,
    ).stdout
    m = re.search(r'data-verdict="(.*?)"', out)
    assert m, out[-800:]
    import html as _html
    import json as _json
    verdict = _json.loads(_html.unescape(m.group(1)))
    assert verdict["colWidth"] == 390, verdict
    assert verdict["bad"] == "", f"page-level overflow: {verdict}"
    assert verdict["wrapped"], "the wrap is not containing the wide table"
