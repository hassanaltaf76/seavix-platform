"""Design-language contract tests (blade shell + brand tokens).

Locks in the mandatory UI rules: Azure-portal blade shell markup in the
base layout, CSS custom-property tokens (no page-local styling), logo
top-left, locally vendored HTMX + Alpine, and WCAG AA contrast (>= 4.5:1)
for every text/background token pair used by the shell.
"""
import re
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
STYLE = (WEB_ROOT / "static" / "style.css").read_text()
BASE = (WEB_ROOT / "templates" / "base.html").read_text()
TEMPLATES = WEB_ROOT / "templates"


def _tokens() -> dict[str, str]:
    body = STYLE.split("}")[0]  # :root block
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(
            r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6}|var\(--[\w-]+\))", body
        )
    }


def _resolve(tokens: dict[str, str], name: str) -> str:
    value = tokens[name]
    for _ in range(4):
        ref = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if not ref:
            break
        value = tokens[ref.group(1)]
    assert value.startswith("#"), f"{name} does not resolve to a hex color"
    return value


def _lum(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    chan = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = chan(r), chan(g), chan(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    l1, l2 = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def test_tokens_sampled_from_logo_palette():
    tokens = _tokens()
    sampled = {"#001830", "#001848", "#183060", "#184878",
               "#306090", "#4878a8", "#2d5888", "#f0f0f0"}
    assert sampled & set(tokens.values()), "no logo-sampled hexes in tokens"


def test_wcag_aa_token_pairs():
    tokens = _tokens()
    pairs = {
        # (fg token, bg token): all shell text/background combos
        ("--sv-ink", "--sv-surface"),
        ("--sv-ink", "--sv-canvas"),
        ("--sv-muted", "--sv-surface"),
        ("--sv-link", "--sv-surface"),
        ("--sv-link", "--sv-canvas"),
        ("--sv-ink-on-dark", "--sv-chrome"),
        ("--sv-ink-on-dark", "--sv-chrome-raise"),
        ("--sv-ink-on-dark", "--sv-blade-head"),
        ("--sv-ink-on-dark", "--sv-navy-700"),
        ("#ffffff", "--sv-accent"),
        ("#ffffff", "--sv-navy-700"),
        ("#ffffff", "--sv-danger"),
        ("--sv-danger", "--sv-surface"),
    }
    for fg, bg in pairs:
        fg_hex = fg if fg.startswith("#") else _resolve(tokens, fg)
        bg_hex = bg if bg.startswith("#") else _resolve(tokens, bg)
        ratio = contrast(fg_hex, bg_hex)
        assert ratio >= 4.5, f"{fg} on {bg} = {ratio:.2f}:1 (AA needs 4.5)"


def test_blade_shell_base_layout():
    assert 'class="sv-chrome"' in BASE
    assert "/static/brand/seavix-logo.png" in BASE      # logo top-left
    assert BASE.index("sv-chrome-brand") < BASE.index("sv-chrome-nav")
    assert 'class="sv-crumbs"' in BASE and "Breadcrumb" in BASE
    assert 'class="sv-blade sv-blade--browse"' in BASE
    assert 'class="sv-blade sv-blade--slide"' in BASE
    assert 'id="blade"' in BASE                          # deep-link/skip target
    assert "/static/vendor/htmx.min.js" in BASE
    assert "/static/vendor/alpine.min.js" in BASE
    assert "sv-skip" in BASE                             # accessibility


def test_no_page_local_styling():
    """No inline style attributes or <style> blocks in any template;
    non-token colors must not appear outside the token definitions."""
    for tpl in TEMPLATES.glob("*.html"):
        text = tpl.read_text()
        assert 'style="' not in text, f"{tpl.name}: inline style attribute"
        assert "<style" not in text, f"{tpl.name}: page-local <style> block"


def test_workstream_pages_use_shell():
    """Login/registration/reset/account pages render inside the shell:
    breadcrumbs block + blade head from first render."""
    for name in ("login", "register", "verify_email", "forgot_password",
                 "reset_password", "org_home", "org_profile"):
        text = (TEMPLATES / f"{name}.html").read_text()
        assert "{% block breadcrumbs %}" in text, f"{name}: no breadcrumbs"
        assert "sv-blade-head" in text, f"{name}: no blade head"


def test_vendor_libs_present():
    vendor = WEB_ROOT / "static" / "vendor"
    assert (vendor / "htmx.min.js").stat().st_size > 20_000
    assert (vendor / "alpine.min.js").stat().st_size > 20_000


def test_favicon_and_pwa_icons_derived_from_logo():
    """favicon + PWA icon set derived from the logo's wave band, referenced
    from the base layout; manifest theme matches the chrome token."""
    import json

    brand = WEB_ROOT / "static" / "brand"
    for name in ("favicon-32.png", "favicon-64.png", "favicon.ico",
                 "apple-touch-icon.png", "icon-192.png", "icon-512.png"):
        assert (brand / name).stat().st_size > 500, f"{name} missing/empty"

    from PIL import Image
    for name, size in (("favicon-32.png", 32), ("icon-192.png", 192),
                       ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        with Image.open(brand / name) as img:
            assert img.size == (size, size), f"{name}: expected square {size}"

    assert "favicon-32.png" in BASE
    assert "apple-touch-icon.png" in BASE
    assert 'rel="manifest"' in BASE
    assert 'name="theme-color" content="#001830"' in BASE

    manifest = json.loads(
        (WEB_ROOT / "static" / "site.webmanifest").read_text()
    )
    assert manifest["theme_color"] == "#001830"
    assert manifest["background_color"] == "#001830"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
