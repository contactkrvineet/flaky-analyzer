import flaky_analyzer
from app import app as flask_app


def test_core_routes_work_with_demo_data(tmp_path):
    db_path = tmp_path / "test_routes.db"
    flaky_analyzer.DB_PATH = str(db_path)

    with flask_app.test_client() as client:
        index_resp = client.get("/")
        assert index_resp.status_code == 200

        seed_resp = client.post("/seed", follow_redirects=True)
        assert seed_resp.status_code == 200
        html = seed_resp.get_data(as_text=True)
        assert "com.api.LoginTest#redirectsAfterLogin" in html
        assert "Detected Flaky Tests" in html

        health_resp = client.get("/healthz")
        assert health_resp.status_code == 200
        assert health_resp.get_json() == {"status": "ok"}

        llm_health_resp = client.get("/llm-health")
        assert llm_health_resp.status_code == 200
        llm_payload = llm_health_resp.get_json()
        assert llm_payload["provider"] in {"anthropic", "openai", "ollama"}
        assert "status" in llm_payload
