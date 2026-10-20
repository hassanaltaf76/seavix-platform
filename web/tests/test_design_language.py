"""Design-language contract tests — Oct 2026 v3 (global LIGHT theme).

Locks in: --seavix-* token family with the light values (bg #FFFFFF, surface
#F5F7FA, border #E2E8F0, ink #12233D, secondary #55677E, accent #1E88E5,
accent-hover #1565C0, navy primary #142E4A), WCAG AA contrast on the LIGHT
surfaces, the three-layout system (base_public / base_auth / blade shell),
zero search markup, no text wordmark next to the logo, the register form
contract (org-type select + confirm password + no slug), the OTP screen
contract, and the favicon/PWA icon set. 31b: the post-auth rail is
logo + company link only (no favorites/services/home items) and the profile
blade is the public-page editor (framing "Your public page — seavix.ai/<slug>"
+ view-public-page link, services description, logo upload, slug + hint).
31b-7: breadcrumbs are the blade navigation — the crumb trail roots at the
dashboard ("Home > Profile"), × closes to its parent crumb (the dashboard
at top level), and no post-auth chrome link (crumbs, ×, logo) drops a
signed-in user onto the public landing.
"""
import json
import re
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
STYLE = (WEB_ROOT / "static" / "style.css").read_text()
BASE = (WEB_ROOT / "templates" / "base.html").read_text()
BASE_PUBLIC = (WEB_ROOT / "templates" / "base_public.html").read_text()
BASE_AUTH = (WEB_ROOT / "templates" / "base_auth.html").read_text()
TEMPLATES = WEB_ROOT / "templates"

AUTH_TEMPLATES = ("login", "register", "forgot_password", "reset_password",
                  "login_otp", "verify_email", "verify_email_confirm",
                  "verify_email_done")
PUBLIC_TEMPLATES = ("landing", "profile", "profile_unavailable",
                    "rfq_form", "rfq_thanks")
BLADE_TEMPLATES = ("org_home", "org_profile", "org_not_found", "dashboard")


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


def test_seavix_token_family_with_light_values():
    tokens = _tokens()
    for name in ("--seavix-navy-900", "--seavix-navy-800", "--seavix-navy-750",
                 "--seavix-navy-700", "--seavix-text-primary",
                 "--seavix-text-secondary", "--seavix-accent",
                 "--seavix-accent-deep", "--seavix-primary",
                 "--seavix-primary-hover", "--seavix-on-primary",
                 "--seavix-border"):
        assert name in tokens, f"missing token {name}"
    expected = {
        "--seavix-navy-900": "#ffffff",
        "--seavix-navy-750": "#f5f7fa",
        "--seavix-navy-700": "#e2e8f0",
        "--seavix-text-primary": "#12233d",
        "--seavix-text-secondary": "#55677e",
        "--seavix-accent": "#1e88e5",
        "--seavix-accent-deep": "#1565c0",
        "--seavix-border": "#e2e8f0",
        "--seavix-primary": "#142e4a",
        "--seavix-on-primary": "#ffffff",
    }
    for name, hex_value in expected.items():
        assert _resolve(tokens, name) == hex_value, (
            f"{name}: expected {hex_value}, got {tokens[name]}"
        )


def test_wcag_aa_light_surface_pairs():
    """Global light theme: every text/background pair the layouts render."""
    tokens = _tokens()
    pairs = {
        ("--seavix-text-primary", "--seavix-navy-900"),   # ink on white
        ("--seavix-text-primary", "--seavix-navy-800"),   # ink on white blades
        ("--seavix-text-primary", "--seavix-navy-750"),   # ink on surface
        ("--seavix-text-secondary", "--seavix-navy-900"),
        ("--seavix-text-secondary", "--seavix-navy-750"),
        ("--seavix-text-secondary", "--seavix-navy-700"), # on hover wash
        ("--seavix-accent-deep", "--seavix-navy-900"),    # links on white
        ("--seavix-accent-deep", "--seavix-navy-750"),    # links on surface
        ("--seavix-accent-bright", "--seavix-navy-900"),  # link hover
        ("--seavix-on-primary", "--seavix-primary"),      # primary button
        ("--seavix-on-primary", "--seavix-primary-hover"),
        ("--seavix-danger", "--seavix-navy-900"),
        ("--seavix-danger", "--seavix-danger-bg"),
        ("--seavix-success", "--seavix-navy-900"),
        ("--seavix-success", "--seavix-success-bg"),
        ("--seavix-accent", "--seavix-navy-900"),         # focus ring (UI 3:1)
    }
    for fg, bg in pairs:
        fg_hex = fg if fg.startswith("#") else _resolve(tokens, fg)
        bg_hex = bg if bg.startswith("#") else _resolve(tokens, bg)
        floor = 3.0 if fg == "--seavix-accent" else 4.5  # non-text UI component
        ratio = contrast(fg_hex, bg_hex)
        assert ratio >= floor, f"{fg} on {bg} = {ratio:.2f}:1 (needs {floor})"


def test_blade_shell_is_post_auth_only():
    """base.html keeps the rail/blades chrome for post-auth pages."""
    assert 'class="sv-topbar"' in BASE
    assert 'class="sv-rail"' in BASE
    assert "sv-rail-tenant" in BASE
    # 31b: rail = logo + company only — no favorites/services sections while
    # no services exist, and no "SeaVix home" menu item anywhere
    assert "Favorites" not in BASE
    assert "All services" not in BASE
    assert "SeaVix home" not in BASE
    assert "sv-rail-fav" not in BASE
    assert "sv-rail-section" not in BASE
    # the company in the rail links to the profile blade
    assert 'href="/org/profile"' in BASE
    assert 'class="sv-blades"' in BASE
    assert "sv-blade-crumbs" in BASE
    assert "sv-blade-titlebar" in BASE
    assert "{% block blade_tabs %}" in BASE
    assert "{% block blade_close %}" in BASE
    assert "/static/vendor/htmx.min.js" in BASE
    assert "/static/vendor/alpine.min.js" in BASE
    assert "sv-skip" in BASE
    assert 'x-data' in BASE and "railCollapsed" in BASE
    # NO global search anywhere in the chrome (returns with the ops console)
    assert 'action="/search"' not in BASE
    assert "sv-search" not in BASE
    assert 'role="search"' not in BASE
    for tpl in TEMPLATES.glob("*.html"):
        text = tpl.read_text()
        assert 'action="/search"' not in text, f"{tpl.name}: search markup"
        assert "sv-search" not in text, f"{tpl.name}: search markup"
        # 31b: no favorites/services/home rail items on any page
        assert "All services" not in text, f"{tpl.name}: services section"
        assert "SeaVix home" not in text, f"{tpl.name}: home menu item"


def test_blade_navigation_never_drops_to_public_landing():
    """31b-7 (amended 31g): breadcrumbs ARE the blade navigation. The crumb
    trail roots at the dashboard (never the public landing), × closes to
    its parent crumb (the dashboard at top level), and the post-auth rail
    logo goes to the dashboard too."""
    # zero public-landing links in the post-auth chrome
    assert 'href="/"' not in BASE
    assert 'href="/dashboard"' in BASE  # rail logo → dashboard
    dash = (TEMPLATES / "dashboard.html").read_text()
    home = (TEMPLATES / "org_home.html").read_text()
    profile = (TEMPLATES / "org_profile.html").read_text()
    # the dashboard is the top-level blade: crumb root "Home" IS the page
    assert "<li aria-current=\"page\">Home</li>" in dash
    # child blades root their crumbs at the dashboard
    assert '<li><a href="/dashboard">Home</a></li>' in home
    assert "<li aria-current=\"page\">Your account</li>" in home
    assert '<li><a href="/dashboard">Home</a></li>' in profile
    assert "<li aria-current=\"page\">Profile</li>" in profile
    # × closes to the parent crumb; a top-level blade closes to the DASHBOARD
    assert 'class="sv-blade-close" href="/dashboard"' in dash
    assert 'class="sv-blade-close" href="/dashboard"' in home
    assert 'class="sv-blade-close" href="/dashboard"' in profile
    assert 'href="/"' not in dash
    assert 'href="/"' not in home
    assert 'href="/"' not in profile


def test_no_text_wordmark_next_to_logo():
    """The logo image carries the wordmark; no adjacent text wordmark in any
    layout (a text node 'SeaVix' inside the logo container fails the test)."""
    containers = [
        re.search(r'<div class="sv-rail-logo">(.*?)</div>', BASE, re.S),
        re.search(r'<a class="sv-public-brand"[^>]*>(.*?)</a>', BASE_PUBLIC, re.S),
        re.search(r'<a class="sv-auth-logo"[^>]*>(.*?)</a>', BASE_AUTH, re.S),
    ]
    assert all(containers), "logo container not found in a base layout"
    for match in containers:
        inner = re.sub(r"<img[^>]*>", "", match.group(1))  # drop the logo img
        text_only = re.sub(r"<[^>]+>", "", inner).strip()
        assert "SeaVix" not in text_only, (
            f"text wordmark next to logo: {text_only!r}"
        )


def test_public_layout_chrome():
    assert 'class="sv-public"' in BASE_PUBLIC
    assert 'class="sv-public-header"' in BASE_PUBLIC
    assert 'class="sv-public-brand"' in BASE_PUBLIC
    assert "/static/brand/seavix-logo.png" in BASE_PUBLIC
    assert "/login" in BASE_PUBLIC and "/register" in BASE_PUBLIC
    # zero app chrome on public pages
    assert "sv-rail" not in BASE_PUBLIC
    assert "sv-blades" not in BASE_PUBLIC
    assert "sv-search" not in BASE_PUBLIC
    assert "sv-rail-tenant" not in BASE_PUBLIC


def test_auth_layout_centered_card():
    assert 'class="sv-auth"' in BASE_AUTH
    assert 'class="sv-auth-logo"' in BASE_AUTH
    assert 'class="sv-auth-card"' in BASE_AUTH
    assert "max-width: 420px" in STYLE
    assert "seavix-logo.png" in BASE_AUTH
    # zero app chrome on auth pages
    assert "sv-rail" not in BASE_AUTH
    assert "sv-blades" not in BASE_AUTH
    assert "sv-search" not in BASE_AUTH
    assert "sv-public-header" not in BASE_AUTH


def test_template_lineage():
    for name in AUTH_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        assert '{% extends "base_auth.html" %}' in text, f"{name} must extend base_auth"
    for name in PUBLIC_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        assert '{% extends "base_public.html" %}' in text, f"{name} must extend base_public"
    for name in BLADE_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        assert '{% extends "base.html" %}' in text, f"{name} must extend the blade shell"
    for name in AUTH_TEMPLATES + PUBLIC_TEMPLATES:
        text = (TEMPLATES / f"{name}.html").read_text()
        assert "sv-rail" not in text, f"{name}: rail markup on a public/auth page"
        assert "sv-blades" not in text, f"{name}: blade markup on a public/auth page"


def test_register_form_contract():
    text = (TEMPLATES / "register.html").read_text()
    # org type = <select> dropdown with the three mapped values
    select = re.search(r"<select[^>]*name=\"org_type\"[^>]*>(.*?)</select>", text, re.S)
    assert select, "org_type must be a <select>"
    for value, label in (("agent", "Port Agent"),
                         ("management", "Management Company"),
                         ("survey_company", "Surveyor")):
        assert f'value="{value}"' in select.group(1), f"missing option {value}"
        assert label in select.group(1), f"missing label {label}"
    # password entered twice
    assert 'name="password"' in text and 'name="confirm_password"' in text
    # NO public slug at registration (it lives in profile settings)
    assert 'name="slug"' not in text
    # organization name stays a text field
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
    assert 'method="post" action="/login/otp/resend"' in text  # resend w/ cooldown
    assert "Back to sign in" in text
    # one clean numeric input — exactly one text-entry field for the code
    assert text.count('name="code"') == 1
    assert 'type="password"' not in text
    # no log-path/dev references in UI copy
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
    assert 'type="password"' not in text, "no password field on the email-action page"
    assert 'name="confirm_password"' not in text


def test_verify_templates_have_no_password_fields():
    for name in ("verify_email", "verify_email_confirm", "verify_email_done"):
        text = (TEMPLATES / f"{name}.html").read_text()
        assert 'type="password"' not in text, f"{name}: password input on email action"


def test_profile_settings_public_slug_section():
    """31b: the profile blade is the public-page editor — framing with the
    root-level URL, services description, slug + hint, logo upload. 31f:
    the editor lives in edit mode; view mode is the WYSIWYG preview."""
    text = (TEMPLATES / "org_profile.html").read_text()
    assert 'name="slug"' in text
    assert 'name="about"' in text
    assert "seavix.ai/" in text
    assert "seavix.ai/p/" not in text  # profiles moved to the root URL
    assert "Your public page" in text
    # the live page opens from the view-mode tab (31f replaced the 31b
    # "View public page" new-tab button with the preview itself)
    assert '{% if org and org.profile_status == ' in text
    assert ">Live page</a>" in text
    assert 'href="/org/profile?view=edit"' in text  # Edit button
    assert "Describe your services" in text
    assert 'action="/org/profile/logo"' in text
    assert 'name="file"' in text
    assert "set it now or come back later" in text
    assert ">Save public page</button>" in text  # 31d view-edit-save
    # 31g: Save stays disabled until the form is dirty — the Alpine
    # component lives on the form (fields + picker share one scope), the
    # flag starts false, every edit surface sets it, and the submit
    # button binds :disabled to it
    assert "dirty: false" in text
    assert '@input="dirty = true"' in text
    assert "this.dirty = true" in text  # port add()/remove()
    assert ':disabled="!dirty"' in text


def test_rail_company_cell_31g():
    """31g: the rail company cell shows the org LOGO when one is set, else
    INITIALS derived from the company name — never a bare glyph — plus
    the company name, and links to the profile blade. Visibility stays
    permission-driven (org.profile.view)."""
    assert "'org.profile.view' in ctx.memberships[0].permissions" in BASE
    assert "rail_org.logo_url" in BASE
    assert 'class="sv-rail-initials"' in BASE
    assert "rail_org.initials" in BASE
    assert "rail_org.name" in BASE
    assert "◉" not in BASE  # the placeholder glyph is gone
    # every blade route feeds the cell the same rail_org shape
    routes = Path(__file__).resolve().parents[1] / "webapp" / "org_pages.py"
    text = routes.read_text()
    assert text.count("_rail_org(") >= 6  # not-found, view, edit, home, dashboard


def test_profile_blade_is_permission_gated_31e():
    """31e: the blade template renders edit surfaces ONLY when the
    membership holds the permission — view-only members get the preview
    with NO edit chrome (the handler decides `mode`; the template gates
    the logo card on org.logo.upload)."""
    text = (TEMPLATES / "org_profile.html").read_text()
    assert "'org.logo.upload' in membership.permissions" in text
    assert "mode == \"view\" and can_edit" in text  # Edit button gating
    home = (TEMPLATES / "org_home.html").read_text()
    # the rail company cell is permission-gated, never role-name-driven
    assert "'org.profile.view' in ctx.memberships[0].permissions" in BASE
    assert 'href="/org/profile"' in BASE
    # org home link to the blade is permission-gated too, and labelled by
    # capability (edit vs view)
    assert "'org.profile.view' in membership.permissions" in home
    assert "Edit your public page" in home
    assert "View your public page" in home


def test_profile_view_mode_is_public_preview_31f():
    """31f: view mode includes the SAME content partial as the public page
    at /<slug> — WYSIWYG by construction. Both templates must reference
    the shared partial and nothing else renders org content."""
    partial = (TEMPLATES / "_public_org_content.html").read_text()
    profile = (TEMPLATES / "profile.html").read_text()
    blade = (TEMPLATES / "org_profile.html").read_text()
    assert '{% include "_public_org_content.html" %}' in profile
    assert '{% include "_public_org_content.html" %}' in blade
    # the org content lives ONLY in the partial now
    assert "sv-org-header" not in profile
    assert "sv-org-header" not in blade
    # the ports section lives in the shared partial
    assert "Ports we serve" in partial
    assert "org.ports" in partial
    # blade structure: view mode preview, edit chrome behind mode == edit
    assert 'class="card sv-preview"' in blade
    assert '{% if mode == "view" %}' in blade


def test_description_renders_as_blocks_31h():
    """31h: the description renders through the `paragraphs` filter in the
    SHARED partial (public page + blade preview at once) — escaped first,
    blank lines -> <p> blocks, single newlines -> <br> — with the prose
    rhythm locked in the stylesheet."""
    partial = (TEMPLATES / "_public_org_content.html").read_text()
    assert "org.about | paragraphs" in partial
    assert 'class="sv-prose"' in partial
    # user text is NEVER raw HTML: the filter escapes before structuring
    filters = (Path(__file__).resolve().parents[1]
               / "webapp" / "filters.py").read_text()
    assert "html.escape" in filters
    assert "Markup" in filters
    assert ".sv-prose p { margin: 0 0 0.75rem; }" in STYLE


def test_port_picker_markup_31f():
    """31f: the edit-mode port picker is an Alpine combobox wired to the
    registry search endpoint, with removable chips persisted as repeated
    hidden `ports` fields on Save."""
    text = (TEMPLATES / "org_profile.html").read_text()
    assert 'id="port-search"' in text
    assert 'x-model="q"' in text
    assert "@input.debounce.300ms" in text
    assert 'fetch("/org/ports/search?q="' in text
    assert 'role="combobox"' in text
    assert 'x-show="open"' in text
    assert "@click.outside" in text
    assert 'name="ports"' in text  # hidden inputs carry the selection
    assert "remove(port.code)" in text
    assert "add(port)" in text
    # multi-select is unlimited: no max/cap attributes on the picker
    picker = re.search(r'class="sv-port-picker"(.*?)</div>\s*{% if port_error', text, re.S)
    assert picker and "max" not in picker.group(1)


def test_verification_resend_action_wired_31e():
    """31e (amended 31g): the unverified tag + resend live in the signed-in
    ACCOUNT MENU (base.html) and the master banner on /dashboard. The
    profile blade, its edit mode, the account page (org home) and the
    shared public partial carry ZERO verification or account state."""
    base = BASE
    assert 'action="/org/verification/resend"' in base
    assert "sv-tag-warn" in base
    assert "unverified — resend" in base
    assert 'aria-label="Account"' in base  # the tag sits in the account menu

    dash = (TEMPLATES / "dashboard.html").read_text()
    assert "Verify your email to activate your public page." in dash
    assert '"owner" in membership.roles' in dash
    assert 'method="post" action="/org/verification/resend"' in dash

    home = (TEMPLATES / "org_home.html").read_text()
    for banned in ("Verify your email", "verification/resend", "unverified"):
        assert banned not in home, f"org home leaks verification state: {banned!r}"

    # the blade is a public-content surface: NO verification state, NO
    # account email line, NO roles — in either mode
    blade = (TEMPLATES / "org_profile.html").read_text()
    for banned in ("/org/verification/resend", "unverified",
                   "Verify your email", "Signed in as", "email_verified",
                   "membership.roles"):
        assert banned not in blade, f"blade leaks account state: {banned!r}"

    # the shared public partial (blade preview == /<slug>) is account-free
    partial = (TEMPLATES / "_public_org_content.html").read_text()
    for banned in ("unverified", "verification", "ctx.user", "membership"):
        assert banned not in partial, f"public partial leaks account state: {banned!r}"


def test_favicon_and_pwa_icons_derived_from_logo():
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
    assert manifest["theme_color"] == "#FFFFFF"  # light chrome
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
