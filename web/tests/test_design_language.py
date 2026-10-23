"""Design-language contract tests — DESIGN V4 (23 Oct 2026, supersedes v3).

The visual authority is the owner's design-spec-v4: Inter self-hosted
(OFL, no CDN), canvas #F7F8FA with white surfaces, ink #101828, ONE
accent #1570EF (interactive states only), navy #0B2447 (buttons/brand
only), elevation e1/e2/e3, radius 6/8/12, 150ms motion, focus rings on
every interactive. This file locks: the vendored font + @font-face, the
§2 token values, WCAG AA >= 4.5:1 on the new pairs, focus rings, the rail
active state, the hero markup, the /org/<slug> routing, and the
behavioral contracts carried forward (three layouts, register form, OTP
screen, no search markup, page navigation, permission gating, 31h/31l
shared partial, zero verification state).
"""
import re
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
STYLE = (WEB_ROOT / "static" / "style.css").read_text()
BASE = (WEB_ROOT / "templates" / "base.html").read_text()
BASE_PUBLIC = (WEB_ROOT / "templates" / "base_public.html").read_text()
BASE_AUTH = (WEB_ROOT / "templates" / "base_auth.html").read_text()
TEMPLATES = WEB_ROOT / "templates"
FONTS = WEB_ROOT / "static" / "fonts"

AUTH_TEMPLATES = ("login", "register", "forgot_password", "reset_password",
                  "login_otp", "verify_email", "verify_email_confirm",
                  "verify_email_done", "verify_gate")
PUBLIC_TEMPLATES = ("landing", "profile", "profile_unavailable",
                    "rfq_form", "rfq_thanks")
PAGE_TEMPLATES = ("org_home", "org_profile", "org_not_found", "dashboard")


def _tokens() -> dict[str, str]:
    body = re.search(r":root\s*\{(.*?)\}", STYLE, re.S).group(1)
    return {
        m.group(1): m.group(2).strip().rstrip(";")
        for m in re.finditer(
            r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6}|rgba?\([^)]*\)|var\(--[\w-]+\))",
            body,
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


# ------------------------------------------------------------- §1 TYPE
def test_inter_is_vendored_and_applied():
    """No CDN: the four woff2 files ship in static/fonts, @font-face
    declares them, and the sans stack + body actually use them."""
    for weight in ("400", "500", "600", "700"):
        f = FONTS / f"inter-latin-{weight}-normal.woff2"
        assert f.is_file(), f"missing vendored font {f.name}"
        assert f.read_bytes()[:4] == b"wOF2", f"{f.name}: not a woff2"
        assert f'font-weight: {weight}' in STYLE
        assert f"/static/fonts/inter-latin-{weight}-normal.woff2" in STYLE
    assert STYLE.count("@font-face") >= 4
    assert 'font-display: swap' in STYLE
    assert '"Inter", -apple-system' in STYLE
    assert "font-family: var(--font-sans)" in STYLE
    # mono stack for numeric data (IMO, codes, the OTP input)
    assert "--font-mono" in STYLE
    assert "var(--font-mono)" in STYLE


def test_type_scale_matches_spec():
    for snippet in (
        "font-size: 44px; line-height: 52px",   # Display
        "font-size: 24px; line-height: 32px",   # H1
        "font-size: 17px; line-height: 24px",   # H2
        "font-size: 14px; line-height: 22px",   # body
        "font-size: 13px; line-height: 18px",   # form labels
        "font-size: 12px; line-height: 16px",   # captions
    ):
        assert snippet in STYLE, snippet
    assert "letter-spacing: -0.02em" in STYLE  # Display
    assert "letter-spacing: -0.01em" in STYLE  # H1/H2
    assert "font-variant-numeric: tabular-nums" in STYLE


# ------------------------------------------------------ §2/§3 TOKENS
def test_v4_token_values():
    tokens = _tokens()
    expected = {
        "--bg": "#F7F8FA",
        "--surface": "#FFFFFF",
        "--border": "#E4E7EC",
        "--border-strong": "#D0D5DD",
        "--ink": "#101828",
        "--text": "#344054",
        "--text-2": "#667085",
        "--accent": "#1570EF",
        "--accent-hover": "#0E5FD0",
        "--navy": "#0B2447",
        "--navy-hover": "#10315F",
        "--on-navy": "#FFFFFF",
        "--success": "#12B76A",
        "--success-bg": "#ECFDF3",
        "--warning": "#F79009",
        "--danger": "#F04438",
        "--danger-bg": "#FEF3F2",
        "--info": "#2E90FA",
    }
    for name, hex_value in expected.items():
        assert name in tokens, f"missing token {name}"
        assert _resolve(tokens, name) == hex_value, (
            f"{name}: expected {hex_value}, got {tokens[name]}"
        )
    # the old v3 family is gone — one source of truth
    for stale in ("--seavix-navy-900", "--seavix-accent", "--seavix-primary"):
        assert stale not in tokens, f"stale v3 token still present: {stale}"


def test_depth_radius_motion_per_spec():
    for snippet in (
        "0 1px 2px rgba(16, 24, 40, .06)",    # e1
        "0 4px 8px rgba(16, 24, 40, .08)",    # e2
        "0 12px 24px rgba(16, 24, 40, .14)",  # e3
        "--r-input: 6px", "--r-btn: 8px", "--r-panel: 12px",
        "--dur: 150ms",
        "cubic-bezier(.2, 0, .2, 1)",
        "translateY(-1px)",                   # hover lift
        "prefers-reduced-motion",
    ):
        assert snippet in STYLE, snippet


def test_focus_rings_on_every_interactive():
    """§3: 2px ring accent at 30% alpha + 1px border accent."""
    assert "var(--ring)" in STYLE
    ring = re.search(r"--ring:\s*(.*?);", STYLE).group(1)
    assert "rgba(21, 112, 239, .30)" in ring
    assert "var(--accent)" in ring
    assert re.search(r":focus-visible\s*\{[^}]*box-shadow:\s*var\(--ring\)", STYLE, re.S)
    # inputs get border-accent + ring on focus
    inputs = re.search(
        r"\.sv-field input:focus[^{]*\{([^}]*)\}", STYLE, re.S)
    assert inputs and "border-color: var(--accent)" in inputs.group(1)


def test_wcag_aa_v4_pairs():
    """AA >= 4.5:1 re-locked on the v4 values (incl. the derived -ink
    text colors the spec's AA rule requires on its tints)."""
    tokens = _tokens()
    pairs = {
        ("--ink", "--bg"),
        ("--ink", "--surface"),
        ("--text", "--surface"),
        ("--text", "--bg"),
        ("--text-2", "--surface"),
        ("--text-2", "--bg"),
        ("--accent-dark", "--surface"),        # links / active nav text
        ("--accent-dark", "--accent-tint"),    # active nav pill
        ("--on-navy", "--navy"),               # primary button
        ("--on-navy", "--navy-hover"),
        ("--on-navy", "--accent"),             # white on accent (4.53:1)
        ("--danger-ink", "--surface"),
        ("--danger-ink", "--danger-bg"),
        ("--success-ink", "--surface"),
        ("--success-ink", "--success-bg"),
        ("--info-ink", "--info-bg"),
        ("--accent", "--bg"),                  # focus ring (UI 3:1 floor)
    }
    for fg, bg in pairs:
        fg_hex = _resolve(tokens, fg)
        bg_value = tokens[bg]
        if bg_value.startswith("rgba"):
            # blend the accent tint over its canvas (white surface)
            m = re.fullmatch(
                r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", bg_value)
            r, g, b, a = (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          float(m.group(4)))
            bg_hex = "#%02X%02X%02X" % tuple(
                round(a * c + (1 - a) * 255) for c in (r, g, b))
        else:
            bg_hex = _resolve(tokens, bg)
        floor = 3.0 if fg == "--accent" else 4.5
        ratio = contrast(fg_hex, bg_hex)
        assert ratio >= floor, f"{fg} on {bg} = {ratio:.2f}:1 (needs {floor})"


# --------------------------------------------------------- §5 NAV
def test_rail_structure_and_active_state():
    """v4 §5: logo → company block → WORKSPACE section → COMPANY section;
    active = accent left-bar 3px + accent-8% pill, exactly one per view
    (the page template marks the item via `active`)."""
    assert 'class="sv-rail"' in BASE
    assert ">Workspace</h2>" in BASE
    assert ">Company</h2>" in BASE
    assert 'class="sv-rail-item"' in BASE
    assert 'href="/dashboard"' in BASE
    assert 'href="/org/profile"' in BASE
    assert '{% if active == "home" %}aria-current="page"' in BASE
    assert '{% if active == "profile" %}aria-current="page"' in BASE
    active = re.search(r"\.sv-rail-item\[aria-current\]\s*\{(.*?)\}", STYLE, re.S)
    assert active and "inset 3px 0 0 var(--accent)" in active.group(1)
    assert "var(--accent-tint)" in active.group(1)
    # the rail is the home of primary navigation: future slots documented
    assert "sv-rail-badge" in STYLE
    # company cell: logo-or-initials + name, permission-driven, top placed
    assert "'org.profile.view' in ctx.memberships[0].permissions" in BASE
    assert "rail_org.logo_url" in BASE
    assert 'class="sv-rail-initials"' in BASE
    assert "rail_org.initials" in BASE
    assert "rail_org.name" in BASE
    block = re.search(r"\.sv-rail-tenant\s*\{(.*?)\}", STYLE, re.S)
    assert block and "margin-top" not in block.group(1), "never bottom-pinned"
    logo_at = BASE.index('class="sv-rail-logo"')
    tenant_at = BASE.index('class="sv-rail-tenant"')
    assert logo_at < tenant_at
    # exactly one active-state marker mechanism in the shell (rail-scoped:
    # two items, conditionally set; the page chrome carries no other)
    rail = BASE[BASE.index('class="sv-rail"'):BASE.index('class="sv-page"')]
    assert rail.count('aria-current="page"') == 2


def test_topbar_account_menu():
    """v4 §5: top-right account menu — initials avatar + name, dropdown
    Your account / Trusted devices / Sign out."""
    assert 'class="sv-account"' in BASE
    assert 'class="sv-avatar"' in BASE
    assert 'aria-haspopup="menu"' in BASE
    assert 'class="sv-dropdown"' in BASE
    assert 'href="/org/home"' in BASE
    assert 'href="/org/home#trusted-devices"' in BASE
    assert 'action="/logout"' in BASE
    # no plain topnav links remain
    assert "sv-topnav" not in BASE


# --------------------------------------------------------- §9 SHELL PATTERN
def test_page_shell_replaces_blades_31p():
    """§9 (owner 23 Oct): the Azure-style blade stack is RETIRED — zero
    sv-blade-* selectors remain in any template or the stylesheet; the
    shell is the fixed sidebar (§5, unchanged) + full page."""
    assert 'class="sv-page"' in BASE
    assert "sv-page-header" in BASE
    for tpl in TEMPLATES.glob("*.html"):
        text = tpl.read_text()
        assert "sv-blade" not in text, f"{tpl.name}: blade selector remains"
    assert "sv-blade" not in STYLE
    for stale in ("--blade-min", "--blade-max", "--r-blade"):
        assert stale not in STYLE, f"stale blade token: {stale}"


def test_page_header_contract_31p():
    """§9: page header = H1 + one-line 13px text-2 description + primary
    action top-right; the page caps at 1120px with a 24px gutter."""
    assert "{% block page_title %}" in BASE
    assert "{% block page_desc %}" in BASE
    assert 'class="sv-page-actions"' in BASE
    page = re.search(r"\.sv-page\s*\{(.*?)\}", STYLE, re.S)
    assert page and "var(--gutter)" in page.group(1)
    inner = re.search(r"\.sv-page-inner\s*\{(.*?)\}", STYLE, re.S)
    assert inner and "var(--page-max)" in inner.group(1)
    header = re.search(r"\.sv-page-header\s*\{(.*?)\}", STYLE, re.S)
    assert header and "space-between" in header.group(1)
    desc = re.search(r"\.sv-page-desc\s*\{(.*?)\}", STYLE, re.S)
    assert desc and "var(--text-2)" in desc.group(1) and "13px" in desc.group(1)
    assert "--page-max: 1120px" in STYLE
    assert "--gutter: 24px" in STYLE
    # the pages carry real headers: H1 + description + action where the
    # flow has a primary action
    dash = (TEMPLATES / "dashboard.html").read_text()
    assert "<h1>Dashboard</h1>" in dash
    assert 'class="sv-page-desc"' in dash
    profile = (TEMPLATES / "org_profile.html").read_text()
    assert "<h1>Your public page</h1>" in profile
    assert 'class="sv-page-desc"' in profile
    assert "{% block page_actions %}" in profile


def test_section_card_settings_pattern_31p():
    """§9: content as stacked section cards — 200px left label column +
    fields right at ≥1024px, stacked below (the Stripe settings pattern)."""
    assert "--section-label-w: 200px" in STYLE
    card = re.search(r"\.sv-section-card\s*\{(.*?)\}", STYLE, re.S)
    assert card and "var(--section-label-w)" in card.group(1)
    stacked = re.search(
        r"@media \(max-width: 1023px\)\s*\{(.*?)\}", STYLE, re.S)
    assert stacked and "grid-template-columns: 1fr" in stacked.group(1)
    for name in ("org_home", "org_profile"):
        text = (TEMPLATES / f"{name}.html").read_text()
        assert 'class="sv-section-card"' in text, f"{name}: section cards"
        assert "sv-section-head" in text, f"{name}: label column"
        assert "sv-section-body" in text, f"{name}: fields column"


def test_page_tabs_component_31p():
    """§9: underline page-tabs (entity detail: Overview/Quotes/Assignment/
    Survey/Report) replace stacked blades — the component ships now, ready
    for stage 4; the profile page is its first consumer."""
    tabs = re.search(r"\.sv-page-tabs\s*\{(.*?)\}", STYLE, re.S)
    assert tabs and "border-bottom: 1px solid var(--border)" in tabs.group(1)
    active = re.search(r"\.sv-page-tabs a\[aria-current\]\s*\{(.*?)\}", STYLE, re.S)
    assert active and "var(--accent)" in active.group(1)
    profile = (TEMPLATES / "org_profile.html").read_text()
    assert 'class="sv-page-tabs"' in profile
    assert ">Public page</a>" in profile
    assert ">Live page</a>" in profile


# --------------------------------------------------------- §6 PAGES
def test_hero_markup_contract():
    """v4 §6: the marketing page is a full-bleed navy hero band — wordmark,
    Display headline, sub, two CTAs, art blended right, then 3 feature
    cards; the chrome lives inside the band."""
    landing = (TEMPLATES / "landing.html").read_text()
    assert 'class="sv-hero-band"' in landing
    assert 'class="sv-hero-art"' in landing
    assert 'class="sv-display"' in landing
    assert "Marine surveys, coordinated." in landing
    assert 'class="sv-hero-sub"' in landing
    assert "primary-outline" in landing and "solid-white" in landing
    assert landing.count('class="sv-feature sv-hoverable"') == 3
    for title in ("For survey companies", "For ship operators", "How RFQs work"):
        assert title in landing
    # full-bleed: the landing suppresses the standalone public header
    assert '{% block public_header %}' in landing
    assert "sv-public-header" not in landing
    band = re.search(r"\.sv-hero-band\s*\{(.*?)\}", STYLE, re.S)
    assert band and "var(--navy)" in band.group(1)
    display = re.search(r"\.sv-display\s*\{(.*?)\}", STYLE, re.S)
    assert display and "44px" in display.group(1) and "#fff" in display.group(1)


def test_dashboard_skeleton_cards():
    dash = (TEMPLATES / "dashboard.html").read_text()
    assert dash.count("sv-skel-card") == 3
    for title in ("Jobs", "RFQs", "Activity"):
        assert f"<h2>{title}</h2>" in dash
    assert dash.count("Arriving in an upcoming release.") == 3


def test_public_profile_card_layout():
    """v4 §6: /org/<slug> renders the 720px card — header row, chips,
    prose, Contact card with icon rows, navy full-width RFQ CTA."""
    partial = (TEMPLATES / "_public_org_content.html").read_text()
    assert "sv-org-card" in partial
    card = re.search(r"\.sv-org-card\s*\{(.*?)\}", STYLE, re.S)
    assert card and "max-width: 720px" in card.group(1)
    assert "sv-contact-row" in partial
    assert partial.count("sv-contact-icon") >= 5  # address/phone/email/whatsapp/website
    assert "sv-rfq-cta" in partial
    cta = re.search(r"\.sv-rfq-cta\s*\{(.*?)\}", STYLE, re.S)
    assert cta and "width: 100%" in cta.group(1) and "var(--navy)" not in cta.group(1)
    assert 'href="/org/{{ org.slug }}/rfq"' in partial
    profile = (TEMPLATES / "profile.html").read_text()
    assert "sv-org-wrap" in profile


def test_logo_pinned_to_top_left_corner_31s():
    """31s (owner 23 Oct): the logo was vertically centered with the
    name/description block — brand marks belong in the card's TOP-LEFT
    corner (reading-pattern standard). Contract: the header carries the
    corner modifier; the logo (or the no-logo placeholder — same slot,
    unchanged behavior) is the header's FIRST child; the alignment is
    flex-start at BOTH the row level (align-items) and the logo itself
    (align-self), so nothing can re-center the mark."""
    partial = (TEMPLATES / "_public_org_content.html").read_text()
    header = re.search(
        r'<header class="([^"]*)">\s*(.*?)</header>', partial, re.S
    )
    assert header, "no <header> in the shared partial"
    classes, body = header.group(1), header.group(2)
    assert "sv-org-header--corner" in classes
    # logo (or the placeholder occupying the same corner slot) is the
    # first child — the name/description div follows it
    assert re.search(
        r'^\s*\{% if org\.logo_url %\}\s*'
        r'<img class="logo"', body
    ), "logo must be the header's first child"
    assert re.search(
        r'<div class="logo placeholder"[^>]*></div>', body
    ), "no-logo placeholder occupies the same corner slot"
    corner = re.search(r"\.sv-org-header--corner\s*\{(.*?)\}", STYLE, re.S)
    assert corner and "align-items: flex-start" in corner.group(1)
    logo_self = re.search(
        r"\.sv-org-header--corner \.logo\s*\{(.*?)\}", STYLE, re.S
    )
    assert logo_self and "align-self: flex-start" in logo_self.group(1)
    logo = re.search(r"\.sv-org-header \.logo\s*\{(.*?)\}", STYLE, re.S)
    assert logo and "64px" in logo.group(1)


def test_inline_svg_icons_constrained_31o():
    """31o (owner screenshot 23 Oct): viewBox-only inline SVGs rendered at
    their default viewport size — huge icons on the profile preview and
    public contact rows. Fix contract: every decorative inline SVG carries
    explicit width/height attributes (§4 sizes: 16 contact rows, 20
    callout, 40 empty states, 26 feature cards) AND the stylesheet pins
    the same sizes per context with flex:none, so a missing attribute can
    never inflate an icon again."""
    for selector, size in (
        (r"\.sv-contact-icon svg", "16px"),   # public contact rows (§4)
        (r"\.sv-info-callout svg", "20px"),   # profile editor callout
        (r"\.sv-empty svg", "40px"),          # empty states (§4)
        (r"\.sv-skel-card svg", "40px"),      # dashboard awaiting-data cards
        (r"\.sv-feature svg", "26px"),        # hero feature cards
        (r"\.sv-rail-item \.sv-glyph svg", "16px"),  # rail
    ):
        rule = re.search(selector + r"\s*\{(.*?)\}", STYLE, re.S)
        assert rule, f"missing CSS rule {selector}"
        body = rule.group(1)
        assert f"width: {size}" in body and f"height: {size}" in body, selector
        assert "flex: none" in body or "flex: 0 0" in body, selector
    # the defensive utility itself (31o): .sv-ic base + size variants, each
    # width/height-pinned with flex:none so a missing attribute on the tag
    # can never inflate the icon
    for cls, size in (("sv-ic", "16px"), ("sv-ic-20", "20px"),
                      ("sv-ic-26", "26px"), ("sv-ic-40", "40px")):
        rule = re.search(rf"\.{cls}\s*\{{(.*?)\}}", STYLE, re.S)
        assert rule, f"missing .{cls} utility rule"
        assert f"width: {size}" in rule.group(1), cls
        assert f"height: {size}" in rule.group(1), cls
        assert "flex: none" in rule.group(1), cls
    # audit ALL templates: every inline svg carries the utility class AND
    # explicit dimensions
    for tpl in sorted(TEMPLATES.glob("*.html")):
        tags = re.findall(r"<svg\b[^>]*>", tpl.read_text())
        for tag in tags:
            assert 'class="sv-ic' in tag, \
                f"{tpl.name}: svg missing sv-ic utility class: {tag[:90]}"
            assert 'width="' in tag and 'height="' in tag, \
                f"{tpl.name}: unconstrained inline svg: {tag[:90]}"


def test_profile_editor_save_bar_and_callout():
    """v4 §6: soft info callout for the address state; the sticky save
    bar appears when dirty (Cancel left, Save right)."""
    text = (TEMPLATES / "org_profile.html").read_text()
    assert "sv-info-callout" in text
    assert "sv-slug-callout" in text
    callout = re.search(r"\.sv-info-callout\s*\{(.*?)\}", STYLE, re.S)
    assert callout and "var(--accent-tint)" in callout.group(1)
    bar = re.search(r"\.sv-save-bar\s*\{(.*?)\}", STYLE, re.S)
    assert bar and "position: sticky" in bar.group(1) and "bottom: 0" in bar.group(1)
    assert 'class="sv-save-bar"' in text
    assert 'x-show="dirty"' in text
    assert ">Cancel</a>" in text
    assert ">Save public page</button>" in text
    # v4 §4: Save disabled until dirty AND valid — the form re-checks
    # constraint validity on every input (bubbling @input on the form)
    assert ':disabled="!(dirty && valid)"' in text
    assert "dirty: false" in text
    assert "valid: true" in text
    assert '@input="valid = $el.checkValidity()"' in text


# --------------------------------------------------------- §7 URLS
def test_org_slug_routing_contract():
    """v4 §7: canonical /org/<slug>; root /<slug> and the /p chain
    301-redirect; callout text says seavix.ai/org/<slug>; RESERVED words
    include `org`."""
    pages = (Path(__file__).resolve().parents[1] / "webapp" / "pages.py").read_text()
    assert '@profiles_router.get("/org/{slug}"' in pages
    assert '@profiles_router.get("/org/{slug}/rfq"' in pages
    assert 'RedirectResponse(f"/org/{slug}", status_code=301)' in pages
    assert 'RedirectResponse(f"/org/{slug}/rfq", status_code=301)' in pages
    main = (Path(__file__).resolve().parents[1] / "webapp" / "main.py").read_text()
    assert (main.index("app.include_router(org_pages_router)")
            < main.index("app.include_router(pages_profiles_router)")
            < main.index("app.include_router(pages_root_router)"))
    profile_tpl = (TEMPLATES / "org_profile.html").read_text()
    assert "seavix.ai/org/{{ org.slug }}" in profile_tpl
    assert "seavix.ai/{{ org.slug }}" not in profile_tpl.replace(
        "seavix.ai/org/{{ org.slug }}", "")
    identity_profile = (
        Path(__file__).resolve().parents[2]
        / "services" / "identity" / "app" / "profile.py"
    ).read_text()
    assert '"org"' in identity_profile.split("RESERVED_SLUGS")[1].split("}")[0]


# ------------------------------------------------ carried-forward contracts
def test_page_shell_is_post_auth_only():
    assert 'class="sv-topbar"' in BASE
    assert 'class="sv-page"' in BASE
    assert "sv-page-header" in BASE
    assert "{% block page_tabs %}" in BASE
    assert "/static/vendor/htmx.min.js" in BASE
    assert "/static/vendor/alpine.min.js" in BASE
    assert "sv-skip" in BASE
    assert "railCollapsed" in BASE
    # no global search anywhere (returns with the ops console)
    assert 'action="/search"' not in BASE
    assert "sv-search" not in BASE
    for tpl in TEMPLATES.glob("*.html"):
        text = tpl.read_text()
        assert 'action="/search"' not in text, f"{tpl.name}: search markup"
        assert "sv-search" not in text, f"{tpl.name}: search markup"
        assert "All services" not in text, f"{tpl.name}: services section"


def test_page_never_drops_to_public_landing():
    """§9: no ×, no crumb trail — and a page never links the public
    landing; the rail logo roots at the dashboard."""
    assert 'href="/"' not in BASE
    assert 'href="/dashboard"' in BASE  # rail logo → dashboard
    dash = (TEMPLATES / "dashboard.html").read_text()
    home = (TEMPLATES / "org_home.html").read_text()
    profile = (TEMPLATES / "org_profile.html").read_text()
    for name, text in (("dashboard", dash), ("org_home", home),
                       ("org_profile", profile)):
        assert 'href="/"' not in text, f"{name}: links the public landing"
        assert "sv-blade" not in text, f"{name}: blade chrome remains"


def test_no_text_wordmark_next_to_logo():
    """The logo image carries the wordmark in the three chrome layouts.
    (The v4 hero wordmark is the hero's own brand line, not a logo
    container — §6 lists it explicitly.)"""
    containers = [
        re.search(r'<div class="sv-rail-logo">(.*?)</div>', BASE, re.S),
        re.search(r'<a class="sv-public-brand"[^>]*>(.*?)</a>', BASE_PUBLIC, re.S),
        re.search(r'<a class="sv-auth-logo"[^>]*>(.*?)</a>', BASE_AUTH, re.S),
    ]
    assert all(containers), "logo container not found in a base layout"
    for match in containers:
        inner = re.sub(r"<img[^>]*>", "", match.group(1))
        text_only = re.sub(r"<[^>]+>", "", inner).strip()
        assert "SeaVix" not in text_only, f"text wordmark next to logo: {text_only!r}"


def test_public_layout_chrome():
    assert 'class="sv-public"' in BASE_PUBLIC
    assert 'class="sv-public-header"' in BASE_PUBLIC
    assert 'class="sv-public-brand"' in BASE_PUBLIC
    assert "/static/brand/seavix-logo.png" in BASE_PUBLIC
    assert "/login" in BASE_PUBLIC and "/register" in BASE_PUBLIC
    assert "sv-rail" not in BASE_PUBLIC
    assert "sv-blades" not in BASE_PUBLIC
    assert "sv-search" not in BASE_PUBLIC
    assert "sv-rail-tenant" not in BASE_PUBLIC
    assert "{% block public_header %}" in BASE_PUBLIC


def test_auth_layout_centered_card():
    assert 'class="sv-auth"' in BASE_AUTH
    assert 'class="sv-auth-logo"' in BASE_AUTH
    assert 'class="sv-auth-card"' in BASE_AUTH
    assert "max-width: 400px" in STYLE
    assert "seavix-logo.png" in BASE_AUTH
    assert "sv-rail" not in BASE_AUTH
    assert "sv-blades" not in BASE_AUTH
    assert "sv-public-header" not in BASE_AUTH


def test_template_lineage():
    for name in AUTH_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        assert '{% extends "base_auth.html" %}' in text, f"{name} must extend base_auth"
    for name in PUBLIC_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        assert '{% extends "base_public.html" %}' in text, f"{name} must extend base_public"
    for name in PAGE_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        assert '{% extends "base.html" %}' in text, f"{name} must extend the page shell"
    for name in AUTH_TEMPLATES + PUBLIC_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        assert "sv-rail" not in text, f"{name}: rail markup on a public/auth page"
        assert "sv-blades" not in text, f"{name}: blade markup on a public/auth page"


def test_register_form_contract():
    text = (TEMPLATES / "register.html").read_text()
    select = re.search(r"<select[^>]*name=\"org_type\"[^>]*>(.*?)</select>", text, re.S)
    assert select, "org_type must be a <select>"
    for value, label in (("agent", "Port Agent"),
                         ("management", "Management Company"),
                         ("survey_company", "Surveyor")):
        assert f'value="{value}"' in select.group(1), f"missing option {value}"
        assert label in select.group(1), f"missing label {label}"
    assert 'name="password"' in text and 'name="confirm_password"' in text
    assert 'name="slug"' not in text
    assert re.search(r"<input[^>]*name=\"org_name\"", text)


def test_no_page_local_styling():
    for tpl in TEMPLATES.glob("*.html"):
        text = tpl.read_text()
        assert 'style="' not in text, f"{tpl.name}: inline style attribute"
        assert "<style" not in text, f"{tpl.name}: page-local <style> block"


def test_otp_screen_contract():
    text = (TEMPLATES / "login_otp.html").read_text()
    assert "Check your inbox" in text
    assert 'inputmode="numeric"' in text
    assert 'autocomplete="one-time-code"' in text
    assert 'maxlength="6"' in text
    assert 'method="post" action="/login/otp/resend"' in text
    assert "Back to sign in" in text
    assert text.count('name="code"') == 1
    # v4 §6: one 48px mono input with .35em tracking (no 6-box widget)
    assert "sv-code-input" in text
    code = re.search(r"\.sv-field input\.sv-code-input\s*\{(.*?)\}", STYLE, re.S)
    assert code and "min-height: 48px" in code.group(1)
    assert "var(--font-mono)" in code.group(1)
    assert "letter-spacing: 0.35em" in code.group(1)
    for banned in ("identity service log", "local dev", "DEV-", "MAIL to="):
        assert banned not in text, f"dev reference in OTP copy: {banned}"


def test_auth_templates_carry_no_dev_references():
    for name in AUTH_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        for banned in ("identity service log", "In local dev", "DEV-", "MAIL to="):
            assert banned not in text, f"{name}: dev reference {banned!r}"


def test_forgot_password_is_email_only():
    text = (TEMPLATES / "forgot_password.html").read_text()
    assert 'name="email"' in text
    assert 'type="password"' not in text
    assert 'name="confirm_password"' not in text


def test_verify_templates_have_no_password_fields():
    for name in ("verify_email", "verify_email_confirm", "verify_email_done"):
        text = (TEMPLATES / f"{name}.html").read_text()
        assert 'type="password"' not in text, f"{name}: password input on email action"


def test_profile_settings_public_slug_section():
    """The profile blade is the public-page editor — /org/<slug> framing,
    services description, slug + hint, logo upload, 31j address-state
    callout, 31g dirty-gated save."""
    text = (TEMPLATES / "org_profile.html").read_text()
    assert 'name="slug"' in text
    assert 'name="about"' in text
    assert "seavix.ai/org/" in text
    assert "seavix.ai/p/" not in text
    assert "Your public page" in text
    assert '{% if org and org.profile_status == ' in text
    assert ">Live page</a>" in text
    assert 'href="/org/profile?view=edit"' in text
    assert "Describe your services" in text
    assert 'action="/org/profile/logo"' in text
    assert 'name="file"' in text
    assert "Choose your public address: seavix.ai/org/{{ org.slug }}" in text
    assert "Your public page: seavix.ai/org/{{ org.slug }}" in text
    assert "slug_is_placeholder" in text
    assert "not-found page" in text
    assert '@input="dirty = true"' in text
    assert "this.dirty = true" in text  # port add()/remove()
    assert ':disabled="!(dirty && valid)"' in text


def test_profile_blade_is_permission_gated_31e():
    text = (TEMPLATES / "org_profile.html").read_text()
    assert "'org.logo.upload' in membership.permissions" in text
    assert 'mode == "view" and can_edit' in text
    home = (TEMPLATES / "org_home.html").read_text()
    assert "'org.profile.view' in ctx.memberships[0].permissions" in BASE
    assert 'href="/org/profile"' in BASE
    routes = Path(__file__).resolve().parents[1] / "webapp" / "org_pages.py"
    routes_text = routes.read_text()
    assert routes_text.count("_rail_org(") >= 6


def test_view_mode_is_public_preview_31f():
    partial = (TEMPLATES / "_public_org_content.html").read_text()
    profile = (TEMPLATES / "profile.html").read_text()
    blade = (TEMPLATES / "org_profile.html").read_text()
    assert '{% include "_public_org_content.html" %}' in profile
    assert '{% include "_public_org_content.html" %}' in blade
    assert "sv-org-header" not in profile
    assert "sv-org-header" not in blade
    assert "Ports we serve" in partial
    assert "org.ports" in partial
    # 31q: the two new chip sections ride the same partial
    assert "org.services" in partial
    assert "org.vessel_types" in partial
    assert "Vessel types we serve" in partial
    assert 'class="card sv-preview"' in blade
    assert '{% if mode == "view" %}' in blade
    # 31q: edit mode renders the partial ZERO times — the preview aside
    # is retired; view mode owns the only include
    assert blade.count('{% include "_public_org_content.html" %}') == 1


def test_edit_mode_full_width_no_preview_aside_31q():
    """31q §6 amendment: edit mode = full-width form, no preview aside;
    preview is for review after editing (view mode, unchanged)."""
    blade = (TEMPLATES / "org_profile.html").read_text()
    style = STYLE
    assert "sv-editor-preview" not in blade
    assert "sv-editor-layout" not in blade
    assert ".sv-editor-layout" not in style
    assert ".sv-editor-preview" not in style
    # the three chip pickers share the generalized picker component
    assert blade.count('class="sv-chip-picker"') == 3
    assert 'id="service-search"' in blade
    assert 'id="vessel-type-search"' in blade
    assert 'id="port-search"' in blade
    assert ".sv-chip-picker" in style
    assert ".sv-picker-results" in style


def test_description_renders_as_blocks_31h():
    partial = (TEMPLATES / "_public_org_content.html").read_text()
    assert "org.about | paragraphs" in partial
    assert 'class="sv-prose"' in partial
    filters = (Path(__file__).resolve().parents[1]
               / "webapp" / "filters.py").read_text()
    assert "html.escape" in filters
    assert "Markup" in filters
    assert ".sv-prose p { margin: 0 0 0.75rem; }" in STYLE


def test_ugc_overflow_wrap_31l():
    """31l holds under v4: every UGC render point keeps the wrap rule."""
    assert "overflow-wrap: anywhere;" in STYLE
    assert "word-break: break-word;" in STYLE
    ugc = re.search(r"\.sv-ugc,\s*\.sv-prose\s*\{(.*?)\}", STYLE, re.S)
    assert ugc and "overflow-wrap: anywhere;" in ugc.group(1)
    partial = (TEMPLATES / "_public_org_content.html").read_text()
    assert '<article class="sv-ugc' in partial
    tenant = re.search(r"\.sv-rail-tenant a\s*\{(.*?)\}", STYLE, re.S)
    assert tenant and "overflow-wrap: anywhere;" in tenant.group(1)
    home = (TEMPLATES / "org_home.html").read_text()
    assert 'class="sv-ugc">Signed in as' in home
    rfq = (TEMPLATES / "rfq_form.html").read_text()
    assert '<span class="sv-ugc">{{ org_name }}</span>' in rfq
    unavailable = (TEMPLATES / "profile_unavailable.html").read_text()
    assert 'class="lede sv-ugc"' in unavailable
    for tpl in ("login_otp", "forgot_password", "verify_email",
                "verify_gate", "verify_email_confirm"):
        text = (TEMPLATES / f"{tpl}.html").read_text()
        assert 'class="sv-auth-sub sv-ugc"' in text, f"{tpl}: email sub"


def test_port_picker_markup_31f():
    text = (TEMPLATES / "org_profile.html").read_text()
    assert 'id="port-search"' in text
    assert '@input.debounce.300ms="search()"' in text
    assert 'role="combobox"' in text
    assert 'x-for="port in results"' in text
    assert 'name="ports"' in text
    assert 'class="sv-chip"' in text
    assert "/org/ports/search" in text


def test_zero_verification_state_post_auth_31i():
    banned = ("unverified", "verification/resend", "Verify your email",
              "Resend verification")
    for name in PAGE_TEMPLATES + ("base",):
        text = (TEMPLATES / f"{name}.html").read_text()
        for word in banned:
            assert word not in text, f"{name}: verification state {word!r}"


def test_rfq_form_posts_to_org_url():
    text = (TEMPLATES / "rfq_form.html").read_text()
    assert 'action="/org/{{ slug }}/rfq"' in text
