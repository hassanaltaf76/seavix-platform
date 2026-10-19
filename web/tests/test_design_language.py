"""Design-language contract tests — Oct 2026 doc (dark-first blade shell).

Locks in: --seavix-* token family with logo-sampled values, WCAG AA contrast
on the DARK surfaces the shell actually uses, blade-shell markup (left rail
with favorites + tenant switcher, blade stack with crumbs/title/close/tabs,
global search), no page-local styling, and the favicon/PWA icon set.
"""
import json
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


def test_seavix_token_family_with_logo_sampled_values():
    tokens = _tokens()
    for name in ("--seavix-navy-900", "--seavix-navy-800", "--seavix-navy-700",
                 "--seavix-text-primary", "--seavix-text-secondary",
                 "--seavix-accent", "--seavix-accent-deep"):
        assert name in tokens, f"missing token {name}"
    sampled = {"#001830", "#183060", "#184878", "#50c0f0", "#4090e0"}
    assert sampled <= set(tokens.values()), "logo-sampled hexes missing"
    assert tokens["--seavix-text-primary"].lower() == "#ffffff"
    assert tokens["--seavix-text-secondary"].lower() == "#a8bcd8"


def test_wcag_aa_dark_shell_pairs():
    """Dark-first UI: every text/background pair the shell renders."""
    tokens = _tokens()
    pairs = {
        ("--seavix-text-primary", "--seavix-navy-900"),
        ("--seavix-text-primary", "--seavix-navy-800"),
        ("--seavix-text-primary", "--seavix-navy-700"),
        # doc-mandated check: secondary text on the card surface
        ("--seavix-text-secondary", "--seavix-navy-800"),
        ("--seavix-text-secondary", "--seavix-navy-900"),
        ("--seavix-accent", "--seavix-navy-900"),      # links on chrome
        ("--seavix-accent", "--seavix-navy-800"),      # links/tabs on blades
        ("--seavix-accent-deep", "--seavix-navy-900"), # pressed text on chrome
        ("--seavix-accent-bright", "--seavix-navy-800"), # link hover on blades
        ("--seavix-accent-bright", "--seavix-navy-900"),
        ("#04202e", "--seavix-accent"),                # primary button label
        ("#04202e", "--seavix-accent-deep"),       # primary button hover label
        ("--seavix-danger", "--seavix-navy-800"),
        ("--seavix-danger", "--seavix-navy-750"),
        ("--seavix-success", "--seavix-navy-800"),
    }
    for fg, bg in pairs:
        fg_hex = fg if fg.startswith("#") else _resolve(tokens, fg)
        bg_hex = bg if bg.startswith("#") else _resolve(tokens, bg)
        ratio = contrast(fg_hex, bg_hex)
        assert ratio >= 4.5, f"{fg} on {bg} = {ratio:.2f}:1 (AA needs 4.5)"


def test_blade_shell_markup():
    assert 'class="sv-topbar"' in BASE
    assert 'role="search"' in BASE and 'action="/search"' in BASE
    assert 'class="sv-rail"' in BASE
    assert "Favorites" in BASE and "All services" in BASE
    assert "sv-rail-tenant" in BASE
    assert "/static/brand/seavix-logo.png" in BASE      # logo top-left
    assert BASE.index("sv-rail-logo") < BASE.index("sv-rail-tenant")
    assert 'class="sv-blades"' in BASE
    assert "sv-blade-crumbs" in BASE
    assert "sv-blade-titlebar" in BASE
    assert "{% block blade_tabs %}" in BASE
    assert "{% block blade_close %}" in BASE
    assert "/static/vendor/htmx.min.js" in BASE
    assert "/static/vendor/alpine.min.js" in BASE
    assert "sv-skip" in BASE
    assert 'x-data' in BASE and "railCollapsed" in BASE  # Alpine rail toggle


def test_no_page_local_styling():
    for tpl in TEMPLATES.glob("*.html"):
        text = tpl.read_text()
        assert 'style="' not in text, f"{tpl.name}: inline style attribute"
        assert "<style" not in text, f"{tpl.name}: page-local <style> block"


def test_workstream_pages_are_blades():
    """Login/registration/reset/account pages: blade crumbs + title + close
    from first render, deep-linkable URLs."""
    for name in ("login", "register", "verify_email", "forgot_password",
                 "reset_password", "org_home", "org_profile"):
        text = (TEMPLATES / f"{name}.html").read_text()
        assert "{% block breadcrumbs %}" in text, f"{name}: no breadcrumbs"
        assert "{% block blade_title %}" in text, f"{name}: no blade title"
        assert "{% block blade_close %}" in text, f"{name}: no blade close"


def test_favicon_and_pwa_icons_derived_from_logo():
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
    manifest = json.loads((WEB_ROOT / "static" / "site.webmanifest").read_text())
    assert manifest["theme_color"] == "#001830"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
