"""31j public-address visibility in the profile editor.

The /pak-marines 404 class of confusion: owners couldn't tell WHERE their
public page was or WHY an address 404'd. The editor must now state the
current address state plainly:

- auto-generated placeholder slug -> a PROMINENT "Choose your public
  address: seavix.ai/<slug>" prompt (not a subtle hint)
- a chosen slug -> "Your public page: seavix.ai/<slug>" (+ View link)
- the reachability rule inline in every case: the address works only
  while the profile is LIVE; a draft shows visitors a not-found page —
  which is exactly what /<slug> does for a draft org.
"""
import re
import uuid

import httpx
import pytest

from webapp.auth import SESSION_COOKIE


def _web() -> httpx.AsyncClient:
    from webapp.main import app as web_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web"
    )


async def _register_and_login(idc, w, email, org_name="Visibility Org"):
    resp = await idc.post(
        "/api/register",
        json={
            "org_name": org_name,
            "org_type": "survey_company",
            "email": email,
            "password": "secret-123",
            "confirm_password": "secret-123",
        },
    )
    assert resp.status_code == 201, resp.text
    generated_slug = resp.json()["slug"]
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(
            sql("UPDATE app_user SET email_verified = true WHERE email = :e"),
            {"e": email},
        )
        await s.commit()
    start = await w.post("/login", data={"email": email, "password": "secret-123"})
    assert "Check your inbox" in start.text
    code = (await idc.get("/api/dev/last-otp", params={"email": email})).json()[
        "code"
    ]
    ok = await w.post(
        "/login/otp", data={"email": email, "code": code}, follow_redirects=False
    )
    assert ok.status_code == 303
    sid = re.search(rf"{SESSION_COOKIE}=([^;]+)", ok.headers["set-cookie"]).group(1)
    w.cookies.set(SESSION_COOKIE, sid)
    return sid, generated_slug


def test_placeholder_slug_gets_prominent_choose_address_prompt(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"sv-{uuid.uuid4().hex[:8]}@example.com"
            async with _web() as w:
                _, generated_slug = await _register_and_login(idc, w, email)
                assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{6}", generated_slug)

                editor = await w.get("/org/profile?view=edit")
                assert editor.status_code == 200
                prompt = f"Choose your public address: seavix.ai/org/{generated_slug}"
                assert prompt in editor.text
                # draft -> 404 rule stated inline, one line
                assert "not-found page" in editor.text
                assert "reachable" in editor.text
                # a chosen-slug page link has no business here yet
                assert "Your public page:" not in editor.text

                # the public address 404s while draft, with the same story
                public = await w.get(f"/org/{generated_slug}")
                assert public.status_code == 404
                assert "isn&#39;t public" in public.text or "isn't public" in public.text

    arun(scenario())


def test_setting_slug_updates_framing_and_live_adds_view_link(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            email = f"sv-{uuid.uuid4().hex[:8]}@example.com"
            async with _web() as w:
                sid, _ = await _register_and_login(idc, w, email)
                slug = f"acme-{uuid.uuid4().hex[:6]}"

                # save a real slug through the editor Save button
                saved = await w.post(
                    "/org/profile", data={"slug": slug, "about": "Surveys"}
                )
                assert saved.status_code == 303
                assert saved.headers["location"] == "/org/profile?saved=1"

                # framing flips: chosen slug, still draft (no View link yet)
                editor = await w.get("/org/profile?view=edit")
                assert f"Your public page: seavix.ai/org/{slug}" in editor.text
                assert "Choose your public address" not in editor.text
                assert "View it live" not in editor.text
                assert "not-found page" in editor.text
                public = await w.get(f"/org/{slug}")
                assert public.status_code == 404

                # go live: the View link appears and the address serves
                live = await idc.post(
                    "/api/orgs/me/profile-status",
                    headers={"X-Seavix-Session": sid},
                    json={"status": "live"},
                )
                assert live.status_code == 200, live.text
                editor = await w.get("/org/profile?view=edit")
                assert f"Your public page: seavix.ai/org/{slug}" in editor.text
                assert f'href="/org/{slug}"' in editor.text  # View it live
                assert "not-found page" not in editor.text
                public = await w.get(f"/org/{slug}")
                assert public.status_code == 200
                assert "Surveys" in public.text

    arun(scenario())
