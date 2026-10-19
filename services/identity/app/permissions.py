"""System permission catalog (D2) — SYSTEM-OWNED, code-defined.

Tenants compose roles FROM this catalog; they never invent permissions.
Every permission documents the capability it gates. The catalog extends as
new endpoints ship; adding a value here is a code change, not data.

Catalog (v1, stage-1):
- org.profile.view       view the org's own (non-public) profile data
- org.profile.edit       edit profile fields (PUT /api/orgs/me/profile)
- org.logo.upload        upload/replace the org logo (POST /api/orgs/me/logo)
- org.profile.publish    go live / back to draft (POST /api/orgs/me/profile-status)
- member.invite          invite new members (task 23)
- member.manage          list/change members: roles, active state (tasks 22/23)
- role.manage            create/edit/delete custom roles (role CRUD)
- rfq.create             raise RFQs against other orgs (work service)
- rfq.view               see RFQs addressed to / raised by the org (work service)
- quote.create           draft quotes on received RFQs (work service)
- quote.send             send a quote revision (work service)
- quote.accept           accept a quote, creating a job (work service)
- job.view               view jobs (work service)
- job.assign             assign jobs to surveyors (work service)

work-service permissions are catalogued now and enforced when those routes
get their own wall (they carry no identity-DB enforcement today).
"""
from __future__ import annotations

import enum


class Permission(str, enum.Enum):
    ORG_PROFILE_VIEW = "org.profile.view"
    ORG_PROFILE_EDIT = "org.profile.edit"
    ORG_LOGO_UPLOAD = "org.logo.upload"
    ORG_PROFILE_PUBLISH = "org.profile.publish"
    MEMBER_INVITE = "member.invite"
    MEMBER_MANAGE = "member.manage"
    ROLE_MANAGE = "role.manage"
    RFQ_CREATE = "rfq.create"
    RFQ_VIEW = "rfq.view"
    QUOTE_CREATE = "quote.create"
    QUOTE_SEND = "quote.send"
    QUOTE_ACCEPT = "quote.accept"
    JOB_VIEW = "job.view"
    JOB_ASSIGN = "job.assign"


ALL_PERMISSIONS: frozenset[str] = frozenset(p.value for p in Permission)


def valid_permissions(values: list[str]) -> list[str]:
    """Return the subset of `values` that are catalog members (D2 guard)."""
    return [v for v in values if v in ALL_PERMISSIONS]


# ---------------------------------------------------------- default roles

# 31e role semantics (profile-blade gating):
# - owner/admin: full profile blade (view + edit + logo + publish)
# - ops: profile VIEW only — the blade renders read-only, no edit controls
# - surveyor/viewer: NO profile access — direct navigation 404s and the
#   rail company cell is hidden (permission-driven, not role-name-driven)
DEFAULT_ROLES: dict[str, frozenset[str]] = {
    "owner": ALL_PERMISSIONS,
    "admin": ALL_PERMISSIONS - {Permission.ROLE_MANAGE.value},
    "ops": frozenset({
        Permission.ORG_PROFILE_VIEW.value,
        Permission.RFQ_CREATE.value,
        Permission.RFQ_VIEW.value,
        Permission.QUOTE_CREATE.value,
        Permission.QUOTE_SEND.value,
        Permission.QUOTE_ACCEPT.value,
        Permission.JOB_VIEW.value,
        Permission.JOB_ASSIGN.value,
    }),
    "surveyor": frozenset({
        Permission.RFQ_VIEW.value,
        Permission.JOB_VIEW.value,
    }),
    "viewer": frozenset({
        Permission.RFQ_VIEW.value,
        Permission.JOB_VIEW.value,
    }),
}
