"""Application settings, loaded once from environment variables.

A frozen dataclass rather than pydantic-settings: one fewer dependency, and tests
can build a ``Settings(...)`` directly and hand it to ``create_app(settings)``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Mapping

DEFAULT_CORS_ORIGINS = ("http://localhost:8501", "http://localhost:3000")
MIN_JWT_SECRET_LENGTH = 32
RATE_LIMIT_BUCKETS = ("login", "signup", "refresh", "chat")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(RuntimeError):
    """Raised at startup when the environment is missing or has invalid settings."""


@dataclass(frozen=True)
class RateLimit:
    """At most ``max_requests`` per sliding ``window_seconds``."""

    max_requests: int
    window_seconds: float

    @classmethod
    def parse(cls, raw: str, name: str = "rate limit") -> "RateLimit":
        """Parse ``"N/window_seconds"``, e.g. ``"10/60"``."""
        try:
            count_s, window_s = raw.strip().split("/", 1)
            limit = cls(int(count_s), float(window_s))
        except ValueError:
            raise ConfigError(f"{name} must look like 'N/seconds' (e.g. '10/60'), got {raw!r}") from None
        if limit.max_requests < 1 or limit.window_seconds <= 0:
            raise ConfigError(f"{name} must have N >= 1 and seconds > 0, got {raw!r}")
        return limit


def _default_rate_limits() -> dict[str, RateLimit]:
    return {
        "login": RateLimit(10, 60),
        "signup": RateLimit(5, 60),
        "refresh": RateLimit(30, 60),
        "chat": RateLimit(30, 60),
    }


@dataclass(frozen=True)
class Settings:
    # repr=False on secrets/URIs so a logged or printed Settings never leaks them.
    jwt_secret: str = field(repr=False)
    mongo_uri: str = field(default="mongodb://localhost:27017", repr=False)
    mongo_db: str = "wanderchat"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    use_mock_graph: bool = True
    mock_graph_delay_seconds: float = 0.0
    stream_node: str = "generate"
    mongo_startup_timeout_seconds: float = 30.0
    log_level: str = "INFO"
    rate_limits: Mapping[str, RateLimit] = field(default_factory=_default_rate_limits)

    def __post_init__(self) -> None:
        if not self.jwt_secret or len(self.jwt_secret) < MIN_JWT_SECRET_LENGTH:
            raise ConfigError(f"JWT_SECRET is required and must be at least {MIN_JWT_SECRET_LENGTH} characters")
        if self.jwt_algorithm != "HS256":
            raise ConfigError("jwt_algorithm is fixed to HS256")
        missing = set(RATE_LIMIT_BUCKETS) - set(self.rate_limits)
        if missing:
            raise ConfigError(f"rate_limits missing buckets: {sorted(missing)}")
        if self.access_token_ttl_minutes <= 0:
            raise ConfigError("ACCESS_TOKEN_TTL_MINUTES must be > 0")
        if self.refresh_token_ttl_days <= 0:
            raise ConfigError("REFRESH_TOKEN_TTL_DAYS must be > 0")
        if self.mock_graph_delay_seconds < 0:
            raise ConfigError("MOCK_GRAPH_DELAY_SECONDS must be >= 0")
        if self.log_level not in LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {list(LOG_LEVELS)}, got {self.log_level!r}")

    def rate_limit(self, bucket: str) -> RateLimit:
        return self.rate_limits[bucket]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ

        def get(name: str, default: str) -> str:
            value = env.get(name)
            return default if value is None or value.strip() == "" else value.strip()

        defaults = _default_rate_limits()
        rate_limits = {}
        for bucket in RATE_LIMIT_BUCKETS:
            var = f"RATE_LIMIT_{bucket.upper()}"
            raw = env.get(var)
            rate_limits[bucket] = RateLimit.parse(raw, var) if raw and raw.strip() else defaults[bucket]

        origins = tuple(o.strip() for o in get("CORS_ORIGINS", ",".join(DEFAULT_CORS_ORIGINS)).split(",") if o.strip())

        return cls(
            jwt_secret=env.get("JWT_SECRET", ""),
            mongo_uri=get("MONGO_URI", "mongodb://localhost:27017"),
            mongo_db=get("MONGO_DB", "wanderchat"),
            access_token_ttl_minutes=_to_int(get("ACCESS_TOKEN_TTL_MINUTES", "15"), "ACCESS_TOKEN_TTL_MINUTES"),
            refresh_token_ttl_days=_to_int(get("REFRESH_TOKEN_TTL_DAYS", "7"), "REFRESH_TOKEN_TTL_DAYS"),
            cors_origins=origins,
            use_mock_graph=_to_bool(get("USE_MOCK_GRAPH", "true"), "USE_MOCK_GRAPH"),
            mock_graph_delay_seconds=_to_float(get("MOCK_GRAPH_DELAY_SECONDS", "0"), "MOCK_GRAPH_DELAY_SECONDS"),
            stream_node=get("STREAM_NODE", "generate"),
            mongo_startup_timeout_seconds=_to_float(
                get("MONGO_STARTUP_TIMEOUT_SECONDS", "30"), "MONGO_STARTUP_TIMEOUT_SECONDS"
            ),
            log_level=get("LOG_LEVEL", "INFO").upper(),
            rate_limits=rate_limits,
        )


def _to_int(raw: str, name: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def _to_float(raw: str, name: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def _to_bool(raw: str, name: str) -> bool:
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings from ``os.environ`` (cached; call ``cache_clear()`` in tests)."""
    return Settings.from_env()
