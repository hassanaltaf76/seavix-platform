"""Public web app tests — service boundaries stubbed at the HTTP helpers.

_identity_get (identity service) and _post_public_rfq (work service) are
stubbed; honeypot and rate-limit tests never reach either (honeypot
short-circuits, limiter trips first). End-to-end with live services is
verified manually and in task 19 (reports/18-absorb-sqlite-rfq.md).
"""
import asyncio

import httpx
import pytest

from webapp import pages
from webapp.main import app
from webapp.ratelimit import RateLimiter

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
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


async def _stub_identity(path: str) -> StubResp:
    slug = path.rsplit("/", 1)[-1]
    if slug == LIVE_ORG["slug"]:
        return StubResp(200, LIVE_ORG)
    return StubResp(404, {"detail": "organization not found"})


@pytest.fixture()
def web(monkeypatch):
    fresh_limiter = RateLimiter(limit=5, window_seconds=3600)
    monkeypatch.setattr(pages, "limiter", fresh_limiter)
    monkeypatch.setattr(pages, "_identity_get", _stub_identity)

    posted: list[dict] = []

    async def fake_post(payload: dict) -> StubResp:
        posted.append(payload)
        if payload["org_slug"] == LIVE_ORG["slug"] and payload["contact_email"]:
            return StubResp(201, {"id": "rfq-1", "status": "open"})
        return StubResp(400, {"detail": "invalid RFQ"})

    monkeypatch.setattr(pages, "_post_public_rfq", fake_post)

    async def _client():
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://web")

    return {"client": _client, "posted": posted}


def _run(coro):
    return asyncio.run(coro)


def _rfq_data():
    return {
        "vessel_imo": "9074729",
        "survey_type": "condition",
        "location_type": "anchorage",
        "contact_email": "charterer@example.com",
        "preferred_date": "2026-11-01",
        "scope": "Annual condition survey",
    }


def test_landing_and_register_form_render(web):
    async def scenario():
        async with (await web["client"]()) as c:
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
        async with (await web["client"]()) as c:
            resp = await c.get(f"/p/{LIVE_ORG['slug']}")
            assert resp.status_code == 200
            assert LIVE_ORG["name"] in resp.text
            assert "Raise an RFQ" in resp.text
            assert LIVE_ORG["about"] in resp.text
            assert "@harbor.example" not in resp.text  # no email on public page

    _run(scenario())


def test_non_live_and_unknown_profiles_show_friendly_page(web):
    async def scenario():
        async with (await web["client"]()) as c:
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


def test_honeypot_never_reaches_work_service(web):
    async def scenario():
        async with (await web["client"]()) as c:
            resp = await c.post(
                f"/p/{LIVE_ORG['slug']}/rfq",
                data={**_rfq_data(), "website": "http://spam.example"},  # bot trap
            )
            assert resp.status_code == 200  # fake success
            assert "Thank you" in resp.text

    _run(scenario())
    assert web["posted"] == []  # nothing was forwarded to the work service


def test_rfq_forwards_to_work_service(web):
    async def scenario():
        async with (await web["client"]()) as c:
            resp = await c.post(f"/p/{LIVE_ORG['slug']}/rfq", data=_rfq_data())
            assert resp.status_code == 200
            assert "Thank you" in resp.text

    _run(scenario())
    (payload,) = web["posted"]
    assert payload["org_slug"] == LIVE_ORG["slug"]
    assert payload["contact_email"] == "charterer@example.com"
    assert payload["survey_type"] == "condition"
    assert payload["vessel_imo"] == "9074729"


def test_work_service_rejection_renders_form_error(web):
    async def scenario():
        async with (await web["client"]()) as c:
            bad = {**_rfq_data(), "contact_email": ""}  # fails work validation
            resp = await c.post(f"/p/{LIVE_ORG['slug']}/rfq", data=bad)
            assert resp.status_code == 400
            assert "error" in resp.text.lower()

    _run(scenario())


def test_rate_limit_trips_on_sixth_submission(web):
    async def scenario():
        async with (await web["client"]()) as c:
            codes = []
            for _ in range(6):
                resp = await c.post(f"/p/{LIVE_ORG['slug']}/rfq", data=_rfq_data())
                codes.append(resp.status_code)
            assert codes[:5] == [200] * 5
            assert codes[5] == 429

    _run(scenario())
    # limiter tripped before the 6th forward: exactly 5 calls reached work
    assert len(web["posted"]) == 5
