"""Security invariant 4 (error half): clients only ever see typed, scrubbed errors."""

from fastapi.testclient import TestClient

from app.core.errors import DEFAULT_MESSAGES, AppError, ErrorCode
from app.main import create_app

SECRET_DETAIL = "internal detail with " + "ATATT" + "q" * 30


def make_client() -> TestClient:
    app = create_app()

    @app.get("/boom-typed")
    async def boom_typed() -> None:
        raise AppError(ErrorCode.INPUT_INVALID, internal_detail=SECRET_DETAIL)

    @app.get("/boom-unexpected")
    async def boom_unexpected() -> None:
        raise RuntimeError(SECRET_DETAIL)

    return TestClient(app, raise_server_exceptions=False)


def test_typed_error_returns_code_and_safe_message_only() -> None:
    response = make_client().get("/boom-typed")
    assert response.status_code == 400
    body = response.json()
    assert body == {
        "error": {
            "code": "INPUT_INVALID",
            "message": DEFAULT_MESSAGES[ErrorCode.INPUT_INVALID],
        }
    }
    assert "ATATT" not in response.text


def test_unexpected_exception_returns_generic_internal_error() -> None:
    response = make_client().get("/boom-unexpected")
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "INTERNAL", "message": DEFAULT_MESSAGES[ErrorCode.INTERNAL]}
    }
    # No stack trace, exception text, or secret leaks to the client.
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text
    assert "ATATT" not in response.text


def test_validation_error_reports_field_names_not_values() -> None:
    app = create_app()

    @app.get("/needs-int")
    async def needs_int(n: int) -> dict[str, int]:
        return {"n": n}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/needs-int", params={"n": "super-secret-value"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INPUT_INVALID"
    assert "super-secret-value" not in response.text


def test_health() -> None:
    client = TestClient(create_app())
    assert client.get("/health").json() == {"status": "ok"}


def test_every_error_code_has_a_default_message() -> None:
    assert set(DEFAULT_MESSAGES) == set(ErrorCode)
