"""Jinja filters shared by every web renderer (31h).

`paragraphs` turns plain user text into block markup: the input is
HTML-escaped FIRST (user text is never raw HTML — XSS), split on blank
lines into <p> blocks, with single newlines becoming <br>. Renderers opt
in by registering the filter on their Jinja environment via `install`.
"""
from __future__ import annotations

import html
import re
from typing import Any

from markupsafe import Markup

_BLANK_LINE = re.compile(r"\n[ \t]*\n+")


def paragraphs(text: Any) -> Markup:
    """Escape, then blank lines delimit <p> blocks; lone newlines -> <br>.

    Returns Markup because the escaped-then-structured output is complete
    HTML: Jinja's autoescape must not re-escape the <p>/<br> tags (the
    INPUT escaping already happened here, first)."""
    if text is None:
        return Markup("")
    escaped = html.escape(str(text), quote=True)
    blocks = [b.strip() for b in _BLANK_LINE.split(escaped) if b.strip()]
    out = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        out.append("<p>" + "<br>\n".join(lines) + "</p>")
    return Markup("\n".join(out))


def digits(text: Any) -> str:
    """31m: strip everything that is not a digit — the wa.me href carries
    the number in pure-digit international form while the visible text
    keeps the user's original formatting."""
    return re.sub(r"\D", "", str(text or ""))


def sv_money(value: Any) -> str:
    """32c: a money amount (string/number/None from a service response)
    as thousands-separated 2dp ("1,950.00"); None/empty -> "0.00"."""
    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, ValueError):
        amount = Decimal(0)
    return f"{amount.quantize(Decimal('0.01')):,}"


def sv_date(value: Any) -> str:
    """32e: an ISO timestamp/date string (a service response's created_at
    or quote_date) rendered as YYYY-MM-DD; empty input -> an em dash."""
    raw = str(value or "").strip()
    return raw[:10] if raw else "—"


def strip_trunk(text: Any) -> str:
    """31u: national digits with ONE leading trunk 0 dropped (E.164 trunk
    prefix). Only the cc-set branch of the tel:/wa.me hrefs applies this —
    cc-less numbers keep their leading zero untouched."""
    d = digits(text)
    return d[1:] if d.startswith("0") else d


def flag(iso2: Any) -> str:
    """31u: ISO alpha-2 -> regional-indicator emoji (AE -> 🇦🇪) — pure chr
    math off the two letters, no image assets, no flag CDN. Empty or
    non-alpha-2 input -> ""."""
    code = str(iso2 or "").upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def tojson_u(value: Any) -> Markup:
    """31u: Jinja's tojson with literal UTF-8 output (flags and em dashes
    stay readable in the served HTML). htmlsafe_json_dumps still escapes
    <>&' to \\uXXXX, so the result is safe inside single-quoted x-data
    attributes; tojson keeps its default ensure_ascii=True behavior."""
    from jinja2.utils import htmlsafe_json_dumps

    return htmlsafe_json_dumps(value, ensure_ascii=False)


def address_lines(org: Any) -> list[str]:
    """31t: the structured public address as display lines (empties
    skipped): line1 / line2 / "city, state postal" / COUNTRY NAME. An
    empty list means no structured field is set — the legacy free-text
    address renders instead (back-compat)."""
    org = org or {}
    lines: list[str] = []
    if org.get("address_line1"):
        lines.append(org["address_line1"])
    if org.get("address_line2"):
        lines.append(org["address_line2"])
    city, state, postal = org.get("city"), org.get("state"), org.get("postal_code")
    cityline = ""
    if city:
        cityline = city
        if state:
            cityline += ", " + state
        if postal:
            cityline += " " + postal
    elif state:
        cityline = state + (" " + postal if postal else "")
    elif postal:
        cityline = postal
    if cityline:
        lines.append(cityline)
    if org.get("country_code"):
        from .countries import country_name
        name = country_name(org["country_code"])
        if name:
            lines.append(name.upper())
    return lines


def install(env: Any) -> None:
    env.filters["paragraphs"] = paragraphs
    env.filters["digits"] = digits
    env.filters["strip_trunk"] = strip_trunk
    # 31u: like tojson but literal UTF-8 — flags/em dashes stay readable
    # in the served HTML. The <>&' html-safe escaping is retained, so
    # single-quoted x-data attributes stay safe. tojson itself keeps
    # its default ensure_ascii=True contract (31r pins the escaped
    # form inside x-data).
    env.filters["tojson_u"] = tojson_u
    # 32c: thousands-separated 2dp money rendering (quote totals, amounts)
    env.filters["sv_money"] = sv_money
    # 32e: list tables render service timestamps as dates
    env.filters["sv_date"] = sv_date
    # 31t: the shared public partial resolves the ISO alpha-2 country
    # code to its display name at render time (vendored countries.py),
    # and builds the structured address lines (empties skipped)
    from .countries import country_name
    env.globals["country_name"] = country_name
    env.globals["address_lines"] = address_lines
    # 31u: regional-indicator flag emoji for an ISO alpha-2 code — the
    # country/dial-code comboboxes render it server-side into the
    # options and the pre-selected label.
    env.globals["flag"] = flag
