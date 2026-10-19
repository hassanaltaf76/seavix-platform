"""Registry API tests — shared-loop (see conftest.py)."""
import os
import random
import uuid

import httpx

os.environ.setdefault(
    "REGISTRY_DATABASE_URL",
    "postgresql+asyncpg://seavix:dev@localhost:5432/seavix_registry",
)

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.main import app  # noqa: E402


def _vessel(imo: str, name: str, **overrides):
    body = {
        "imo": imo,
        "name": name,
        "cargo_class": "liquid",
        "ship_type": "oil_tanker",
        "size_class": "other",
        "pump_type": "steam_turbine",
    }
    body.update(overrides)
    return body


def test_healthz(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            resp = await c.get("/healthz")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok", "service": "registry"}

    arun(scenario())


def test_upsert_idempotency(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            imo = "9" + "".join(random.choices("0123456789", k=6))
            frag = uuid.uuid4().hex[:8]
            # create
            r1 = await c.post("/vessels", json=_vessel(imo, f"MV Test One {frag}"))
            assert r1.status_code == 200 and r1.json() == {"imo": imo, "result": "created"}
            # update same IMO — no duplicate
            r2 = await c.post(
                "/vessels",
                json=_vessel(imo, f"MV Test One Renamed {frag}", dwt=5000, flag="MT"),
            )
            assert r2.status_code == 200 and r2.json() == {"imo": imo, "result": "updated"}
            # exactly one row, updated fields present
            g = await c.get(f"/vessels/{imo}")
            assert g.status_code == 200
            body = g.json()
            assert body["name"] == f"MV Test One Renamed {frag}"
            assert body["dwt"] == 5000
            assert body["flag"] == "MT"
            s = await c.get("/vessels", params={"search": frag})
            assert len(s.json()["vessels"]) == 1

    arun(scenario())


def test_search_fragment(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            frag = uuid.uuid4().hex[:8]
            imo_a = "8" + "".join(random.choices("0123456789", k=6))
            imo_b = "7" + "".join(random.choices("0123456789", k=6))
            await c.post("/vessels", json=_vessel(imo_a, f"Aurora {frag}"))
            await c.post("/vessels", json=_vessel(imo_b, "Unrelated Name"))

            resp = await c.get("/vessels", params={"search": frag.lower()})
            names = [v["name"] for v in resp.json()["vessels"]]
            assert f"Aurora {frag}" in names
            assert "Unrelated Name" not in names

            # the official UN/LOCODE name is "Al Fujayrah" (the 31f import
            # superseded the old hand-seeded "Fujairah" label)
            ports = await c.get("/ports", params={"search": "fujayrah"})
            assert any(p["code"] == "AEFJR" for p in ports.json()["ports"])
            # country search: exact country code ranks first (31f)
            ports_sg = await c.get("/ports", params={"search": "SG"})
            assert ports_sg.json()["ports"][0]["country"] == "SG"

    arun(scenario())


def test_ports_search_by_name_code_and_country(arun):
    """31f combobox backend: search matches name, UN/LOCODE or country."""
    async def scenario():
        from sqlalchemy import text as sql

        from registry.db import SessionLocal

        async with SessionLocal() as s:
            await s.execute(
                sql(
                    "INSERT INTO port (code, name, country) VALUES "
                    "(:code, :name, :country) ON CONFLICT (code) DO NOTHING"
                ),
                [
                    {"code": "AEFJR", "name": "Al Fujayrah", "country": "AE"},
                    {"code": "SGSIN", "name": "Singapore", "country": "SG"},
                    {"code": "NLRTM", "name": "Rotterdam", "country": "NL"},
                ],
            )
            await s.commit()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            by_name = await c.get("/ports", params={"search": "rotterdam"})
            assert any(p["code"] == "NLRTM" for p in by_name.json()["ports"])
            by_code = await c.get("/ports", params={"search": "aefjr"})
            assert any(p["code"] == "AEFJR" for p in by_code.json()["ports"])
            by_country = await c.get("/ports", params={"search": "SG"})
            codes = [p["code"] for p in by_country.json()["ports"]]
            assert "SGSIN" in codes
            assert "NLRTM" not in codes  # ILIKE on country only for SG

    arun(scenario())


def test_ports_lookup_request_order_and_unknown_omitted(arun):
    """31f: bulk code resolution preserves request order and drops codes
    the registry does not know."""
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(
                "/ports/lookup", params={"codes": "SGSIN,XXXXX,NLRTM,AEFJR"}
            )
            ports = r.json()["ports"]
            assert [p["code"] for p in ports] == ["SGSIN", "NLRTM", "AEFJR"]
            assert all("name" in p and "country" in p for p in ports)
            empty = await c.get("/ports/lookup", params={"codes": ""})
            assert empty.json() == {"ports": []}

    arun(scenario())


def test_bad_imo_422(arun):
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            bad = await c.post("/vessels", json=_vessel("12345", "Short IMO"))
            assert bad.status_code == 422
            bad2 = await c.post("/vessels", json=_vessel("12345678", "Long IMO"))
            assert bad2.status_code == 422
            bad3 = await c.post("/vessels", json=_vessel("ABCDEFG", "Alpha IMO"))
            assert bad3.status_code == 422
            g = await c.get("/vessels/123")
            assert g.status_code == 422

    arun(scenario())
