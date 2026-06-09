import requests

import app as webapp
import flaky_analyzer


def test_fetch_route_success_flash_with_mocked_fetch(tmp_path, monkeypatch):
    db_path = tmp_path / "test_fetch_success.db"
    flaky_analyzer.DB_PATH = str(db_path)

    monkeypatch.setattr(webapp, "fetch_repo", lambda *args, **kwargs: (4, 2, 12, 0))

    with webapp.app.test_client() as client:
        resp = client.post(
            "/fetch",
            data={
                "repo": "http://github.com/contactkrvineet/python-bdd-automation-framework",
                "branch": "main",
                "filter": "test",
                "max_runs": "15",
            },
            follow_redirects=True,
        )

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Ingested 12 test results from 2 artifacts across 4 runs." in html


def test_fetch_route_handles_github_rate_limit_error(tmp_path, monkeypatch):
    db_path = tmp_path / "test_fetch_error.db"
    flaky_analyzer.DB_PATH = str(db_path)

    response = requests.Response()
    response.status_code = 403

    def _raise_http_error(*args, **kwargs):
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(webapp, "fetch_repo", _raise_http_error)

    with webapp.app.test_client() as client:
        resp = client.post(
            "/fetch",
            data={
                "repo": "octocat/Hello-World",
                "branch": "main",
                "filter": "test",
                "max_runs": "15",
            },
            follow_redirects=True,
        )

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "GitHub API 403: authentication or rate limit." in html
