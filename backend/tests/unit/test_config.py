"""Config parsing: defaults, rate-limit strings, env parsing."""

from __future__ import annotations

import pytest

from backend.config import ConfigError, RateLimit, Settings, get_settings

pytestmark = pytest.mark.unit

def env(**kw: str) -> dict[str, str]:
    return dict(kw)


def test_defaults() -> None:
    s = Settings.from_env(env())
    assert s.cors_origins == ("http://localhost:8501", "http://localhost:3000")
    assert s.use_mock_graph is True
    assert s.mock_graph_delay_seconds == 0.0
    assert s.stream_node == "generate"
    assert s.rate_limit("chat") == RateLimit(30, 60)


def test_env_values_read() -> None:
    s = Settings.from_env(
        env(
            USE_MOCK_GRAPH="false",
            MOCK_GRAPH_DELAY_SECONDS="0.25",
            STREAM_NODE="answer",
        )
    )
    assert s.use_mock_graph is False and s.mock_graph_delay_seconds == 0.25
    assert s.stream_node == "answer"


def test_blank_values_fall_back_to_defaults() -> None:
    s = Settings.from_env(env(STREAM_NODE="  ", RATE_LIMIT_CHAT=" ", CORS_ORIGINS=""))
    assert s.stream_node == "generate"
    assert s.rate_limit("chat") == RateLimit(30, 60)
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


def test_rate_limit_env_chat() -> None:
    s = Settings.from_env(env(RATE_LIMIT_CHAT="7/30"))
    assert s.rate_limit("chat") == RateLimit(7, 30)


def test_rate_limit_env_bad_value_names_variable() -> None:
    with pytest.raises(ConfigError, match="RATE_LIMIT_CHAT"):
        Settings.from_env(env(RATE_LIMIT_CHAT="lots"))


@pytest.mark.parametrize(
    "var, value",
    [
        ("USE_MOCK_GRAPH", "maybe"),
        ("MOCK_GRAPH_DELAY_SECONDS", "slow"),
        ("MOCK_GRAPH_DELAY_SECONDS", "-1"),
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
        Settings(rate_limits={"other": RateLimit(1, 1)})


def test_settings_frozen() -> None:
    s = Settings()
    with pytest.raises(Exception):
        s.stream_node = "x"  # type: ignore[misc]


def test_get_settings_reads_os_environ_and_caches(monkeypatch) -> None:
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("STREAM_NODE", "from_env_node")
        first = get_settings()
        assert first.stream_node == "from_env_node"
        monkeypatch.setenv("STREAM_NODE", "changed")
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
