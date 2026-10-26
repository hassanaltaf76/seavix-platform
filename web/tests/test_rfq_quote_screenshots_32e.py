"""32e: the owner-review screenshot set — eight shots covering the RFQ +
Quotation navigation, tables and filters:

1. the tenant rail showing both new sections and their submenus
2. the own-company live-profile preview with the non-actionable RFQ CTA
3. View RFQ — default-all populated table (every channel + status)
4. View RFQ — a filtered result with the Awaiting Quote Quote action
5. View Quotes — populated table with the filter bar
6. Generate Quote opened from an Awaiting Quote row (RFQ preselected)
7. View RFQ at 390px (deliberate horizontal containment)
8. View Quotes at 390px

Identity + work + web run in-process; every row is created through the
real pages. Proof lands in var/screenshots/32e-*.png; skips cleanly
without Chrome.
"""
import uuid

import httpx
import pytest

from test_page_shell_screenshots import CHROME, _shot
from test_profile_preview_ports import _web
from test_quote_screenshots_32c import _shot_mobile
from test_rfq_quotes_32e import _fixture


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome not installed")
def test_rfq_quote_screenshot_set_32e(arun, id_asgi, tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=id_asgi), base_url="http://id"
        ) as idc:
            u = uuid.uuid4().hex[:8]
            async with _web() as w:
                fx = await _fixture(idc, w, u)

                # 1. the rail: both sections + submenus
                listing = await w.get("/org/rfqs")
                assert listing.status_code == 200
                assert "Requests for Quotation" in listing.text
                assert "Quotations" in listing.text
                _shot(listing.text, tmp_path, "rail-sections", 1280, 1000,
                      prefix="32e")

                # 2. own-company live-profile preview: the CTA is disabled
                pub = await w.get(f"/org/{fx['slug']}")
                assert 'sv-rfq-cta" type="button" disabled' in pub.text
                _shot(pub.text, tmp_path, "own-profile-preview", 1280, 1400,
                      prefix="32e")

                # 3. View RFQ default-all, populated
                assert fx["ref_a"] in listing.text
                assert "Public page" in listing.text
                _shot(listing.text, tmp_path, "rfq-default-all", 1440, 1200,
                      prefix="32e")

                # 4. View RFQ filtered + the Awaiting Quote Quote action
                filtered = await w.get("/org/rfqs?status=awaiting_quote")
                assert f'/org/quotes/raise?rfq_id={fx["rfq_a"]}' \
                    in filtered.text
                _shot(filtered.text, tmp_path, "rfq-filtered-quote-action",
                      1440, 1100, prefix="32e")

                # 5. View Quotes populated + filter bar
                quotes = await w.get("/org/quotes")
                assert ">Issued</span>" in quotes.text
                assert "Port/location" in quotes.text
                _shot(quotes.text, tmp_path, "quotes-default-all", 1440,
                      1150, prefix="32e")

                # 6. Generate Quote from an Awaiting Quote row
                form = await w.get(
                    f'/org/quotes/raise?rfq_id={fx["rfq_a"]}')
                assert form.status_code == 200
                assert f'value="{fx["rfq_a"]}"' in form.text
                _shot(form.text, tmp_path, "quote-from-rfq-row", 1280, 1750,
                      prefix="32e")

                # 7+8. the two tables at a true 390px
                _shot_mobile(listing.text, tmp_path, "mobile-rfq-table", 390,
                             1400, prefix="32e")
                _shot_mobile(quotes.text, tmp_path, "mobile-quotes-table",
                             390, 1400, prefix="32e")

    arun(scenario())
