# Flaky Test Analyzer

Detects, ranks, and explains flaky tests from CI run history. A test is "flaky"
when it produces different results without the code changing. This tool reads
test-report history, scores each test's flakiness, ranks them by wasted CI time,
and uses an LLM to suggest a root cause.

## Live Status

- Public app: https://flakytest.vineetkr.com/
- Health check: https://flakytest.vineetkr.com/healthz
- LLM health: https://flakytest.vineetkr.com/llm-health
- Default Vercel URL (may be access-protected): https://flaky-analyzer-igxhqfo4h-vineets-projects-bec904e0.vercel.app/

## What's in here

```
flaky-analyzer/
├── app.py              # Flask web GUI + server (this is the "GUI")
├── flaky_analyzer.py   # detection engine: parse -> store -> score (also a CLI)
├── requirements.txt    # Python dependencies
├── preview.html        # static look-only mockup of the GUI (no backend)
├── .vscode/
│   └── launch.json     # press F5 in VS Code to run the GUI
└── ci-snippet/
    └── flaky-tests.yml # copy this into a target repo's .github/workflows/
```

The GUI is not a standalone file — it is HTML served by `app.py` at runtime.
It only appears in a browser once the server is running.

## Use It As A Public Web App (General Users)

If you are using the hosted app (not running code locally), follow this flow:

1. Open the public URL shared by the maintainer.
2. Click **Load demo data** to verify the dashboard and ranking are working.
3. To analyze a real project, paste a public GitHub repo URL in **Public repo URL**.
4. Keep **Artifact name has** set to `test` unless your workflow uploads artifacts with another name.
5. Click **Fetch and analyze**.

Expected behavior for general users:

- You do **not** need to provide your own GitHub token in the UI.
- The target repo **must upload JUnit/Surefire XML test artifacts** in GitHub Actions; otherwise fetch can complete but no flaky analysis data will appear.
- The strongest flaky signals require repeated runs/reruns; a single run per commit often shows no flakes.
- The **Explain** button is model-agnostic and can use Anthropic, OpenAI, or Ollama.

## Run it in VS Code

1. Open this folder: File -> Open Folder -> `flaky-analyzer`.
2. Install the Microsoft **Python** extension if you don't have it.
3. Open a terminal (Ctrl+`) and create a virtual environment:
   - macOS/Linux: `python3 -m venv venv && source venv/bin/activate`
   - Windows (PowerShell): `python -m venv venv` then `venv\Scripts\Activate.ps1`
     Accept VS Code's prompt to use the new environment.
4. Install dependencies: `pip install -r requirements.txt`
5. Start it: `./.venv/bin/python app.py` (or just press **F5**). The app now runs without Flask's reloader for a faster, steadier local render.
6. Open the printed URL: http://127.0.0.1:5000 (stop with Ctrl+C).

First run: skip all config and click **Load demo data** to see the dashboard
populate with sample flaky tests.

## The AI "Explain" step

Core flaky detection works without any LLM. The Explain button supports multiple providers.

Set one provider before running (or in `.vscode/launch.json`):

- Anthropic: `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...`
- OpenAI: `LLM_PROVIDER=openai` and `OPENAI_API_KEY=...`
- Ollama (local): `LLM_PROVIDER=ollama` and optional `OLLAMA_BASE_URL` (default `http://localhost:11434`)
- If using OpenAI provider, install package once: `pip install openai`

Optional for all providers:

- `LLM_MODEL` to override model name (for example `claude-sonnet-4-5`, `gpt-4o-mini`, `llama3.2`)
- `GET /llm-health` to verify provider and env configuration in a deployed app

Everything else (fetch, scoring, demo data) works without LLM configuration.

## Analyzing a real repo

- **Public repo:** paste its URL; no user token required. NOTE: GitHub requires
  authentication to _download_ artifacts even for public repos, and anonymous
  access is capped at 60 requests/hour. So set a `GITHUB_TOKEN` on the server
  (the service's own token / GitHub App) for this to actually fetch contents.
- **Private repo:** don't paste a personal token into a hosted version. Instead
  add `ci-snippet/flaky-tests.yml` to the repo.

### If You See No Data After Fetch

Check these in the target repo/workflow:

1. CI uploads JUnit/Surefire-style XML artifacts.
2. Artifact names match the filter value used in the UI.
3. There are multiple runs/reruns so flaky signals can be observed.

## Hosting speed

If you want the app to feel fast on a free tier, the tradeoff is usually cold-start lag:

- **Fastest free option for a static demo:** Cloudflare Pages, but it only fits a static UI.
- **Best simple host for this Flask app:** Render, using `gunicorn` and the `PORT` env var.
- **Fastest real backend experience:** a small paid instance; free tiers typically sleep.

Render setup:

1. Install dependencies from `requirements.txt`.
2. Start command: `gunicorn app:app`
3. Make sure `ANTHROPIC_API_KEY` and optional `GITHUB_TOKEN` are set in the host env.
4. The app already listens on `0.0.0.0:$PORT` when deployed.

If you are only demoing the UI, use the static `preview.html` or host a static front-end on a CDN. If you need the live analyzer, keep the Flask backend and accept that free hosting may sleep between requests.

## Deploy On Vercel (from GitHub)

This repo includes `vercel.json` so Vercel can run the Flask app directly.

1. Push this branch to your GitHub repository.
2. In Vercel, click **Add New -> Project** and import the repo.
3. Keep defaults; Vercel will detect Python from `requirements.txt`.
4. Add environment variables in Vercel Project Settings -> Environment Variables:

- `FLASK_SECRET_KEY` (required for production sessions/flash messages)
- `GITHUB_TOKEN` (recommended, avoids GitHub API rate limits and artifact download failures)
- `ANTHROPIC_API_KEY` (optional, required only for the Explain button)

5. Deploy.

### Public access note

- If your `*.vercel.app` URL returns **401**, Vercel deployment protection is enabled.
- Your custom domain can still be public and working at the same time.
- For public demos, share the custom domain URL (or disable protection for the default deployment URL).

### Public Access Checklist

Use this quick checklist after each deploy:

1. Open your custom domain root URL and confirm the app page loads.
2. Open `/healthz` on the same domain and confirm it returns `{"status":"ok"}`.
3. Open `/llm-health` and confirm provider status is `ok` or expected `misconfigured` (if no LLM key is set yet).
4. Click **Load demo data** and verify rows appear in the dashboard.
5. Test one known public GitHub repo URL with **Fetch and analyze**.
6. If no rows appear, confirm the target repo uploads JUnit/Surefire XML artifacts.
7. If using Explain, confirm `LLM_PROVIDER` and matching credentials are configured.

### Important Vercel notes

- On Vercel, SQLite is configured to `/tmp/flaky.db` automatically.
- `/tmp` is ephemeral in serverless environments, so dashboard data is not guaranteed to persist across cold starts/redeploys.
- If you need durable storage, move `results` to a managed DB (for example Postgres/Supabase/Neon) instead of SQLite.

## The most important caveat

A pipeline that runs each test **once per commit produces no flake data** —
you cannot tell a flake from a real regression when the code also changed. Real
detection needs tests to **rerun or repeat**. That is exactly what the CI
snippet does: `-Dsurefire.rerunFailingTestsCount=2` makes intermittent failures
show up as `<flakyFailure>` entries that this tool reads. Add the snippet first;
without it, most repos will show an empty report.

## Tester Playbook (How To Verify It Works)

Use this checklist to validate both analysis logic and UI behavior.

### A) Local logic verification (fast, deterministic)

Run from project root:

```bash
./.venv/bin/python - <<'PY'
import app
from flaky_analyzer import score_tests

app.seed_demo()
rows = score_tests()
ids = [r["test_id"] for r in rows]
print("rows", len(rows))
print("ids", ids)
assert len(rows) == 3
assert "com.api.LoginTest#redirectsAfterLogin" in ids
assert "com.api.PaymentTest#chargesCard" in ids
assert "com.api.CartTest#concurrentAdd" in ids
assert "com.api.HealthTest#pingReturns200" not in ids
print("PASS: scoring logic looks correct")
PY
```

Expected:

- exactly 3 flaky tests detected from demo data
- stable health test is excluded

### B) Local web flow verification

```bash
./.venv/bin/python - <<'PY'
from app import app

with app.test_client() as c:
  assert c.get("/").status_code == 200
  seeded = c.post("/seed", follow_redirects=True)
  assert seeded.status_code == 200
  html = seeded.get_data(as_text=True)
  assert "com.api.LoginTest#redirectsAfterLogin" in html
  assert c.get("/healthz").json == {"status": "ok"}
print("PASS: routes and dashboard render correctly")
PY
```

### C) Hosted verification (public)

1. Open root URL and confirm page loads.
2. Open `/healthz` and confirm `{\"status\":\"ok\"}`.
3. Click **Load demo data** and confirm table rows appear.
4. Click **Explain** and confirm either a response or a clear missing-key error.

### D) Regression checks before release

1. Run both local checks above.
2. Redeploy and repeat hosted verification.
3. Confirm no 500s in Vercel logs on `GET /` and `GET /healthz`.

## Automated Tests (Pytest + GitHub Actions)

This repository includes automated tests and CI:

- Local command: `./.venv/bin/python -m pytest -q`
- CI workflow: `.github/workflows/tests.yml`

The CI suite runs on every push and pull request and verifies:

1. Flaky scoring logic against deterministic demo data.
2. Core Flask routes (`/`, `/seed`, `/healthz`) and dashboard render behavior.
3. Fetch flow behavior with mocked GitHub responses (success + API rate-limit/auth errors).

## CLI (optional, no GUI)

```
python flaky_analyzer.py ingest --reports target/surefire-reports \
    --commit "$GIT_SHA" --branch "$GIT_BRANCH" --run-id "$CI_RUN_ID"
python flaky_analyzer.py report --top 20
```
