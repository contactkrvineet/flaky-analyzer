# Flaky Test Analyzer

Detects, ranks, and explains flaky tests from CI run history. A test is "flaky"
when it produces different results without the code changing. This tool reads
test-report history, scores each test's flakiness, ranks them by wasted CI time,
and uses an LLM to suggest a root cause.

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

The Explain button needs an Anthropic API key on the server. Set it before
running (or fill it into `.vscode/launch.json`):

- macOS/Linux: `export ANTHROPIC_API_KEY=sk-...`
- Windows (PowerShell): `$env:ANTHROPIC_API_KEY="sk-..."`

Everything else (fetch, scoring, demo data) works without it.

## Analyzing a real repo

- **Public repo:** paste its URL; no user token required. NOTE: GitHub requires
  authentication to _download_ artifacts even for public repos, and anonymous
  access is capped at 60 requests/hour. So set a `GITHUB_TOKEN` on the server
  (the service's own token / GitHub App) for this to actually fetch contents.
- **Private repo:** don't paste a personal token into a hosted version. Instead
  add `ci-snippet/flaky-tests.yml` to the repo.

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

## The most important caveat

A pipeline that runs each test **once per commit produces no flake data** —
you cannot tell a flake from a real regression when the code also changed. Real
detection needs tests to **rerun or repeat**. That is exactly what the CI
snippet does: `-Dsurefire.rerunFailingTestsCount=2` makes intermittent failures
show up as `<flakyFailure>` entries that this tool reads. Add the snippet first;
without it, most repos will show an empty report.

## CLI (optional, no GUI)

```
python flaky_analyzer.py ingest --reports target/surefire-reports \
    --commit "$GIT_SHA" --branch "$GIT_BRANCH" --run-id "$CI_RUN_ID"
python flaky_analyzer.py report --top 20
```
