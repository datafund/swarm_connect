"""The API docs say the gateway is alpha software."""
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_openapi_title_says_alpha():
    assert app.title == f"{settings.PROJECT_NAME} (alpha)"


def test_docs_page_title_says_alpha():
    response = TestClient(app).get("/docs")
    assert response.status_code == 200
    assert f"<title>{settings.PROJECT_NAME} (alpha) - Swagger UI</title>" in response.text
