"""Error envelope + the real ``create_app`` WITHOUT its lifespan (so: no MongoDB at all).

Running /chat on an app that has no database attached is itself evidence for hard
constraint 1: if a protected request touched the DB, it would fail here.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.auth.tokens import create_access_token
from backend.errors import AppError, install_exception_handlers
from backend.graph import MockGraph
from backend.main import create_app
from backend.tests.helpers import USER_ID, assert_envelope, bearer, client_for, make_settings

pytestmark = pytest.mark.unit

SETTINGS = make_settings()


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str
    n: int


def envelope_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/app-error")
    async def app_error():
        raise AppError(418, "teapot", "Short and stout.", headers={"X-Extra": "1"})

    @app.get("/app-error-401")
    async def app_error_401():
        raise AppError(401, "invalid_token", "nope")

    @app.get("/http-error")
    async def http_error():
        raise HTTPException(status_code=404, detail="Thing not found")

    @app.get("/http-error-dict")
    async def http_error_dict():
        raise HTTPException(status_code=429, detail={"secret": "x"})

    @app.get("/crash")
    async def crash():
        raise RuntimeError("db password is hunter2")

    @app.post("/validate")
    async def validate(body: Body):
        return {"ok": True}

    return app


async def test_app_error_envelope_and_headers() -> None:
    async with client_for(envelope_app()) as c:
        r = await c.get("/app-error")
    assert_envelope(r, 418, "teapot")
    assert r.json()["error"]["message"] == "Short and stout."
    assert r.headers["x-extra"] == "1"


async def test_every_401_gets_www_authenticate() -> None:
    async with client_for(envelope_app()) as c:
        assert_envelope(await c.get("/app-error-401"), 401, "invalid_token")


async def test_http_exception_mapped() -> None:
    async with client_for(envelope_app()) as c:
        r = await c.get("/http-error")
        assert_envelope(r, 404, "not_found")
        assert r.json()["error"]["message"] == "Thing not found"
        r = await c.get("/http-error-dict")  # non-string detail is never echoed
        assert r.status_code == 429
        assert set(r.json()["error"]) == {"code", "message", "details"}
        assert r.json()["error"]["code"] == "rate_limited" and "secret" not in r.text


async def test_unhandled_exception_is_generic_500(caplog) -> None:
    async with client_for(envelope_app()) as c:
        r = await c.get("/crash")
    assert_envelope(r, 500, "internal_error")
    assert "hunter2" not in r.text and "Traceback" not in r.text and "RuntimeError" not in r.text
    assert "hunter2" in caplog.text  # logged server-side with the traceback


async def test_validation_details_never_echo_input() -> None:
    secret = "hunter2-secret-pw"
    async with client_for(envelope_app()) as c:
        r = await c.post("/validate", json={"password": secret, "n": "not-int-" + secret, "extra": secret})
    err = assert_envelope(r, 422, "validation_error")
    assert secret not in r.text
    locs = {tuple(d["loc"]) for d in err["details"]}
    assert ("body", "n") in locs and ("body", "extra") in locs


@pytest.mark.parametrize(
    "content, ctype",
    [(b"{not json", "application/json"), (b"[1,2]", "application/json"), (b'"str"', "application/json"), (b"", "application/json")],
    ids=["malformed", "array", "string", "empty"],
)
async def test_bad_json_bodies_are_validation_errors(content: bytes, ctype: str) -> None:
    async with client_for(envelope_app()) as c:
        r = await c.post("/validate", content=content, headers={"content-type": ctype})
    assert_envelope(r, 422, "validation_error")


# ------------------------------------------------------------------ real app, no lifespan


def bare_app(**settings_over) -> FastAPI:
    app = create_app(make_settings(**settings_over))
    app.state.graph = MockGraph()  # what the lifespan would set; there is NO mongo/db
    return app


async def test_chat_works_with_no_database_attached() -> None:
    app = bare_app()
    assert not hasattr(app.state, "db") and not hasattr(app.state, "mongo_client")
    async with client_for(app) as c:
        r = await c.post(
            "/chat", json={"message": "hello", "thread_id": "t1"}, headers=bearer(create_access_token(USER_ID, SETTINGS))
        )
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"response", "thread_id"} and r.json()["thread_id"] == "t1"


async def test_chat_unauthenticated_on_bare_app() -> None:
    async with client_for(bare_app()) as c:
        assert_envelope(await c.post("/chat", json={"message": "hi"}), 401, "not_authenticated")


async def test_404_and_405_envelopes() -> None:
    async with client_for(bare_app()) as c:
        assert_envelope(await c.get("/nope"), 404, "not_found")
        r = await c.get("/chat")
        assert_envelope(r, 405, "method_not_allowed")
        assert "POST" in r.headers["allow"]


async def test_cors_preflight_allowed_origin() -> None:
    async with client_for(bare_app()) as c:
        r = await c.options(
            "/chat",
            headers={
                "Origin": "http://localhost:8501",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:8501"
    allowed = r.headers["access-control-allow-methods"]
    assert "POST" in allowed and "GET" in allowed
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in r.headers


async def test_cors_disallowed_origin_gets_no_allow_header() -> None:
    async with client_for(bare_app()) as c:
        r = await c.options(
            "/chat", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
        )
    assert "access-control-allow-origin" not in r.headers


async def test_cors_origins_from_settings_and_retry_after_exposed() -> None:
    app = bare_app(cors_origins=("https://app.example",), limits={"chat": "1/60"})
    tok = create_access_token(USER_ID, SETTINGS)
    async with client_for(app) as c:
        h = {**bearer(tok), "Origin": "https://app.example"}
        r1 = await c.post("/chat", json={"message": "a"}, headers=h)
        r2 = await c.post("/chat", json={"message": "b"}, headers=h)
        default_origin = await c.options(
            "/chat", headers={"Origin": "http://localhost:8501", "Access-Control-Request-Method": "POST"}
        )
    assert r1.status_code == 200 and r1.headers["access-control-allow-origin"] == "https://app.example"
    assert_envelope(r2, 429, "rate_limited")
    assert "retry-after" in r2.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-origin" not in default_origin.headers


def test_module_level_app_is_lazy(monkeypatch) -> None:
    import backend.main as main
    from backend.config import get_settings

    # Importing backend.main must not need JWT_SECRET; accessing .app builds from env.
    monkeypatch.setenv("JWT_SECRET", "z" * 40)
    get_settings.cache_clear()
    main.__dict__.pop("app", None)
    try:
        app = main.app
        assert isinstance(app, FastAPI) and main.app is app
        with pytest.raises(AttributeError):
            main.does_not_exist  # noqa: B018
    finally:
        main.__dict__.pop("app", None)
        get_settings.cache_clear()


# ------------------------------------------------------------------ UnhandledErrorMiddleware (pure ASGI)


async def _drive(inner):
    from backend.errors import UnhandledErrorMiddleware

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}
    return UnhandledErrorMiddleware(inner), scope, receive, send, sent


async def test_unhandled_before_start_becomes_500_envelope() -> None:
    import json

    async def inner(scope, receive, send):
        raise RuntimeError("secret detail")

    mw, scope, receive, send, sent = await _drive(inner)
    await mw(scope, receive, send)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 500
    body = json.loads(b"".join(m.get("body", b"") for m in sent[1:]))
    assert body == {"error": {"code": "internal_error", "message": "An internal error occurred.", "details": None}}


async def test_unhandled_after_start_is_reraised_without_second_response() -> None:
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("mid-stream crash")

    mw, scope, receive, send, sent = await _drive(inner)
    with pytest.raises(RuntimeError, match="mid-stream crash"):
        await mw(scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sum(m["type"] == "http.response.start" for m in sent) == 1


async def test_non_http_scopes_pass_through() -> None:
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw, _, receive, send, _ = await _drive(inner)
    await mw({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


async def test_500_from_real_app_has_cors_header() -> None:
    from backend.dependencies import get_graph

    app = bare_app()

    def broken():
        raise RuntimeError("x")

    app.dependency_overrides[get_graph] = broken
    async with client_for(app) as c:
        r = await c.post(
            "/chat",
            json={"message": "hi"},
            headers={**bearer(create_access_token(USER_ID, SETTINGS)), "Origin": "http://localhost:3000"},
        )
    assert_envelope(r, 500, "internal_error")
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_configure_logging_is_idempotent() -> None:
    import logging

    from backend.main import configure_logging

    configure_logging("INFO")
    configure_logging("WARNING")
    configure_logging("DEBUG")
    lg = logging.getLogger("backend")
    ours = [h for h in lg.handlers if getattr(h, "_wanderchat_handler", False)]
    assert len(ours) == 1 and lg.level == logging.DEBUG and lg.propagate
    configure_logging("INFO")
    assert lg.level == logging.INFO
