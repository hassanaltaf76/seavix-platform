"""31s: the public-profile logo pinned to the card's TOP-LEFT corner —
owner review shots.

Uploads a REAL logo (64x64 navy square, generated in-test) through the
profile logo endpoint, points the rendered <img> at the actual blob file
(file URI — headless Chrome loads it), sets the org LIVE in-script, and
screenshots the header on the live /org/<slug> page AND the view-mode
preview. Both render the SAME shared partial, so the corner layout must
look identical on both surfaces (31f byte-equality).

Proof lands in var/screenshots/31s-*.png. Skips cleanly without Chrome.
"""
import struct
import uuid
import zlib
from pathlib import Path

import httpx
import pytest

from test_page_shell_screenshots import _shot
from test_profile_preview_ports import (
    _register_and_login,
    _verify_email_and_go_live,
    _web,
)

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _navy_logo_png(size: int = 64) -> bytes:
    """A valid solid-navy square PNG (magic bytes + real pixels)."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + bytes((11, 36, 71)) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + ihdr + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _inline_logo(html: str, blob_key: str, root: Path) -> str:
    """The page references the logo at /media/<key> (dev: streamed by the
    web app); for the headless render point the <img> at the blob file
    itself so the real mark shows in the corner."""
    file_uri = (root / blob_key).resolve().as_uri()
    return html.replace(f'src="/media/{blob_key}"', f'src="{file_uri}"')


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_logo_corner_screenshot_set_31s(arun, id_asgi, tmp_path):
    async def scenario():
        import os
        import sys

        sys.path.insert(0, os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..",
                         "services", "identity")))
        from sqlalchemy import text as sql

        from app.config import blob_root
        from app.db import SessionLocal

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            email = f"lg-{u}@example.com"
            slug = f"logo-{u}"
            async with _web() as w:
                sid = await _register_and_login(idc, w, email, slug)
                saved = await w.post(
                    "/org/profile",
                    data={"slug": slug, "about": "Independent marine surveyors."},
                    follow_redirects=False,
                )
                assert saved.status_code == 303

                # upload a real logo through the profile endpoint
                logo = await w.post(
                    "/org/profile/logo",
                    files={"file": ("logo.png", _navy_logo_png(), "image/png")},
                )
                assert logo.status_code == 200, logo.text
                assert "Logo updated." in logo.text

                # set the org LIVE in-script — the public page must answer
                await _verify_email_and_go_live(idc, email, sid)

                # the org's logo blob key (the media file is on disk)
                async with SessionLocal() as s:
                    key = (
                        await s.execute(
                            sql("SELECT logo_blob_key FROM organization "
                                "WHERE slug = :slug"), {"slug": slug})
                    ).scalar_one()
                assert key

                # 1. live /org/<slug> — logo in the top-left corner
                pub = await w.get(f"/org/{slug}")
                assert pub.status_code == 200
                assert "sv-org-header--corner" in pub.text
                assert f'src="/media/{key}"' in pub.text
                _shot(_inline_logo(pub.text, key, blob_root()),
                      tmp_path, "live-header", 1280, 860, prefix="31s")

                # 2. view-mode preview — the SAME partial, same corner
                view = await w.get("/org/profile")
                assert view.status_code == 200
                assert "sv-org-header--corner" in view.text
                assert f'src="/media/{key}"' in view.text
                _shot(_inline_logo(view.text, key, blob_root()),
                      tmp_path, "preview-header", 1280, 1000, prefix="31s")

    arun(scenario())
