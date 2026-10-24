
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_schema_is_available():
    response = client.get("/openapi.json")
    assert response.status_code == 200

    schema = response.json()
    assert "paths" in schema
    assert "/health" in schema["paths"]


def test_docs_endpoint_is_available():
    response = client.get("/docs")
    assert response.status_code == 200


def test_employees_endpoint_exists():
    response = client.get("/employees?page=1&page_size=10")

    # 200 means the endpoint returned data successfully.
    # 503 may indicate that MongoDB is unavailable.
    assert response.status_code in (200, 503)

    if response.status_code == 200:
        data = response.json()
        assert isinstance(data, dict)
        assert "items" in data


def test_attendance_endpoint_exists():
    response = client.get("/attendance?page=1&page_size=10")

    assert response.status_code in (200, 503)

    if response.status_code == 200:
        assert isinstance(response.json(), dict)
