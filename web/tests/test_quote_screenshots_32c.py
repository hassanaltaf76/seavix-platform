"""32c: the owner-review screenshot set — six shots covering the quotation
surfaces end to end:

1. the raise form (RFQ summary card, line-item rows, Save bar forced)
2. the document preview (the .sv-doc quotation card, draft state)
3. the RFQ detail Quotes section (revisions, status badges, raise CTA)
4. the issued document (approval block, revision action)
5. the quotes list (populated with the issued row, formatted totals)
6. the raise form at 390px (mobile — CDP device-metrics override; Chrome
   clamps --window-size below ~500px, so a plain window-size shot would
   crop the page's right edge)

Identity + work + web run in-process; every row is created through the
REAL pages. Proof lands in var/screenshots/32c-*.png; skips cleanly
without Chrome.
"""
import base64
import json
import os
import select
import subprocess
import time
import uuid

import httpx
import pytest

from test_org_quotes_32c import (
    QUOTE_FORM,
    _issue,
    _raise_draft,
    _tenant_client_rfq,
)
from test_page_shell_screenshots import CHROME, SHOTS, WEB_ROOT, _shot
from test_profile_preview_ports import _web

FORCE_SAVE_BAR = "<style>.sv-save-bar{display:flex !important}</style>"


def _shot_mobile(html: str, tmp_path, name: str, width: int, height: int,
                 extra: str = "", prefix: str = "32c") -> None:
    """True-width mobile shot via the DevTools protocol's
    Emulation.setDeviceMetricsOverride — the same mechanism a real phone
    emulation uses. Needed because this Chrome clamps --window-size below
    ~500px (a --window-size=390 shot opens wider and the PNG crops the
    page's right edge). Chrome is driven over the --remote-debugging-pipe
    fds (3 = commands in, 4 = events out); no websockets, no extra deps."""
    page = html.replace(
        'href="/static/style.css"',
        f'href="{(WEB_ROOT / "static" / "style.css").as_uri()}"',
    ).replace(
        'src="/static/',
        f'src="{(WEB_ROOT / "static").as_uri()}/',
    )
    if extra:
        page = page.replace("</body>", extra + "\n</body>")
    f = tmp_path / f"{prefix}-{name}.html"
    f.write_text(page)

    cmd_r, cmd_w = os.pipe()    # test writes, chrome reads (fd 3)
    out_r, out_w = os.pipe()    # chrome writes, test reads (fd 4)

    def _preexec() -> None:
        # os.pipe() can return 3/4 (uv keeps them open), which would make
        # dup2(3, 3) a no-op and leave O_CLOEXEC set — chrome then sees a
        # closed fd 3 at exec. Dup to fresh high fds first, then onto 3/4.
        a = os.dup(cmd_r)
        b = os.dup(out_w)
        os.dup2(a, 3)
        os.dup2(b, 4)
        os.close(a)
        os.close(b)

    proc = subprocess.Popen(
        [str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
         "--remote-debugging-pipe",
         f"--user-data-dir={tmp_path / "cdp-profile"}", "about:blank"],
        preexec_fn=_preexec, close_fds=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(cmd_r)
    os.close(out_w)

    buf = b""
    next_id = 0

    def send(method: str, params: dict, session: str | None = None) -> int:
        nonlocal next_id
        next_id += 1
        msg = {"id": next_id, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        os.write(cmd_w, json.dumps(msg).encode() + b"\0")
        return next_id

    def read_msg(deadline: float) -> dict:
        nonlocal buf
        while True:
            i = buf.find(b"\0")
            if i != -1:
                msg, buf = buf[:i], buf[i + 1:]
                return json.loads(msg)
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"cdp timeout, buffered={buf[:200]!r}")
            ready, _, _ = select.select([out_r], [], [], left)
            if ready:
                chunk = os.read(out_r, 65536)
                if not chunk:
                    raise RuntimeError("chrome closed the debug pipe")
                buf += chunk

    def wait_for(mid: int, timeout: float = 30) -> dict:
        deadline = time.time() + timeout
        while True:
            msg = read_msg(deadline)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    try:
        target = wait_for(send("Target.createTarget",
                               {"url": "about:blank"}))["targetId"]
        session = wait_for(send(
            "Target.attachToTarget",
            {"targetId": target, "flatten": True}))["sessionId"]
        wait_for(send("Page.enable", {}, session))
        wait_for(send("Emulation.setDeviceMetricsOverride",
                      {"width": width, "height": height,
                       "deviceScaleFactor": 1, "mobile": True}, session))
        send("Page.navigate", {"url": f.as_uri()}, session)
        deadline = time.time() + 30
        while True:
            msg = read_msg(deadline)
            if (msg.get("method") == "Page.loadEventFired"
                    and msg.get("sessionId") == session):
                break
        time.sleep(1.0)  # settle: Alpine init + local fonts
        result = wait_for(send("Page.captureScreenshot",
                               {"format": "png"}, session))
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / f"{prefix}-{name}.png").write_bytes(
            base64.b64decode(result["data"]))
    finally:
        try:
            send("Browser.close", {})
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        os.close(cmd_w)
        os.close(out_r)


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_quote_screenshot_set_32c(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                _, _, rfq_id, _ = await _tenant_client_rfq(
                    idc, w, u, ref=f"Q-SHOT-{u}", vessel="MT Sea Falcon")

                # commercial defaults give the document its letterhead data
                await w.post(
                    "/org/commercial",
                    data={"quote_prefix": "PMS",
                          "terms": ["Our offer is valid for 30 days from the "
                                    "date of this quotation.",
                                    "Payment is due within 07 days of "
                                    "invoice date."],
                          "bank_name": "Habib Bank", "branch": "Karachi",
                          "account_name": "Pak Marine Surveys",
                          "account_number": "PK36HBOK0001",
                          "swift": "HBPKPKKA"},
                    follow_redirects=False,
                )

                # 1. the raise form (Save bar forced visible)
                form = await w.get(f"/org/quotes/raise?rfq_id={rfq_id}")
                assert form.status_code == 200
                assert "quoteRfqPicker" in form.text or \
                    "Dar Shipping" in form.text
                _shot(form.text, tmp_path, "raise-form", 1280, 1750,
                      extra=FORCE_SAVE_BAR, prefix="32c")

                # 6. the raise form at 390px (true-width iframe capture)
                _shot_mobile(form.text, tmp_path, "mobile-form", 390, 1500,
                             extra=FORCE_SAVE_BAR, prefix="32c")

                # 2. the draft document preview
                quote_id = await _raise_draft(w, rfq_id)
                detail = await w.get(f"/org/quotes/{quote_id}")
                assert detail.status_code == 200
                assert 'class="sv-doc"' in detail.text
                _shot(detail.text, tmp_path, "doc-preview", 1280, 1750,
                      prefix="32c")

                # 3. the RFQ detail Quotes section (draft row + raise CTA)
                rfq_page = await w.get(f"/org/rfqs/{rfq_id}")
                assert "Pricing revisions for this RFQ." in rfq_page.text
                _shot(rfq_page.text, tmp_path, "rfq-detail-quotes", 1280, 1500,
                      prefix="32c")

                # 4. the issued document
                await _issue(w, quote_id)
                issued = await w.get(f"/org/quotes/{quote_id}?issued=1")
                assert ">Issued</span>" in issued.text
                _shot(issued.text, tmp_path, "issued", 1280, 1750,
                      prefix="32c")

                # 5. the quotes list, populated with the issued row
                listing = await w.get("/org/quotes")
                assert listing.status_code == 200
                assert ">Issued</span>" in listing.text
                _shot(listing.text, tmp_path, "quotes-list", 1280, 900,
                      prefix="32c")

    arun(scenario())
