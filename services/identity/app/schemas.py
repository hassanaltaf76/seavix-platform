"""Request/response schemas for the public registration API."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

SLUG_RE = re.compile(r"^[a-z0-9-]+$")
ACCOUNT_KINDS = ("survey_company", "client")


class RegisterRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    org_type: str = Field(min_length=1, max_length=50)
    slug: str = Field(min_length=2, max_length=64)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    account_kind: Literal["survey_company", "client"]

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v: str) -> str:
        if not SLUG_RE.fullmatch(v):
            raise ValueError("slug must match ^[a-z0-9-]+$")
        return v


class RegisterResponse(BaseModel):
    org_id: str
    slug: str


class PublicOrgResponse(BaseModel):
    slug: str
    name: str
    about: str | None
    website: str | None
    phone: str | None
    logo_url: str | None
    profile_status: str

    model_config = {"extra": "forbid"}
