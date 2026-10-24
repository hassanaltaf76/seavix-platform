"""Request/response schemas for the public registration API."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-z0-9-]+$")

# Organization type, chosen from a dropdown at registration (form values map
# to these). Stored verbatim in organization.type (TEXT — no migration).
# 32b: 'client' joins the vocabulary — the ship-operator funnel
# registration (the public RFQ intake guard lands there). Client orgs get
# the 'client' system role instead of 'owner' (RFQs section only).
ORG_TYPES = ("agent", "management", "survey_company", "client")


class RegisterRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    org_type: Literal["agent", "management", "survey_company", "client"]
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    confirm_password: str = Field(min_length=6, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> "RegisterRequest":
        if self.password != self.confirm_password:
            raise ValueError("passwords do not match")
        return self


class RegisterResponse(BaseModel):
    org_id: str
    slug: str  # server-generated placeholder; the user sets the public slug
        # later in profile settings


class CatalogEntry(BaseModel):
    """31q: one row of a system catalog (service / vessel type) — the code
    is the stored vocabulary, the label is the humanized render text.
    32d: `origin` marks system vs harvested custom entries (custom rows
    carry a badge in picker dropdowns only, never on chips/public)."""
    code: str
    label: str
    origin: str = "system"


class PublicOrgResponse(BaseModel):
    slug: str
    name: str
    type: str
    about: str | None
    website: str | None
    phone: str | None
    # 31m: public contact block. contact_email is the PUBLIC business
    # contact — the account/login email never enters this model.
    address: str | None
    contact_email: str | None
    whatsapp: str | None
    logo_url: str | None
    profile_status: str
    # ports of operation as UN/LOCODE codes (31f); the web app resolves
    # names against the registry for rendering
    ports: list[str] = []
    # 31q: published services + vessel types, RESOLVED to [{code, label}]
    # rows by the identity read model (identity owns these catalogs; the
    # vessel-type codes are the same vocabulary registry uses)
    services: list[CatalogEntry] = []
    vessel_types: list[CatalogEntry] = []
    # 32d: survey types offered, resolved [{code, label}] rows
    survey_types: list[CatalogEntry] = []
    # 31t: structured public address + country dial codes. The legacy
    # free-text `address` stays (deprecated): when ANY structured field
    # is set, structured wins at render and the legacy text is hidden;
    # legacy-only profiles render as before.
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    phone_cc: str | None = None
    whatsapp_cc: str | None = None

    model_config = {"extra": "forbid"}
