"""Request/response bodies for /auth/*. Tokens travel only in JSON bodies and the
``Authorization`` header, never in URLs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from backend.auth.passwords import password_policy_violation
from backend.auth.tokens import MAX_TOKEN_LENGTH

# Login accepts any non-empty password up to this many characters. Signup enforces
# the real policy (8..72 UTF-8 bytes). The cap only bounds request size.
MAX_LOGIN_PASSWORD_CHARS = 1024

TokenString = Annotated[str, Field(min_length=1, max_length=MAX_TOKEN_LENGTH)]


class _Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def _strip_email(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("email", mode="after")
    @classmethod
    def _lower_email(cls, value: str) -> str:
        # Lowercase the whole address (EmailStr only lowercases the domain), so
        # "Foo@x.com" and "foo@x.com" are one account under the unique index.
        return value.lower()


class SignupRequest(_Credentials):
    password: str = Field(description="8..72 bytes of UTF-8.")

    @field_validator("password")
    @classmethod
    def _password_policy(cls, value: str) -> str:
        reason = password_policy_violation(value)
        if reason is not None:
            raise ValueError(reason)
        return value


class LoginRequest(_Credentials):
    # No signup policy here: a policy change must not lock existing users out, and
    # a short or long password should get the same 401 invalid_credentials as any
    # other wrong password, not a different 422.
    password: str = Field(min_length=1, max_length=MAX_LOGIN_PASSWORD_CHARS)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: TokenString


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: TokenString


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")


class UserResponse(BaseModel):
    id: str
    email: str
    created_at: datetime
