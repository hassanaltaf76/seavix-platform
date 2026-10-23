"""Request/response schemas for the public registration API."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

SLUG_RE = re.compile(r"^[a-z0-9-]+$")

# Organization type, chosen from a dropdown at registration (form values map
# to these). Stored verbatim in organization.type (TEXT — no migration).
ORG_TYPES = ("agent", "management", "survey_company")


class RegisterRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    org_type: Literal["agent", "management", "survey_company"]
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

    model_config = {"extra": "forbid"}
