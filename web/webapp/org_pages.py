"""Authenticated org pages. The deny-by-default middleware guarantees a
resolved AuthContext here — require_auth reads it from the request scope
(no second identity round-trip).

31f: the profile page is a VIEW/EDIT pair. View mode renders the org's
public page through the SAME content partial as /<slug> (WYSIWYG by
construction); edit mode (org.profile.edit) carries the Save button that
persists about + slug + ports of operation in one PUT.

31g: /dashboard is the post-auth landing page (home_for).

31i: login gates on a verified email, so every post-auth surface carries
ZERO verification state — no banner, no account-menu tag, no resend. The
only verification UI is the pre-login gate screen (auth_pages).

31p: §9 retired the blade stack — these routes render full pages inside
the fixed-sidebar shell; routes and deep-linking are unchanged.

31j: the account surface (/org/home) carries the trusted-device panel
(count + revoke-all); the profile editor states the public address
plainly (placeholder prompt vs live URL + draft→404 one-liner)."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from seavix_ports import AuthContext, MembershipContext, require_auth

from .auth import (
    SESSION_HEADER,
    clear_trusted_cookie,
    home_for,
    session_id_from,
)
from .countries import COUNTRIES, country_name, split_dial
from .filters import flag
from .filters import install as install_template_filters
from .identity_client import identity_request
from .registry_client import registry_request, resolve_ports

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
install_template_filters(templates.env)

_KEEPALIVE = """
<script>
setInterval(() => fetch('/session/refresh', {method: 'POST'}), 30 * 60 * 1000);
</script>
"""

ORG_PROFILE_VIEW = "org.profile.view"
ORG_PROFILE_EDIT = "org.profile.edit"
ORG_LOGO_UPLOAD = "org.logo.upload"


def _first_membership(ctx: AuthContext) -> MembershipContext | None:
    return ctx.memberships[0] if ctx.memberships else None


async def _org_summary(slug: str) -> dict | None:
    resp = await identity_request("GET", f"/api/orgs/{slug}")
    return resp.json() if resp.status_code == 200 else None


async def _org_with_ports(org: dict | None) -> dict | None:
    """Public org summary with ports resolved to registry names — the one
    shape the shared content partial renders (31f)."""
    if org is None:
        return None
    resolved = await resolve_ports(org.get("ports") or [])
    return {**org, "ports": resolved}


def _redirect(target: str):
    return RedirectResponse(target, status_code=303)


def _has(membership: MembershipContext | None, permission: str) -> bool:
    return membership is not None and permission in membership.permissions


def _rail_org(org: dict | None, membership: MembershipContext | None) -> dict:
    """31g: the rail company cell renders on every page — the org logo
    when one is set, otherwise initials derived from the company name,
    plus the name itself. Same public org summary the page renders."""
    name = (org or {}).get("name") or (membership.slug if membership else "")
    words = name.split()
    initials = ("".join(w[0] for w in words[:2]) or "S").upper()
    return {"name": name, "logo_url": (org or {}).get("logo_url"),
            "initials": initials}


# 31j: the registration flow never asks for a slug — identity's
# create_organization generates "<slugified-name>-<6 hex>" (orgs.py
# _generate_slug). A slug still matching that shape is an auto-generated
# placeholder: the editor must say "choose your address" loudly instead
# of presenting it as if it were the company's real public URL.
def _is_generated_slug(name: str, slug: str) -> bool:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "org"
    return re.fullmatch(re.escape(base) + r"-[0-9a-f]{6}", slug or "") is not None


def _dial_item(cc: str | None, country_code: str | None) -> dict | None:
    """31u: the dial-code combobox's pre-selected entry. One dial code
    maps to several ISO entries (GG/JE/IM/GB are all +44) — the org's
    own country wins when its dial matches, else the first alpha-sorted
    entry carrying that dial code."""
    if not cc:
        return None
    fallback = None
    for name, alpha2, dial in COUNTRIES:
        if dial == cc:
            if country_code and alpha2 == country_code:
                return {"name": name, "iso2": alpha2, "dial": dial,
                        "flag": flag(alpha2)}
            if fallback is None:
                fallback = {"name": name, "iso2": alpha2, "dial": dial,
                            "flag": flag(alpha2)}
    return fallback


def _dial_label(cc: str | None, item: dict | None) -> str:
    """31u: the pre-selected dial-code label ("🇦🇪 +971 — United Arab
    Emirates") — must stay in sync with the format function the
    combobox passes for client-side picks."""
    if not cc or not item:
        return ""
    return f'{item["flag"]} {cc} — {item["name"]}'


async def _trusted_device_count(request: Request) -> int:
    """31j account surface: active trusted devices for the signed-in user."""
    sid = session_id_from(request)
    if not sid:
        return 0
    resp = await identity_request(
        "GET", "/api/auth/trusted-devices", headers={SESSION_HEADER: sid}
    )
    if resp.status_code != 200:
        return 0
    return int(resp.json().get("count", 0))


def _page_not_found(request: Request, ctx: AuthContext) -> HTMLResponse:
    """404-over-403 (task-23 policy, extended to permission-gated pages):
    a surface the membership has no permission to know about renders the
    friendly not-found page with a REAL 404 — never a 403."""
    membership = _first_membership(ctx)
    return templates.TemplateResponse(
        request,
        "org_not_found.html",
        {"request": request, "ctx": ctx, "membership": membership,
         "rail_org": _rail_org(None, membership), "keepalive": _KEEPALIVE},
        status_code=404,
    )


def _view_context(ctx, membership, org, saved: bool,
                  published: bool = False, unpublished: bool = False) -> dict:
    return {
        "request": None, "ctx": ctx, "membership": membership, "org": org,
        "rail_org": _rail_org(org, membership),
        "keepalive": _KEEPALIVE, "mode": "view",
        "can_edit": _has(membership, ORG_PROFILE_EDIT), "saved": saved,
        "published": published, "unpublished": unpublished,
        "can_publish": _has(membership, ORG_PROFILE_PUBLISH),
        # 32e: view mode previews the member's OWN public profile — the
        # shared content partial renders the RFQ CTA disabled (same as
        # the live /org/<slug> page renders it for the owner), keeping
        # the 31f preview==public byte-equality contract.
        "own_profile": True,
    }


def _edit_context(ctx, membership, org, *, slug_error=None, port_error=None,
                  logo_saved=False, logo_error=None, field_errors=None) -> dict:
    # 31t: legacy dial splitting. A 31m-era phone like "+971 50 123 4567"
    # pre-fills cc=+971 + national="50 123 4567" (longest dial-prefix
    # match); a stored cc always wins. 31u: the cc pickers also resolve
    # the pre-selected entry (flag + label) the combobox starts with.
    def _prefill(cc: str | None, national: str | None) -> dict:
        if cc:
            split_cc, rest = cc, national or ""
        else:
            split_cc, rest = split_dial(national)
        item = _dial_item(split_cc, (org or {}).get("country_code"))
        return {"cc": split_cc, "national": rest, "item": item,
                "label": _dial_label(split_cc, item)}
    country_code = (org or {}).get("country_code") or ""
    resolved_country = country_name(country_code)
    country_label = (f"{flag(country_code)} {resolved_country}"
                     if resolved_country else "")
    return {
        "request": None, "ctx": ctx, "membership": membership, "org": org,
        "rail_org": _rail_org(org, membership),
        "keepalive": _KEEPALIVE, "mode": "edit",
        "can_edit": _has(membership, ORG_PROFILE_EDIT),
        "slug_error": slug_error, "port_error": port_error,
        "logo_saved": logo_saved, "logo_error": logo_error,
        # 31m: per-field inline errors for the public contact block
        # (address/phone/contact_email/whatsapp), keyed by field name.
        "field_errors": field_errors or {},
        # 31u: the vendored ISO list as combobox items (client-side
        # filtering — no endpoint; the list is a constant). Flags are
        # regional-indicator emoji computed server-side.
        "sv_countries": [{"name": name, "iso2": a2, "dial": dial,
                          "flag": flag(a2)}
                         for name, a2, dial in COUNTRIES],
        "country_label": country_label,
        "phone_prefill": _prefill(
            (org or {}).get("phone_cc"), (org or {}).get("phone")),
        "whatsapp_prefill": _prefill(
            (org or {}).get("whatsapp_cc"), (org or {}).get("whatsapp")),
        # 31j: the editor states the public address plainly — placeholder
        # slug vs chosen slug, and draft vs live reachability.
        "slug_is_placeholder": _is_generated_slug(
            (org or {}).get("name") or "", (org or {}).get("slug") or ""
        ),
    }


def _render(request: Request, template: str, ctx: dict,
            status_code: int = 200) -> HTMLResponse:
    ctx["request"] = request
    return templates.TemplateResponse(request, template, ctx,
                                      status_code=status_code)


@router.get("/org/profile", response_class=HTMLResponse)
async def org_profile(
    request: Request,
    view: str = "",
    saved: str = "",
    published: str = "",
    unpublished: str = "",
    ctx: AuthContext = Depends(require_auth),
):
    membership = _first_membership(ctx)
    if membership is None:
        return _redirect("/org/home")
    if membership.org_type != "survey_company":
        return _redirect(home_for(membership.org_type))
    # 31e: the page requires org.profile.view. Without it this route is a
    # 404 (not 403), and the rail company cell that links here is hidden.
    if not _has(membership, ORG_PROFILE_VIEW):
        return _page_not_found(request, ctx)
    org = await _org_with_ports(await _org_summary(membership.slug))
    # 31f view/edit split: ?view=edit opens the editor (edit permission
    # required); anything else renders the WYSIWYG public preview.
    if view == "edit" and _has(membership, ORG_PROFILE_EDIT):
        return _render(request, "org_profile.html",
                       _edit_context(ctx, membership, org))
    return _render(request, "org_profile.html",
                   _view_context(ctx, membership, org, saved == "1",
                                 published == "1", unpublished == "1"))


ORG_PROFILE_PUBLISH = "org.profile.publish"


def _publish_context(ctx, membership, org, error=None) -> dict:
    return {
        "request": None, "ctx": ctx, "membership": membership, "org": org,
        "rail_org": _rail_org(org, membership),
        "keepalive": _KEEPALIVE, "mode": "view", "error": error,
    }


@router.get("/org/profile/publish", response_class=HTMLResponse)
async def profile_publish_form(
    request: Request, ctx: AuthContext = Depends(require_auth),
):
    """Owner 24 Oct: the canonical publish flow is
    Profile → Edit → Preview → Publish / Go live. The preview links here;
    this is the explicit human gate (no silent state change on Save)."""
    membership = _first_membership(ctx)
    if membership is None:
        return _redirect("/org/home")
    if membership.org_type != "survey_company":
        return _redirect(home_for(membership.org_type))
    if not _has(membership, ORG_PROFILE_PUBLISH):
        return _page_not_found(request, ctx)
    org = await _org_summary(membership.slug)
    return _render(request, "org_profile_publish.html",
                   _publish_context(ctx, membership, org))


@router.post("/org/profile/publish")
async def profile_publish(
    request: Request,
    action: str = Form("live"),
    ctx: AuthContext = Depends(require_auth),
):
    membership = _first_membership(ctx)
    if membership is None:
        return _redirect("/org/home")
    if membership.org_type != "survey_company":
        return _redirect(home_for(membership.org_type))
    if not _has(membership, ORG_PROFILE_PUBLISH):
        return _page_not_found(request, ctx)
    org = await _org_summary(membership.slug)
    if action not in ("live", "draft"):
        return _render(request, "org_profile_publish.html",
                       _publish_context(ctx, membership, org,
                                        error="unknown action"),
                       status_code=422)
    sid = session_id_from(request)
    resp = await identity_request(
        "POST", "/api/orgs/me/profile-status", json={"status": action},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code == 200:
        flag = "published" if action == "live" else "unpublished"
        return _redirect(f"/org/profile?{flag}=1")
    detail = ""
    try:
        detail = str(resp.json().get("detail", ""))
    except Exception:
        pass
    if resp.status_code == 403:
        return _render(request, "org_profile_publish.html",
                       _publish_context(ctx, membership, org,
                                        error=detail or
                                        "verify your email before going live"),
                       status_code=403)
    return _render(request, "org_profile_publish.html",
                   _publish_context(ctx, membership, org,
                                    error=detail or "could not update — "
                                    "try again"),
                   status_code=502)


@router.get("/org/ports/search")
async def org_ports_search(request: Request, q: str = "",
                           ctx: AuthContext = Depends(require_auth)):
    """Alpine combobox backend for the port picker: proxies the registry
    port search (name / UN/LOCODE / country). Reference data — any signed-in
    member may query it; only edit mode surfaces it."""
    resp = await registry_request("GET", "/ports", params={"search": q})
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="registry unavailable")
    return resp.json()


# 32d: the services + vessel-type + survey-type pickers proxy IDENTITY's own
# catalogs (migration 0011/0015) — same reference-data rule as the port
# picker. Identity requires a session, so the caller's session header rides
# along.
@router.get("/org/services/search")
async def org_services_search(request: Request, q: str = "",
                              ctx: AuthContext = Depends(require_auth)):
    sid = session_id_from(request)
    resp = await identity_request(
        "GET", "/api/catalog/services/search", params={"q": q},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity unavailable")
    return resp.json()


@router.get("/org/vessel-types/search")
async def org_vessel_types_search(request: Request, q: str = "",
                                  ctx: AuthContext = Depends(require_auth)):
    sid = session_id_from(request)
    resp = await identity_request(
        "GET", "/api/catalog/vessel-types/search", params={"q": q},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity unavailable")
    return resp.json()


@router.get("/org/survey-types/search")
async def org_survey_types_search(request: Request, q: str = "",
                                  ctx: AuthContext = Depends(require_auth)):
    sid = session_id_from(request)
    resp = await identity_request(
        "GET", "/api/catalog/survey-types/search", params={"q": q},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="identity unavailable")
    return resp.json()


# 32d: picker "Create …" proxies — POST the query as a new custom catalog
# entry (identity: origin='custom' + created_by_org_id; duplicate label
# returns the existing entry). The chip is added client-side from the
# response; the profile Save persists the org join.
class _CatalogLabelIn(BaseModel):
    label: str


@router.post("/org/services/create")
async def org_service_create(payload: _CatalogLabelIn,
                             request: Request,
                             ctx: AuthContext = Depends(require_auth)):
    sid = session_id_from(request)
    resp = await identity_request(
        "POST", "/api/catalog/services", json=payload.model_dump(),
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="identity unavailable")
    return JSONResponse(resp.json(), status_code=resp.status_code)


@router.post("/org/vessel-types/create")
async def org_vessel_type_create(payload: _CatalogLabelIn,
                                 request: Request,
                                 ctx: AuthContext = Depends(require_auth)):
    sid = session_id_from(request)
    resp = await identity_request(
        "POST", "/api/catalog/vessel-types", json=payload.model_dump(),
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="identity unavailable")
    return JSONResponse(resp.json(), status_code=resp.status_code)


@router.post("/org/survey-types/create")
async def org_survey_type_create(payload: _CatalogLabelIn,
                                 request: Request,
                                 ctx: AuthContext = Depends(require_auth)):
    sid = session_id_from(request)
    resp = await identity_request(
        "POST", "/api/catalog/survey-types", json=payload.model_dump(),
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="identity unavailable")
    return JSONResponse(resp.json(), status_code=resp.status_code)


@router.post("/org/profile")
async def org_profile_update(
    request: Request,
    slug: str = Form(...),
    about: str = Form(""),
    address: str = Form(""),
    phone: str = Form(""),
    contact_email: str = Form(""),
    whatsapp: str = Form(""),
    ports: list[str] = Form([]),
    services: list[str] = Form([]),
    vessel_types: list[str] = Form([]),
    survey_types: list[str] = Form([]),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    postal_code: str = Form(""),
    country_code: str = Form(""),
    phone_cc: str = Form(""),
    whatsapp_cc: str = Form(""),
    ctx: AuthContext = Depends(require_auth),
):
    """Edit-mode Save: services description + page slug + ports of
    operation + services + vessel types + the structured public contact
    block in one PUT. Ports are validated against the registry before
    any write; services/vessel types are validated by identity against
    its own catalog (422 detail names the field); field errors re-render
    edit mode inline. 31e: edits require org.profile.edit — otherwise
    the same 404 as the page.

    31t: `address` arrives as a HIDDEN field preserving any legacy
    free-text address verbatim (the editor no longer shows it); the
    structured fields ride the same Save. Legacy validation lives in
    identity (national numbers: digits/spaces/dashes, 5-14 digits)."""
    membership = _first_membership(ctx)
    if membership is None or membership.org_type != "survey_company":
        raise HTTPException(status_code=403, detail="not available")
    if not _has(membership, ORG_PROFILE_EDIT):
        return _page_not_found(request, ctx)

    codes = [p.strip().upper() for p in ports if p.strip()]
    svc_codes = [s.strip() for s in services if s.strip()]
    vt_codes = [v.strip() for v in vessel_types if v.strip()]
    st_codes = [s.strip() for s in survey_types if s.strip()]
    org = await _org_with_ports(await _org_summary(membership.slug))
    resolved = await resolve_ports(codes)
    known = {p["code"] for p in resolved}
    unknown = [c for c in codes if c not in known]
    if unknown:
        return _render(
            request, "org_profile.html",
            {**_edit_context(ctx, membership, org,
                             port_error=f"unknown port: {unknown[0]}"),
             "org": {**(org or {}), "ports": resolved}},
            status_code=422,
        )

    # 31t: the structured public contact block rides the same Save;
    # empty fields clear the column. contact_email is the PUBLIC
    # business contact — nothing here ever reads the account email.
    submitted = {
        "about": about.strip() or None,
        "address": address.strip() or None,
        "phone": phone.strip() or None,
        "contact_email": contact_email.strip() or None,
        "whatsapp": whatsapp.strip() or None,
        "address_line1": address_line1.strip() or None,
        "address_line2": address_line2.strip() or None,
        "city": city.strip() or None,
        "state": state.strip() or None,
        "postal_code": postal_code.strip() or None,
        "country_code": country_code.strip().upper() or None,
        "phone_cc": phone_cc.strip() or None,
        "whatsapp_cc": whatsapp_cc.strip() or None,
    }
    sid = session_id_from(request)
    resp = await identity_request(
        "PUT", "/api/orgs/me/profile",
        json={"slug": slug.strip(), **submitted, "ports": codes,
              "services": svc_codes, "vessel_types": vt_codes,
              "survey_types": st_codes},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    if resp.status_code == 200:
        return _redirect("/org/profile?saved=1")

    # re-render edit mode with the SUBMITTED values (the user keeps what
    # they typed) and the field error surfaced inline
    org = {**(org or {}), **submitted, "ports": resolved}
    slug_error = None
    field_errors: dict[str, str] = {}
    detail = resp.json().get("detail")
    if resp.status_code == 409 and isinstance(detail, dict):
        slug_error = f"{detail.get('field', 'slug')}: {detail.get('message', 'conflict')}"
    elif resp.status_code == 422 and isinstance(detail, list):
        for item in detail:
            msg = str(item.get("msg", ""))
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            loc = item.get("loc") or []
            field = str(loc[-1]) if loc else ""
            if field == "slug":
                slug_error = ("that address is reserved — pick another"
                              if "reserved" in msg else
                              "use 2-64 lowercase letters, digits and dashes")
            else:
                field_errors[field] = msg
    if slug_error is None:
        slug_error = "could not save — try again"
    return _render(
        request, "org_profile.html",
        _edit_context(ctx, membership, org, slug_error=slug_error,
                      field_errors=field_errors),
        status_code=resp.status_code if resp.status_code in (409, 422) else 502,
    )


@router.post("/org/profile/logo", response_class=HTMLResponse)
async def org_profile_logo(
    request: Request,
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_auth),
):
    """Logo upload proxies to the identity service (org.logo.upload enforced
    there); re-renders EDIT mode with the outcome. 31e: the route itself
    pre-checks org.logo.upload — without it, the same 404 as the page."""
    membership = _first_membership(ctx)
    if membership is None or membership.org_type != "survey_company":
        raise HTTPException(status_code=403, detail="not available")
    if not _has(membership, ORG_LOGO_UPLOAD):
        return _page_not_found(request, ctx)
    sid = session_id_from(request)
    resp = await identity_request(
        "POST", "/api/orgs/me/logo",
        files={"file": (file.filename or "logo",
                        await file.read(), file.content_type)},
        headers={SESSION_HEADER: sid} if sid else {},
    )
    org = await _org_with_ports(await _org_summary(membership.slug))
    logo_error = None
    if resp.status_code != 200:
        logo_error = resp.json().get("detail", "could not upload — try again")
    return _render(
        request, "org_profile.html",
        _edit_context(ctx, membership, org,
                      logo_saved=resp.status_code == 200, logo_error=logo_error),
        status_code=200 if resp.status_code == 200 else 502,
    )


async def _org_home_context(
    request: Request, ctx: AuthContext, *, devices_revoked: bool = False,
    password_changed: bool = False, password_error: str | None = None,
) -> dict:
    membership = _first_membership(ctx)
    org = await _org_summary(membership.slug) if membership else None
    return {
        "request": request, "ctx": ctx, "membership": membership, "org": org,
        "rail_org": _rail_org(org, membership),
        "trusted_device_count": await _trusted_device_count(request),
        "devices_revoked": devices_revoked,
        "password_changed": password_changed,
        "password_error": password_error,
        "keepalive": _KEEPALIVE,
    }


@router.get("/org/home", response_class=HTMLResponse)
async def org_home(request: Request, ctx: AuthContext = Depends(require_auth),
                   devices: str = "", password: str = ""):
    return templates.TemplateResponse(
        request,
        "org_home.html",
        await _org_home_context(
            request, ctx,
            devices_revoked=devices == "revoked",
            password_changed=password == "changed",
        ),
    )


@router.post("/account/password")
async def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    ctx: AuthContext = Depends(require_auth),
):
    """32b FEATURE 3: Account → Change password. The signed-in session is
    the reauthentication factor — identity verifies the current password,
    rotates the Firebase password, and revokes every OTHER session and
    trusted device. This browser's session deliberately survives, so the
    success path is a plain redirect with a flash; a 422 re-renders the
    account page inline with the validation detail."""
    sid = session_id_from(request)
    if not sid:
        return _redirect("/login")  # the wall guarantees a session; fail closed
    resp = await identity_request(
        "POST", "/api/auth/change-password",
        json={"current_password": current_password,
              "new_password": new_password,
              "confirm_password": confirm_password},
        headers={SESSION_HEADER: sid},
    )
    if resp.status_code == 200:
        return _redirect("/org/home?password=changed")

    error = "could not change password — try again"
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    if resp.status_code == 422:
        if isinstance(detail, dict):
            error = str(detail.get("message") or error)
        elif isinstance(detail, list) and detail:
            msg = str(detail[0].get("msg", ""))
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, "):]
            error = msg or error
    return _render(
        request, "org_home.html",
        await _org_home_context(request, ctx, password_error=error),
        status_code=422 if resp.status_code == 422 else 502,
    )


@router.post("/account/trusted-devices/revoke-all")
async def trusted_devices_revoke_all(
    request: Request, ctx: AuthContext = Depends(require_auth),
) -> RedirectResponse:
    """31j: 'Sign out all trusted devices' — revokes every enrolment at the
    identity service and drops THIS browser's cookie too, so the next
    sign-in here needs the OTP like everywhere else."""
    sid = session_id_from(request)
    if sid:
        await identity_request(
            "DELETE", "/api/auth/trusted-devices",
            headers={SESSION_HEADER: sid},
        )
    response = _redirect("/org/home?devices=revoked")
    clear_trusted_cookie(response)
    return response


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request,
                    ctx: AuthContext = Depends(require_auth)):
    """31g: the post-auth landing page — every active member lands here
    after sign-in (home_for), and signed-in "/" redirects here. Until the
    operational surfaces (jobs, RFQs, activity) ship, the body is an
    honest placeholder. 31i: zero verification state — login gates on a
    verified email, so post-auth chrome carries no banner, tag or resend."""
    membership = _first_membership(ctx)
    org = await _org_summary(membership.slug) if membership else None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "ctx": ctx, "membership": membership, "org": org,
         "rail_org": _rail_org(org, membership),
         "keepalive": _KEEPALIVE},
    )
