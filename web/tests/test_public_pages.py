"""Public web app tests — identity service stubbed at the HTTP boundary.

Honeypot and rate-limit tests never reach identity (honeypot short-circuits
before the org lookup; the limiter trips first), so a stubbed _identity_get
keeps these tests hermetic. End-to-end with live services was verified
manually (reports/12-public-web-app.md).
"""
import asyncio
import os
import sqlite3

import httpx
import pytest

from webapp import pages
from webapp.main import app
from webapp.rfq import RateLimiter

LIVE_ORG = {
    "slug": "harbor-surveys",
    "name": "Harbor Surveys Ltd",
    "about": "Independent surveyors.",
    "website": "https://harbor.example",
    "phone": "+1 555 0100",
    "logo_url": None,
    "profile_status": "live",
}


class StubResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture()
def web(monkeypatch, tmp_path):
    monkeypatch.setenv("SEAVIX_RFQ_DB", str(tmp_path / "web.db"))
    fresh_limiter = RateLimiter(limit=5, window_seconds=3600)
    monkeypatch.setattr(pages, "limiter", fresh_limiter)
    monkeypatch.setattr(
        pages, "_identity_get",
        lambda path: _stub(path),
    )

    async def _client():
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://web")

    return _client


async def _stub(path: str) -> StubResp:
    slug = path.rsplit("/", 1)[-1]
    if slug == LIVE_ORG["slug"]:
        return StubResp(200, LIVE_ORG)
    return StubResp(404, {"detail": "organization not found"})


def _run(coro):
    return asyncio.run(coro)


def test_landing_and_register_form_render(web):
    async def scenario():
        async with (await web()) as c:
            landing = await c.get("/")
            assert landing.status_code == 200
            assert "SeaVix" in landing.text

            form = await c.get("/register")
            assert form.status_code == 200
            assert 'name="account_kind"' in form.text
            assert 'value="survey_company"' in form.text
            assert 'value="client"' in form.text

    _run(scenario())


def test_profile_page_renders_and_hides_email(web):
    async def scenario():
        async with (await web()) as c:
            resp = await c.get(f"/p/{LIVE_ORG['slug']}")
            assert resp.status_code == 200
            assert LIVE_ORG["name"] in resp.text
            assert "Raise an RFQ" in resp.text
            assert LIVE_ORG["about"] in resp.text
            assert "@harbor.example" not in resp.text  # no email on public page

    _run(scenario())


def test_non_live_and_unknown_profiles_show_friendly_page(web):
    async def scenario():
        async with (await web()) as c:
            unknown = await c.get("/p/does-not-exist")
            assert unknown.status_code == 404
            assert "Profile not available" in unknown.text

            async def draft_stub(path: str) -> StubResp:
                if path.rsplit("/", 1)[-1] == "draft-co":
                    return StubResp(200, {**LIVE_ORG, "slug": "draft-co",
                                          "profile_status": "draft"})
                return StubResp(404, {})

            pages._identity_get = draft_stub
            draft = await c.get("/p/draft-co")
            assert draft.status_code == 404
            assert "still a draft" in draft.text

    _run(scenario())


def test_honeypot_rejects_bots(web, tmp_path):
    async def scenario():
        async with (await web()) as c:
            resp = await c.post(
                f"/p/{LIVE_ORG['slug']}/rfq",
                data={
                    "vessel_imo": "9074729",
                    "survey_type": "condition",
                    "location_type": "OPL",
                    "website": "http://spam.example",  # bot filled the trap
                },
            )
            assert resp.status_code == 200  # fake success
            assert "Thank you" in resp.text

    _run(scenario())
    db = sqlite3.connect(tmp_path / "web.db")
    table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='rfq'"
    ).fetchone()
    # honeypot must persist nothing — often the db file/table is never created
    assert table is None or db.execute("SELECT count(*) FROM rfq").fetchone()[0] == 0


def _rfq_data():
    return {
        "vessel_imo": "9074729",
        "survey_type": "condition",
        "location_type": "anchorage",
        "preferred_date": "2026-11-01",
        "scope": "Annual condition survey",
    }


def test_rfq_persists_and_thanks(web, tmp_path):
    async def scenario():
        async with (await web()) as c:
            resp = await c.post(f"/p/{LIVE_ORG['slug']}/rfq", data=_rfq_data())
            assert resp.status_code == 200
            assert "Thank you" in resp.text

    _run(scenario())
    db = sqlite3.connect(tmp_path / "web.db")
    rows = db.execute("SELECT org_slug, survey_type FROM rfq").fetchall()
    assert rows == [(LIVE_ORG["slug"], "condition")]


def test_rate_limit_trips_on_sixth_submission(web):
    async def scenario():
        async with (await web()) as c:
            codes = []
            for _ in range(6):
                resp = await c.post(f"/p/{LIVE_ORG['slug']}/rfq", data=_rfq_data())
                codes.append(resp.status_code)
            assert codes[:5] == [200] * 5
            assert codes[5] == 429

    _run(scenario())
