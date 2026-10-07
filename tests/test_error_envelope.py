"""One error envelope (#381): `detail` unchanged, `code` and `message` on top."""
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app, error_envelope

client = TestClient(app)


def test_dict_detail_with_code_is_mirrored():
    detail = {"code": "FILE_TOO_LARGE", "message": "too big", "extra": 1}
    assert error_envelope(413, detail) == {"detail": detail, "code": "FILE_TOO_LARGE", "message": "too big"}


def test_string_detail_gets_status_code():
    assert error_envelope(404, "Stamp not found") == {
        "detail": "Stamp not found", "code": "HTTP_404", "message": "Stamp not found"}


def test_x402_body_is_kept_under_detail():
    body = {"x402Version": 1, "error": "Payment required", "accepts": [{"scheme": "exact"}]}
    env = error_envelope(402, body)
    assert env["detail"] is body
    assert env["code"] == "HTTP_402" and env["message"] == "Payment required"


def test_nested_detail_string_and_missing_message():
    assert error_envelope(503, {"error": None, "detail": "try later"})["message"] == "try later"
    assert error_envelope(502, None)["message"] == "Bad Gateway"
    assert error_envelope(400, {"code": None})["code"] == "HTTP_400"


def test_unknown_route_uses_envelope():
    r = client.get("/api/v1/no-such-route")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found", "code": "HTTP_404", "message": "Not Found"}


def test_validation_error_keeps_detail_list():
    r = client.get("/api/v1/pricing?depth=99")
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert isinstance(body["detail"], list) and body["detail"][0]["loc"] == ["query", "depth"]


def test_raised_http_exception_headers_are_kept():
    from fastapi import FastAPI
    from app.main import http_exception_handler
    from starlette.exceptions import HTTPException as StarletteHTTPException

    mini = FastAPI()
    mini.add_exception_handler(StarletteHTTPException, http_exception_handler)

    @mini.get("/x")
    async def x():
        raise HTTPException(status_code=429, detail={"code": "FREE_QUOTA_EXCEEDED", "message": "m"},
                            headers={"Retry-After": "5"})

    r = TestClient(mini).get("/x")
    assert r.status_code == 429 and r.headers["Retry-After"] == "5"
    assert r.json()["code"] == "FREE_QUOTA_EXCEEDED"
    assert r.json()["detail"] == {"code": "FREE_QUOTA_EXCEEDED", "message": "m"}
