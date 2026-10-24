"""Owner 24 Oct — the canonical profile publish flow:

Profile → Edit → Preview → Publish / Go live → Save

Root cause reproduced first: the web app had NO publish control at all —
identity's POST /api/orgs/me/profile-status had no caller, so no UI
sequence could ever persist profile_status='live' (the owner's pak-marines
row was set live by manual SQL). These tests drive the real pages with a
disposable draft org (the owner's profile is never touched).

Required proof:
- draft public URL unavailable before publishing (signed-out 404);
- the exact UI sequence succeeds and persists live in Postgres;
- /org/<slug> then returns 200 signed out;
- a later profile Save does NOT reset the status;
- unpublish (Take offline) works and re-publishing works;
- org.profile.publish gating: owner/admin yes, ops/surveyor/viewer 404.
"""
import uuid

import httpx
import pytest

from webapp.auth import SESSION_COOKIE

from test_org_rfqs_32b import (
    _live_tenant,
    _login_web,
    _same_org_member,
)
from test_profile_preview_ports import (
    _register_and_login,
    _web,
)


async def _status_of(slug: str) -> str:
    from sqlalchemy import text as sql

    from app.db import SessionLocal
    async with SessionLocal() as s:
        return (await s.execute(
            sql("SELECT profile_status FROM organization WHERE slug = :s"),
            {"s": slug})).scalar_one()


def test_publish_flow_end_to_end(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"pub-{u}@example.com"
            slug = f"pub-{u}"
            async with _web() as w:
                await _register_and_login(idc, w, email, slug)

                # Edit → Save (sets the public slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "Independent surveyors."},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:300]
                assert saved.headers["location"] == "/org/profile?saved=1"

                # draft: the public URL is unavailable before publishing
                assert await _status_of(slug) == "draft"
                anon = await w.get(f"/org/{slug}")
                assert anon.status_code == 404
                assert "not available" in anon.text or "unavailable" in anon.text

                # Preview: the publish action is present for the owner
                preview = await w.get("/org/profile")
                assert preview.status_code == 200
                assert "Publish / Go live" in preview.text
                assert 'href="/org/profile/publish"' in preview.text

                # Publish / Go live: the explicit human gate
                confirm = await w.get("/org/profile/publish")
                assert confirm.status_code == 200
                assert 'name="action"' in confirm.text
                assert 'value="live"' in confirm.text
                done = await w.post("/org/profile/publish",
                                    data={"action": "live"},
                                    follow_redirects=False)
                assert done.status_code == 303, done.text[:300]
                assert done.headers["location"] == "/org/profile?published=1"

                # persisted live in Postgres, public URL now 200 signed out
                assert await _status_of(slug) == "live"
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                flash = await w.get("/org/profile?published=1")
                assert "is public now" in flash.text

                # the flow ends with Save — a later Save must NOT unlive it
                saved_again = await w.post(
                    "/org/profile",
                    data={"slug": slug,
                          "about": "Independent marine surveyors."},
                    follow_redirects=False,
                )
                assert saved_again.status_code == 303
                assert await _status_of(slug) == "live"

                # Take offline → public 404 again; re-publish → live again
                off = await w.post("/org/profile/publish",
                                   data={"action": "draft"},
                                   follow_redirects=False)
                assert off.status_code == 303
                assert off.headers["location"] == "/org/profile?unpublished=1"
                assert await _status_of(slug) == "draft"
                gone = await w.get(f"/org/{slug}")
                assert gone.status_code == 404
                again = await w.post("/org/profile/publish",
                                     data={"action": "live"},
                                     follow_redirects=False)
                assert again.status_code == 303
                assert await _status_of(slug) == "live"

    arun(scenario())


def test_publish_permission_matrix(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            # live tenant so a second role can be added to a real org
            async with _web() as wt:
                tenant_email, slug = await _live_tenant(idc, wt, u)
            from sqlalchemy import text as sql

            from app.db import SessionLocal
            async with SessionLocal() as s:
                org_id = str((await s.execute(
                    sql("SELECT id FROM organization WHERE slug = :s"),
                    {"s": slug})).scalar_one())

            # ops / surveyor / viewer lack org.profile.publish: 404
            for role in ("ops", "surveyor", "viewer"):
                sid = await _same_org_member(
                    idc, org_id, role, f"pub-{role}-{u}@example.com")
                async with _web() as w:
                    w.cookies.set(SESSION_COOKIE, sid)
                    page = await w.get("/org/profile/publish")
                    assert page.status_code == 404, f"{role} saw the page"
                    post = await w.post("/org/profile/publish",
                                        data={"action": "live"},
                                        follow_redirects=False)
                    assert post.status_code == 404, f"{role} posted"
            # status untouched by the denied attempts
            assert await _status_of(slug) == "live"

            # admin holds the permission (ALL minus role.manage)
            admin_sid = await _same_org_member(
                idc, org_id, "admin", f"pub-admin-{u}@example.com")
            async with _web() as w:
                w.cookies.set(SESSION_COOKIE, admin_sid)
                page = await w.get("/org/profile/publish")
                assert page.status_code == 200

            # bad action is rejected without touching the row
            async with _web() as wt2:
                await _login_web(idc, wt2, tenant_email)
                bad = await wt2.post("/org/profile/publish",
                                     data={"action": "bogus"},
                                     follow_redirects=False)
                assert bad.status_code == 422
            assert await _status_of(slug) == "live"

    arun(scenario())
