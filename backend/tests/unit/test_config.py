"""Config parsing: JWT_SECRET required (>= 32 chars), rate-limit strings, env parsing."""

from __future__ import annotations

import pytest

from backend.config import ConfigError, RateLimit, Settings, get_settings

pytestmark = pytest.mark.unit

SECRET = "s" * 32


def env(**kw: str) -> dict[str, str]:
    return {"JWT_SECRET": SECRET, **kw}


@pytest.mark.parametrize("environ", [{}, {"JWT_SECRET": ""}, {"JWT_SECRET": "s" * 31}])
def test_jwt_secret_missing_or_short_fails(environ) -> None:
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        Settings.from_env(environ)


def test_jwt_secret_32_chars_ok() -> None:
    assert Settings.from_env(env()).jwt_secret == SECRET


@pytest.mark.parametrize("secret", ["", "short", "x" * 31])
def test_direct_construction_validates_secret(secret: str) -> None:
    with pytest.raises(ConfigError):
        Settings(jwt_secret=secret)


def test_algorithm_fixed_hs256() -> None:
    with pytest.raises(ConfigError):
        Settings(jwt_secret=SECRET, jwt_algorithm="HS512")


def test_defaults() -> None:
    s = Settings.from_env(env())
    assert s.mongo_db == "wanderchat"
    assert s.jwt_algorithm == "HS256"
    assert s.access_token_ttl_minutes == 15
    assert s.refresh_token_ttl_days == 7
    assert s.cors_origins == ("http://localhost:8501", "http://localhost:3000")
    assert s.use_mock_graph is True
    assert s.mock_graph_delay_seconds == 0.0
    assert s.stream_node == "generate"
    assert s.mongo_startup_timeout_seconds == 30.0
    assert s.rate_limit("login") == RateLimit(10, 60)
    assert s.rate_limit("signup") == RateLimit(5, 60)
    assert s.rate_limit("refresh") == RateLimit(30, 60)
    assert s.rate_limit("chat") == RateLimit(30, 60)


def test_env_values_read() -> None:
    s = Settings.from_env(
        env(
            MONGO_URI="mongodb://db:27017",
            MONGO_DB="other",
            ACCESS_TOKEN_TTL_MINUTES="5",
            REFRESH_TOKEN_TTL_DAYS="1",
            USE_MOCK_GRAPH="false",
            MOCK_GRAPH_DELAY_SECONDS="0.25",
            STREAM_NODE="answer",
            MONGO_STARTUP_TIMEOUT_SECONDS="3",
        )
    )
    assert (s.mongo_uri, s.mongo_db) == ("mongodb://db:27017", "other")
    assert (s.access_token_ttl_minutes, s.refresh_token_ttl_days) == (5, 1)
    assert s.use_mock_graph is False and s.mock_graph_delay_seconds == 0.25
    assert s.stream_node == "answer" and s.mongo_startup_timeout_seconds == 3.0


def test_blank_values_fall_back_to_defaults() -> None:
    s = Settings.from_env(env(MONGO_DB="  ", RATE_LIMIT_LOGIN=" ", CORS_ORIGINS=""))
    assert s.mongo_db == "wanderchat"
    assert s.rate_limit("login") == RateLimit(10, 60)
    assert s.cors_origins == ("http://localhost:8501", "http://localhost:3000")


def test_cors_origins_comma_separated_and_trimmed() -> None:
    s = Settings.from_env(env(CORS_ORIGINS=" https://a.example , https://b.example,, "))
    assert s.cors_origins == ("https://a.example", "https://b.example")


@pytest.mark.parametrize("raw, expected", [("10/60", RateLimit(10, 60)), (" 3/0.5 ", RateLimit(3, 0.5)), ("1/1", RateLimit(1, 1))])
def test_rate_limit_parse_ok(raw: str, expected: RateLimit) -> None:
    assert RateLimit.parse(raw) == expected


@pytest.mark.parametrize("raw", ["", "10", "abc", "10/", "/60", "10/x", "x/60", "0/60", "-1/60", "5/0", "5/-3", "1.5/60", "10-60"])
def test_rate_limit_parse_bad(raw: str) -> None:
    with pytest.raises(ConfigError):
        RateLimit.parse(raw)


@pytest.mark.parametrize("bucket", ["LOGIN", "SIGNUP", "REFRESH", "CHAT"])
def test_rate_limit_env_per_bucket(bucket: str) -> None:
    s = Settings.from_env(env(**{f"RATE_LIMIT_{bucket}": "7/30"}))
    assert s.rate_limit(bucket.lower()) == RateLimit(7, 30)


def test_rate_limit_env_bad_value_names_variable() -> None:
    with pytest.raises(ConfigError, match="RATE_LIMIT_CHAT"):
        Settings.from_env(env(RATE_LIMIT_CHAT="lots"))


@pytest.mark.parametrize(
    "var, value",
    [
        ("ACCESS_TOKEN_TTL_MINUTES", "fifteen"),
        ("REFRESH_TOKEN_TTL_DAYS", "1.5"),
        ("USE_MOCK_GRAPH", "maybe"),
        ("MOCK_GRAPH_DELAY_SECONDS", "slow"),
        ("MOCK_GRAPH_DELAY_SECONDS", "-1"),
        ("MONGO_STARTUP_TIMEOUT_SECONDS", "soon"),
    ],
)
def test_bad_env_values_fail(var: str, value: str) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env(**{var: value}))


@pytest.mark.parametrize("raw, expected", [("1", True), ("TRUE", True), ("yes", True), ("on", True), ("0", False), ("False", False), ("no", False), ("off", False)])
def test_bool_parsing(raw: str, expected: bool) -> None:
    assert Settings.from_env(env(USE_MOCK_GRAPH=raw)).use_mock_graph is expected


def test_missing_rate_limit_bucket_rejected() -> None:
    with pytest.raises(ConfigError):
        Settings(jwt_secret=SECRET, rate_limits={"login": RateLimit(1, 1)})


def test_repr_does_not_leak_secret_or_uri() -> None:
    s = Settings(jwt_secret="TOPSECRET" * 4, mongo_uri="mongodb://user:hunter2@db:27017")
    assert "TOPSECRET" not in repr(s) and "hunter2" not in repr(s)


def test_settings_frozen() -> None:
    s = Settings(jwt_secret=SECRET)
    with pytest.raises(Exception):
        s.jwt_secret = "x" * 40  # type: ignore[misc]


def test_get_settings_reads_os_environ_and_caches(monkeypatch) -> None:
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("JWT_SECRET", "e" * 40)
        monkeypatch.setenv("MONGO_DB", "from_env_db")
        first = get_settings()
        assert first.mongo_db == "from_env_db"
        monkeypatch.setenv("MONGO_DB", "changed")
        assert get_settings() is first
    finally:
        get_settings.cache_clear()


def test_log_level_default_and_case_insensitive() -> None:
    assert Settings.from_env(env()).log_level == "INFO"
    assert Settings.from_env(env(LOG_LEVEL="debug")).log_level == "DEBUG"
    assert Settings.from_env(env(LOG_LEVEL=" Warning ")).log_level == "WARNING"


@pytest.mark.parametrize("bad", ["loud", "TRACE", "verbose", "10"])
def test_invalid_log_level_fails(bad: str) -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env(env(LOG_LEVEL=bad))
