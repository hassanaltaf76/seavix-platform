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


def install(env: Any) -> None:
    env.filters["paragraphs"] = paragraphs
    env.filters["digits"] = digits
