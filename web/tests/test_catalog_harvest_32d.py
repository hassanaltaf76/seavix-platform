"""32d: custom catalog entries + harvest, end to end through the REAL
services (identity + work + web in-process on one loop).

Covers: create-from-picker persists (origin custom + org provenance, via
the identity tests) and surfaces in a SECOND org's picker search (system
first, custom after, origin for the badge); duplicate create selects the
existing entry; the profile Save persists a custom chip and the public
page renders it; the RFQ form's survey-type picker works anonymously
(search + create with NULL provenance) and the submitted multi-select is
stored on the work RFQ (rfq_survey_type).
"""
import uuid

import httpx
from sqlalchemy import text as sql

from test_profile_preview_ports import (
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)


def test_custom_entry_harvest_end_to_end(arun, id_asgi):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            label = f"Underwater welding {u}"
            slug_a = f"harvest-a-{u}"
            async with _web() as wa:
                sid_a = await _register_and_login(
                    idc, wa, f"harv-a-{u}@example.com", slug_a
                )
                await _verify_email_and_go_live(idc, f"harv-a-{u}@example.com",
                                                sid_a)

                # org A creates a custom service via the picker proxy
                made = await wa.post("/org/services/create",
                                     json={"label": f"  {label} "})
                assert made.status_code == 201, made.text
                code = made.json()["code"]
                assert made.json()["origin"] == "custom"

                # org A saves its profile with the custom chip
                saved = await wa.post(
                    "/org/profile",
                    data={"slug": slug_a, "services": [code]},
                    follow_redirects=False,
                )
                assert saved.status_code == 303, saved.text[:300]

            # org B (fresh session) sees the harvest, system rows first
            async with _web() as wb:
                await _register_and_login(idc, wb, f"harv-b-{u}@example.com",
                                          f"harvest-b-{u}")
                found = await wb.get("/org/services/search",
                                     params={"q": label})
                assert found.status_code == 200
                entries = found.json()["services"]
                mine = [e for e in entries if e["label"] == label]
                assert len(mine) == 1 and mine[0]["origin"] == "custom"
                origins = [e["origin"] for e in entries]
                assert origins == sorted(origins, key=lambda o: o != "system")

                # duplicate create SELECTS the existing entry (200 + code)
                dup = await wb.post("/org/services/create",
                                    json={"label": label.upper()})
                assert dup.status_code == 200, dup.text
                assert dup.json()["code"] == code

            # the public page renders the custom chip for org A
            async with _web() as wpub:
                pub = await wpub.get(f"/org/{slug_a}")
                assert pub.status_code == 200
                assert label in pub.text

                # the RFQ form picker is anonymous and catalog-backed
                form = await wpub.get(f"/org/{slug_a}/rfq")
                assert form.status_code == 200
                assert 'id="survey-type-search"' in form.text
                assert "sv-chip-picker" in form.text

                search = await wpub.get("/org/rfq/survey-types/search",
                                        params={"q": "condition"})
                assert search.status_code == 200
                assert any(e["code"] == "condition"
                           for e in search.json()["survey_types"])

                created = await wpub.post("/org/rfq/survey-types/create",
                                          json={"label": f"Drone inspection {u}"})
                assert created.status_code == 201, created.text
                custom_st = created.json()["code"]

                # submit the RFQ with TWO survey types (system + custom)
                from webapp import pages
                pages.limiter._hits.clear()  # shared dev limiter: fresh key
                rfq = await wpub.post(
                    f"/org/{slug_a}/rfq",
                    data={
                        "vessel_imo": "9074729",
                        "survey_types": ["pre_purchase", custom_st],
                        "location_type": "anchorage",
                        "contact_email": f"charterer-{u}@example.com",
                    },
                )
                assert rfq.status_code == 200, rfq.text[:300]
                assert "Thank you" in rfq.text

            # the work RFQ stored the org-independent multi-select
            from work.db import SessionLocal

            async with SessionLocal() as s:
                rfq_id = (await s.execute(
                    sql("SELECT id FROM rfq WHERE contact_email = :e "
                        "ORDER BY created_at DESC LIMIT 1"),
                    {"e": f"charterer-{u}@example.com"})).scalar_one()
                codes = set((await s.execute(
                    sql("SELECT survey_type FROM rfq_survey_type "
                        "WHERE rfq_id = :id"), {"id": rfq_id})).scalars().all())
                assert codes == {"pre_purchase", custom_st}
                # legacy column carries the FIRST submitted code
                legacy = (await s.execute(
                    sql("SELECT survey_type FROM rfq WHERE id = :id"),
                    {"id": rfq_id})).scalar_one()
                assert legacy == "pre_purchase"

    arun(scenario())
