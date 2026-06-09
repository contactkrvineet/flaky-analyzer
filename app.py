"""
app.py — local web GUI for the flaky test analyzer.

Instead of running the CLI, you enter a GitHub repo (owner/repo) and a token,
and this app pulls recent GitHub Actions runs, downloads the test-report
artifacts each run uploaded, parses them, and shows a ranked flaky dashboard.

Why a token and not just the repo: flakiness is computed from CI *run history*
(which test passed/failed over time), which lives in GitHub Actions — not in
the git tree. The commit SHA is pulled along as a label on each run.

Setup:
    pip install flask requests
    # keep this file next to flaky_analyzer.py
    python app.py
    # open http://127.0.0.1:5000

Token: a fine-grained or classic PAT with read access to Actions on the repo
(scope: `actions:read`, plus `repo` for private repos). It is used only to call
api.github.com from your machine and is never stored or logged.

Note on data richness:
  - One run per commit only gives the "flip over time" signal.
  - The strong "same-commit divergence" signal needs >1 run on the SAME sha,
    i.e. re-runs or scheduled/nightly runs of the same suite.
  - GitHub artifacts expire (default 90 days), so "fetch all" is bounded by
    your repo's artifact retention.
"""

import io
import os
import time
import zipfile
import xml.etree.ElementTree as ET

import requests
from flask import Flask, request, render_template_string, redirect, url_for, flash

if os.environ.get("VERCEL"):
    # Force a writable SQLite path in Vercel serverless runtime.
    os.environ["FLAKY_DB"] = "/tmp/flaky.db"

# Reuse the engine you already have.
from flaky_analyzer import (connect, score_tests, _outcome, _message,
                            explain_test_messages)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-dev-only")

GH = "https://api.github.com"
_DASHBOARD_CACHE = {"ts": 0.0, "rows": []}
_DASHBOARD_CACHE_TTL_S = 3


def llm_config_status():
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    model = os.environ.get("LLM_MODEL", "")

    if provider == "anthropic":
        configured = bool(os.environ.get("ANTHROPIC_API_KEY"))
        required = ["ANTHROPIC_API_KEY"]
        default_model = "claude-sonnet-4-5"
    elif provider == "openai":
        configured = bool(os.environ.get("OPENAI_API_KEY"))
        required = ["OPENAI_API_KEY"]
        default_model = "gpt-4o-mini"
    elif provider == "ollama":
        configured = True
        required = []
        default_model = os.environ.get("OLLAMA_MODEL", "llama3.2")
    else:
        return {
            "status": "error",
            "provider": provider,
            "message": "Unsupported LLM_PROVIDER. Use anthropic, openai, or ollama.",
            "supported_providers": ["anthropic", "openai", "ollama"],
        }

    return {
        "status": "ok" if configured else "misconfigured",
        "provider": provider,
        "configured": configured,
        "required_env": required,
        "model": model or default_model,
    }


# --------------------------------------------------------------------------- #
# GitHub Actions fetch
# --------------------------------------------------------------------------- #
def gh_headers(token):
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "flaky-analyzer",
    }
    if token:  # anonymous when empty (public metadata only, 60 req/hr/IP)
        h["Authorization"] = f"Bearer {token}"
    return h


def parse_repo(s):
    """Accept 'owner/repo' or a full github.com URL; return (owner, repo)."""
    s = s.strip().removesuffix(".git")
    if "github.com/" in s:
        s = s.split("github.com/", 1)[1]
    parts = s.strip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None, None
    return parts[0], parts[1]


def list_runs(owner, repo, token, branch, max_runs):
    """Return completed workflow runs (id, head_sha, created_at, name)."""
    runs, page = [], 1
    while len(runs) < max_runs:
        params = {"per_page": 100, "page": page, "status": "completed"}
        if branch:
            params["branch"] = branch
        r = requests.get(f"{GH}/repos/{owner}/{repo}/actions/runs",
                         headers=gh_headers(token), params=params, timeout=30)
        r.raise_for_status()
        batch = r.json().get("workflow_runs", [])
        if not batch:
            break
        runs.extend(batch)
        page += 1
    return runs[:max_runs]


def list_artifacts(owner, repo, token, run_id):
    r = requests.get(f"{GH}/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
                     headers=gh_headers(token), timeout=30)
    r.raise_for_status()
    return r.json().get("artifacts", [])


def download_artifact_xml(owner, repo, token, artifact_id):
    """Download an artifact zip and yield (filename, xml_root) for each XML."""
    url = f"{GH}/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip"
    # requests follows the 302 to the signed URL and strips auth cross-host.
    r = requests.get(url, headers=gh_headers(token), timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for name in z.namelist():
            if name.lower().endswith(".xml"):
                try:
                    yield name, ET.fromstring(z.read(name))
                except ET.ParseError:
                    continue


def ingest_root(conn, root, commit_sha, branch, run_id, ts):
    """Insert all <testcase> rows from one parsed XML report root."""
    rows = []
    for tc in root.iter("testcase"):
        cls = tc.get("classname") or tc.get("class") or ""
        name = tc.get("name") or ""
        test_id = f"{cls}#{name}" if cls else name
        try:
            duration = float(tc.get("time") or 0.0)
        except ValueError:
            duration = 0.0
        rows.append((str(run_id), commit_sha, branch, ts, test_id,
                     _outcome(tc), duration, _message(tc)))
    if rows:
        conn.executemany(
            "INSERT INTO results (run_id, commit_sha, branch, ts, test_id, "
            "outcome, duration, message) VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def fetch_repo(owner, repo, token, branch, max_runs, name_filter):
    conn = connect()
    runs = list_runs(owner, repo, token, branch, max_runs)
    n_runs = n_cases = n_arts = dl_errors = 0
    for run in runs:
        run_id = run["id"]
        sha = run.get("head_sha", "")
        ts = run.get("created_at", "")
        rbranch = run.get("head_branch", branch or "unknown")
        arts = [a for a in list_artifacts(owner, repo, token, run_id)
                if not a.get("expired") and name_filter.lower() in a["name"].lower()]
        if not arts:
            continue
        # re-fetch is idempotent: clear any prior rows for this run first
        conn.execute("DELETE FROM results WHERE run_id=?", (str(run_id),))
        for a in arts:
            try:
                for _, root in download_artifact_xml(owner, repo, token, a["id"]):
                    n_cases += ingest_root(conn, root, sha, rbranch, run_id, ts)
                n_arts += 1
            except requests.HTTPError:
                # downloading artifact contents needs auth even for public repos
                dl_errors += 1
        n_runs += 1
    conn.commit()
    conn.close()
    return n_runs, n_arts, n_cases, dl_errors


# --------------------------------------------------------------------------- #
# Demo data — seeds realistic flaky tests so the UI + AI explain are demoable
# without depending on a live repo or the GitHub API during a presentation.
# --------------------------------------------------------------------------- #
def seed_demo():
    conn = connect()
    conn.execute("DELETE FROM results WHERE run_id LIKE 'demo-%'")
    TIMEOUT = ("org.openqa.selenium.TimeoutException: Expected condition failed: "
               "waiting for element to be clickable (tried for 10 seconds) "
               "at LoginPage.submit(LoginPage.java:42)")
    CONN = ("java.net.ConnectException: Connection refused: connect "
            "at com.api.ApiClient.get(ApiClient.java:88)")
    RACE = ("java.util.ConcurrentModificationException "
            "at java.util.ArrayList$Itr.checkForComodification(ArrayList.java:1013)")
    rows = [
        # A: rerun flake (timing) — definitive flake signal
        ("demo-1", "a1b2c3", "main", "2026-05-01T01:00:00", "com.api.LoginTest#redirectsAfterLogin", "flake", 2.1, TIMEOUT),
        ("demo-2", "d4e5f6", "main", "2026-05-02T01:00:00", "com.api.LoginTest#redirectsAfterLogin", "pass", 1.8, ""),
        ("demo-3", "g7h8i9", "main", "2026-05-03T01:00:00", "com.api.LoginTest#redirectsAfterLogin", "flake", 2.4, TIMEOUT),
        # B: same-commit divergence (external dependency) — two runs on sha j1k2l3
        ("demo-4", "j1k2l3", "main", "2026-05-04T01:00:00", "com.api.PaymentTest#chargesCard", "pass", 0.9, ""),
        ("demo-4b", "j1k2l3", "main", "2026-05-04T02:00:00", "com.api.PaymentTest#chargesCard", "fail", 0.7, CONN),
        ("demo-5", "m4n5o6", "main", "2026-05-05T01:00:00", "com.api.PaymentTest#chargesCard", "pass", 0.8, ""),
        # C: high flip rate (race condition)
        ("demo-1", "a1b2c3", "main", "2026-05-01T01:00:00", "com.api.CartTest#concurrentAdd", "pass", 0.5, ""),
        ("demo-2", "d4e5f6", "main", "2026-05-02T01:00:00", "com.api.CartTest#concurrentAdd", "fail", 0.6, RACE),
        ("demo-3", "g7h8i9", "main", "2026-05-03T01:00:00", "com.api.CartTest#concurrentAdd", "pass", 0.5, ""),
        ("demo-4", "j1k2l3", "main", "2026-05-04T01:00:00", "com.api.CartTest#concurrentAdd", "fail", 0.6, RACE),
        # D: stable — must NOT appear in the report
        ("demo-1", "a1b2c3", "main", "2026-05-01T01:00:00", "com.api.HealthTest#pingReturns200", "pass", 0.1, ""),
        ("demo-2", "d4e5f6", "main", "2026-05-02T01:00:00", "com.api.HealthTest#pingReturns200", "pass", 0.1, ""),
    ]
    conn.executemany(
        "INSERT INTO results (run_id, commit_sha, branch, ts, test_id, "
        "outcome, duration, message) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    _DASHBOARD_CACHE["ts"] = 0.0
    _DASHBOARD_CACHE["rows"] = []
    return len(rows)


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #
PAGE = """
<!doctype html><meta charset="utf-8"><title>Flaky Test Analyzer</title>
<style>
    :root {
        color-scheme: dark;
        --bg: #06090d;
        --panel: #10161d;
        --panel-elevated: #131b23;
        --ink: #edf3fb;
        --muted: #9aa8ba;
        --brand: #7dd3fc;
        --brand-strong: #3bb5ef;
        --line: #273445;
        --warn-bg: #151515;
        --warn-line: #424242;
        --flash-bg: #12202b;
        --ok: #22c55e;
        --pill-mid: #f59e0b;
        --pill-hot: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
        margin: 0;
        font: 15px/1.6 "Avenir Next", "Futura", "Trebuchet MS", sans-serif;
        color: var(--ink);
        background:
            radial-gradient(circle at 12% -10%, #1a1a1a 0%, transparent 44%),
            radial-gradient(circle at 100% 0%, #111111 0%, transparent 38%),
            var(--bg);
    }
    .wrap {
        max-width: 980px;
        margin: 2rem auto;
        padding: 0 1rem;
        animation: fade-in .45s ease-out;
    }
    .page-top {
        display: flex;
        justify-content: flex-end;
        margin-bottom: .75rem;
    }
    .source-badge {
        display: inline-flex;
        align-items: center;
        gap: .35rem;
        padding: .38rem .72rem;
        border: 1px solid rgba(173, 229, 255, 0.24);
        border-radius: 999px;
        background: rgba(13, 21, 31, 0.78);
        color: #b6e8ff;
        font-size: .78rem;
        text-decoration: none;
        transition: transform .12s ease, border-color .12s ease, background .12s ease;
    }
    .source-badge:hover {
        transform: translateY(-1px);
        border-color: rgba(173, 229, 255, 0.55);
        background: rgba(20, 30, 42, 0.95);
        color: #eaf9ff;
    }
    .source-badge .arrow {
        font-size: .9em;
        opacity: .8;
    }
    .hero {
        background: linear-gradient(145deg, #152232, #0d151f 58%);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 1.15rem 1.2rem;
        box-shadow: 0 10px 30px rgba(0, 0, 0, .42);
    }
    h1 {
        margin: 0;
        font-size: 1.75rem;
        letter-spacing: .02em;
    }
    p.sub {
        color: var(--muted);
        margin: .35rem 0 0;
        max-width: 76ch;
    }
    .credit {
        margin-top: .55rem;
        font-size: .86rem;
        color: var(--ok);
    }
    .metrics {
        margin-top: .9rem;
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: .7rem;
    }
    .metric {
        border: 1px solid var(--line);
        border-radius: 10px;
        background: rgba(13, 21, 31, 0.7);
        padding: .68rem .78rem;
    }
    .metric-label {
        color: var(--muted);
        font-size: .76rem;
        text-transform: uppercase;
        letter-spacing: .06em;
    }
    .metric-value {
        margin-top: .2rem;
        font-size: 1.05rem;
        font-weight: 800;
        font-variant-numeric: tabular-nums;
    }
    .card {
        border: 1px solid var(--line);
        background: var(--panel);
        border-radius: 12px;
        margin: 1rem 0;
        padding: 1rem 1.1rem;
    }
    .disclaimer {
        background: var(--warn-bg);
        border-color: var(--warn-line);
    }
    .muted { color: var(--muted); }
    form {
        display: grid;
        grid-template-columns: 170px 1fr;
        gap: .65rem .9rem;
        align-items: center;
    }
    label {
        font-weight: 700;
        font-size: .94rem;
    }
    input {
        padding: .56rem .65rem;
        border: 1px solid #314156;
        border-radius: 8px;
        font: inherit;
        width: 100%;
        background: #0d151e;
        color: var(--ink);
    }
    input:focus {
        outline: 2px solid rgba(61, 171, 227, 0.35);
        border-color: var(--brand-strong);
    }
    button {
        grid-column: 2;
        justify-self: start;
        padding: .56rem 1.15rem;
        border: 0;
        border-radius: 8px;
        background: linear-gradient(180deg, var(--brand), #5dc4f3);
        color: #062536;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
        transition: transform .12s ease, background-color .12s ease;
    }
    button:hover { background: var(--brand-strong); transform: translateY(-1px); }
    .table-wrap {
        margin-top: 1rem;
        border: 1px solid var(--line);
        border-radius: 12px;
        overflow: hidden;
        background: #0d141d;
    }
    table {
        border-collapse: collapse;
        width: 100%;
        background: transparent;
    }
    th, td {
        text-align: left;
        padding: .62rem .72rem;
        border-bottom: 1px solid rgba(41, 61, 84, 0.58);
    }
    th {
        font-size: .78rem;
        text-transform: uppercase;
        letter-spacing: .06em;
        color: #b8c8da;
        background: #172433;
        position: sticky;
        top: 0;
        z-index: 1;
    }
    tbody tr.data-row {
        transition: background-color .2s ease;
    }
    tbody tr.data-row:nth-child(4n + 1),
    tbody tr.data-row:nth-child(4n + 2) {
        background: rgba(12, 21, 31, 0.75);
    }
    tbody tr.data-row:hover {
        background: rgba(37, 64, 92, 0.35);
    }
    td.num { text-align: right; font-variant-numeric: tabular-nums; }
    .score { font-weight: 800; }
    .score-pill {
        display: inline-flex;
        min-width: 3.1rem;
        justify-content: center;
        padding: .18rem .42rem;
        border-radius: 999px;
        font-size: .78rem;
        font-weight: 800;
        background: rgba(34, 197, 94, 0.16);
        border: 1px solid rgba(34, 197, 94, 0.48);
        color: #8ef0b0;
    }
    .score-pill.mid {
        background: rgba(245, 158, 11, 0.16);
        border-color: rgba(245, 158, 11, 0.52);
        color: #ffcf7a;
    }
    .score-pill.hot {
        background: rgba(239, 68, 68, 0.16);
        border-color: rgba(239, 68, 68, 0.5);
        color: #ff9d9d;
    }
    .flash {
        padding: .72rem 1rem;
        border: 1px solid #2f4865;
        border-radius: 10px;
        background: var(--flash-bg);
        margin: 1rem 0;
    }
    .test { font-family: Menlo, Monaco, Consolas, "Liberation Mono", monospace; font-size: .84rem; }
    .hint { color: var(--muted); font-size: .85rem; }
    .exp-btn {
        padding: .36rem .72rem;
        font-size: .78rem;
        background: rgba(61, 171, 227, 0.11);
        border: 1px solid rgba(84, 184, 235, 0.56);
        color: #ade5ff;
        border-radius: 999px;
        cursor: pointer;
        transition: transform .12s ease, border-color .12s ease, background .12s ease;
    }
    .exp-btn:hover {
        transform: translateY(-1px);
        border-color: #7fd3ff;
        background: rgba(61, 171, 227, 0.2);
    }
    .exp-btn:disabled {
        opacity: .6;
        cursor: wait;
    }
    .exp-row {
        display: none;
    }
    .exp-row.open {
        display: table-row;
        animation: reveal .25s ease;
    }
    .exp-cell {
        padding: 0;
        border-bottom: 1px solid rgba(41, 61, 84, 0.58);
    }
    .exp-panel {
        margin: .52rem;
        padding: .68rem .78rem;
        border: 1px solid #2f4865;
        border-radius: 10px;
        background: linear-gradient(165deg, #101a25, #0c141f);
    }
    .exp-meta {
        margin-bottom: .42rem;
        font-size: .74rem;
        letter-spacing: .05em;
        text-transform: uppercase;
        color: #7fb9da;
    }
    .exp-body {
        white-space: pre-wrap;
        font-family: Menlo, Monaco, Consolas, "Liberation Mono", monospace;
        font-size: .82rem;
        line-height: 1.48;
        color: #dbe9f7;
    }
    .demo {
        background: #0f1822;
        border: 1px dashed #406281;
        color: #c5e8ff;
        padding: .42rem .92rem;
        border-radius: 8px;
        cursor: pointer;
        font: inherit;
    }
    .footer {
        margin: 1rem 0 0;
        padding: .65rem 1rem 0;
        border-top: 1px solid rgba(41, 61, 84, 0.58);
        color: var(--muted);
        font-size: .82rem;
        text-align: center;
    }
    .footer a {
        color: #ade5ff;
        text-decoration: none;
        border-bottom: 1px solid rgba(173, 229, 255, 0.22);
    }
    .footer a:hover {
        color: #e8f8ff;
        border-bottom-color: rgba(232, 248, 255, 0.85);
    }
    .footer .arrow {
        font-size: .9em;
        opacity: .8;
        margin-left: .15rem;
    }
    @keyframes fade-in {
        from { opacity: 0; transform: translateY(6px); }
        to { opacity: 1; transform: translateY(0); }
    }
    @keyframes reveal {
        from { opacity: 0; transform: translateY(-4px); }
        to { opacity: 1; transform: translateY(0); }
    }
    @media (max-width: 760px) {
        .wrap { margin: 1rem auto; }
        h1 { font-size: 1.42rem; }
        .metrics { grid-template-columns: 1fr; }
        form { grid-template-columns: 1fr; }
        button { grid-column: 1; width: 100%; }
    }
</style>

<div class="wrap">
<div class="page-top">
    <a class="source-badge" href="https://github.com/contactkrvineet/flaky-analyzer" target="_blank" rel="noreferrer">Source code <span class="arrow">↗</span></a>
</div>
<section class="hero">
    <h1>Flaky Test Analyzer</h1>
    <p class="sub">Analyze flaky test behavior from GitHub Actions artifacts and prioritize tests by wasted CI time and instability signals.</p>
    <div class="credit">Developed by Vineet Kumar</div>
    <div class="metrics">
        <div class="metric">
            <div class="metric-label">Detected Flaky Tests</div>
            <div class="metric-value">{{ flaky_count }}</div>
        </div>
        <div class="metric">
            <div class="metric-label">Total Wasted CI Time (s)</div>
            <div class="metric-value">{{ "%.1f"|format(total_wasted_s) }}</div>
        </div>
        <div class="metric">
            <div class="metric-label">Highest Flake Score</div>
            <div class="metric-value">{{ "%.2f"|format(top_score) }}</div>
        </div>
    </div>
</section>

{% for msg in get_flashed_messages() %}<div class="flash">{{ msg }}</div>{% endfor %}

<section class="card disclaimer">
    <strong>Demo disclaimer</strong>
    <div class="muted">This web UI currently supports public GitHub repository URLs for quick evaluation and demo flows.</div>
    <div class="muted" style="margin-top:.35rem">For private repositories, use the workflow at <code>ci-snippet/flaky-tests.yml</code> (or copy equivalent steps) in your repo CI pipeline, then run this analyzer against generated reports in your controlled environment.</div>
    <div class="hint" style="margin-top:.35rem">Why this matters: reliable flake detection needs test reruns/repeats; single run per commit usually cannot expose flaky behavior.</div>
</section>

<section class="card">
<div class="hint" style="margin-bottom:.75rem"><strong>Before you fetch:</strong> the target repository must upload JUnit/Surefire XML test artifacts in GitHub Actions (for example via an upload-artifact step). If no matching test artifact exists, the dashboard cannot analyze flakiness.</div>
<form method="post" action="{{ url_for('fetch') }}">
    <label>Public repo URL</label><input name="repo" placeholder="https://github.com/owner/repo" required>
    <label>Branch (optional)</label><input name="branch" placeholder="main">
    <label>Artifact name has</label><input name="filter" value="test" required>
    <label>Max runs to fetch</label><input name="max_runs" type="number" value="15" min="1" max="300">
    <button type="submit">Fetch and analyze</button>
    <span class="hint" style="grid-column:2">Tip: set <code>GITHUB_TOKEN</code> on the server for better API reliability and fewer rate-limit failures.</span>
</form>
</section>

<section class="card">
    <strong>AI Explain setup (model-agnostic)</strong>
    <div class="hint" style="margin-top:.35rem">Flaky analysis works without any LLM. Explain supports <code>LLM_PROVIDER=anthropic|openai|ollama</code>.</div>
    <div class="hint" style="margin-top:.25rem">Examples: <code>LLM_PROVIDER=anthropic</code> + <code>ANTHROPIC_API_KEY</code>, or <code>LLM_PROVIDER=openai</code> + <code>OPENAI_API_KEY</code>, or <code>LLM_PROVIDER=ollama</code>.</div>
    <div class="hint" style="margin-top:.15rem">Optional model override: <code>LLM_MODEL</code> (for example <code>gpt-4o-mini</code> or <code>llama3.2</code>).</div>
</section>

<form method="post" action="{{ url_for('seed') }}" style="margin:-.5rem 0 1rem">
  <button class="demo" type="submit">Load demo data</button>
  <span class="hint">No repo handy? Seed realistic flaky tests to try the dashboard + AI explain.</span>
</form>

{% if rows %}
<div class="table-wrap">
<table>
    <thead>
        <tr><th>Score</th><th class="num">Wasted&nbsp;(s)</th><th class="num">Runs</th>
                <th class="num">Flip</th><th class="num">Reruns</th><th>Test</th><th>Explain</th></tr>
    </thead>
    <tbody>
    {% for r in rows %}
    <tr class="data-row">
        <td class="score num">
            <span class="score-pill{% if r.score >= 0.9 %} hot{% elif r.score >= 0.6 %} mid{% endif %}">{{ "%.2f"|format(r.score) }}</span>
        </td>
        <td class="num">{{ "%.1f"|format(r.wasted_time_s) }}</td>
        <td class="num">{{ r.runs }}</td>
        <td class="num">{{ "%.2f"|format(r.flip_rate) }}</td>
        <td class="num">{{ r.rerun_flakes }}</td>
        <td class="test">{{ r.test_id }}</td>
        <td><button type="button" class="exp-btn" data-test="{{ r.test_id }}"
                                onclick="explain({{ loop.index0 }}, this)">Explain</button></td>
    </tr>
    <tr id="exp-{{ loop.index0 }}" class="exp-row">
        <td colspan="7" class="exp-cell">
            <div class="exp-panel">
                <div class="exp-meta">Root Cause Analysis</div>
                <div class="exp-body"></div>
            </div>
        </td>
    </tr>
    {% endfor %}
    </tbody>
</table>
</div>
{% else %}
<p class="hint">No flaky tests yet. Fetch a repo or load demo data above
(real detection needs more than one run per commit, e.g. reruns).</p>
{% endif %}

<script>
async function explain(idx, btn) {
  const test = btn.dataset.test;
  const row = document.getElementById('exp-' + idx);
    const body = row.querySelector('.exp-body');
    if (row.classList.contains('open')) {
        row.classList.remove('open');
        btn.textContent = 'Explain';
        return;
    }
    row.classList.add('open');
    body.textContent = 'Analyzing root cause...';
    btn.textContent = 'Loading...';
  btn.disabled = true;
  try {
    const res = await fetch('/explain?test=' + encodeURIComponent(test));
    const data = await res.json();
        body.textContent = data.result || data.error || 'No response.';
  } catch (e) {
        body.textContent = 'Error: ' + e;
  } finally {
    btn.disabled = false;
        btn.textContent = 'Hide';
  }
}
</script>

</div>
"""


@app.route("/")
def index():
    now = time.monotonic()
    if now - _DASHBOARD_CACHE["ts"] > _DASHBOARD_CACHE_TTL_S:
        _DASHBOARD_CACHE["rows"] = score_tests()[:50]
        _DASHBOARD_CACHE["ts"] = now
    rows = _DASHBOARD_CACHE["rows"]
    return render_template_string(
        PAGE,
        rows=rows,
        flaky_count=len(rows),
        total_wasted_s=sum(r["wasted_time_s"] for r in rows),
        top_score=rows[0]["score"] if rows else 0.0,
    )


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.route("/llm-health")
def llm_health():
    return llm_config_status()


@app.route("/fetch", methods=["POST"])
def fetch():
    try:
        owner, repo = parse_repo(request.form["repo"])
        if not owner:
            flash("Enter a valid public GitHub repo URL or owner/repo.")
            return redirect(url_for("index"))
        # UI targets public repos; service token (if set) helps avoid rate limits.
        token = os.environ.get("GITHUB_TOKEN", "")
        n_runs, n_arts, n_cases, dl_errors = fetch_repo(
            owner, repo, token,
            request.form.get("branch", "").strip(),
            int(request.form.get("max_runs", 30)),
            request.form.get("filter", "test").strip() or "test",
        )
        if n_cases:
            _DASHBOARD_CACHE["ts"] = 0.0
            flash(f"Ingested {n_cases} test results from {n_arts} artifacts "
                  f"across {n_runs} runs.")
        elif dl_errors:
            flash("Found test artifacts but couldn't download their contents. "
                  "GitHub may require authentication to download artifact contents. "
                  "Set GITHUB_TOKEN on the server for this demo UI, or for private repos "
                  "use ci-snippet/flaky-tests.yml in your own CI setup.")
        else:
            flash("Fetched runs but found no matching test artifacts. Check the "
                  "'artifact name has' filter against your workflow's upload step — "
                  "and confirm the repo actually uploads test reports.")
    except requests.HTTPError as e:
        code = e.response.status_code
        if code in (401, 403):
            flash(f"GitHub API {code}: authentication or rate limit. Public metadata "
                  f"is capped at 60 req/hr without a token; set GITHUB_TOKEN on the server "
                  f"for this demo.")
        else:
            flash(f"GitHub API error {code}: check that this is a valid public repo URL.")
    except Exception as e:
        flash(f"Error: {e}")
    return redirect(url_for("index"))


@app.route("/seed", methods=["POST"])
def seed():
    n = seed_demo()
    flash(f"Loaded {n} demo rows. Click 'Explain' on any flaky test below.")
    return redirect(url_for("index"))


@app.route("/explain")
def explain():
    test = request.args.get("test", "")
    conn = connect()
    msgs = [m for (m,) in conn.execute(
        "SELECT message FROM results WHERE test_id=? AND message<>'' "
        "ORDER BY ts DESC LIMIT 5", (test,)).fetchall()]
    conn.close()
    if not msgs:
        return {"error": "No failure messages stored for this test."}

    try:
        return {"result": explain_test_messages(test, msgs)}
    except Exception as e:
        return {"error": "Explain unavailable. Configure LLM_PROVIDER and matching credentials. "
                         "Supported providers: anthropic, openai, ollama. "
                         f"Details: {e}"}


if __name__ == "__main__":
    app.run(
        debug=False,
        use_reloader=False,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
    )
