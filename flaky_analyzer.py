"""
flaky_analyzer.py — a minimal, stack-agnostic flaky test analyzer.

Consumes JUnit / Maven Surefire XML reports (also works for TestNG and
Cucumber runs that emit JUnit-style XML), stores per-run outcomes in SQLite,
scores flakiness per test, and ranks flaky tests by wasted CI time.

Detection signals (strongest first):
  1. rerun flake  — Surefire <flakyFailure>/<flakyError>: definitive.
  2. same-commit divergence — same SHA produced both pass and fail.
  3. flip rate    — how often the outcome changes over the run history.

Usage:
    # 1) Tell Maven to rerun failures so flakes get labelled (pom.xml):
    #    <configuration><rerunFailingTestsCount>2</rerunFailingTestsCount></configuration>
    #
    # 2) After each CI run, ingest that run's reports:
    python flaky_analyzer.py ingest --reports target/surefire-reports \
        --commit "$GIT_SHA" --branch "$GIT_BRANCH" --run-id "$CI_RUN_ID"
    #
    # 3) Anytime, produce the ranked flaky report:
    python flaky_analyzer.py report --top 20
    #
    # 4) Optional: explain a test's likely root cause via the Anthropic API
    #    (needs `pip install anthropic` and ANTHROPIC_API_KEY in the env):
    python flaky_analyzer.py classify --test "com.acme.LoginTest#loginRedirects"
"""

import argparse
import datetime as dt
import glob
import os
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict

DB_PATH = os.environ.get("FLAKY_DB", "flaky.db")


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def connect():
    db_path = DB_PATH
    try:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError:
        # Last-resort fallback for serverless/read-only deploy environments.
        fallback = "/tmp/flaky.db"
        os.environ["FLAKY_DB"] = fallback
        db_path = fallback
        conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS results (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id    TEXT,
            commit_sha TEXT,
            branch    TEXT,
            ts        TEXT,
            test_id   TEXT,
            outcome   TEXT,   -- pass | fail | flake | skip
            duration  REAL,
            message   TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_test ON results(test_id)")
    return conn


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
def _outcome(tc):
    """Classify a <testcase> element into a single outcome."""
    if tc.find("flakyFailure") is not None or tc.find("flakyError") is not None:
        return "flake"
    if tc.find("skipped") is not None:
        return "skip"
    if any(tc.find(t) is not None for t in
           ("failure", "error", "rerunFailure", "rerunError")):
        return "fail"
    return "pass"


def _message(tc):
    for tag in ("flakyFailure", "flakyError", "failure", "error"):
        el = tc.find(tag)
        if el is not None:
            return (el.get("message") or el.text or "").strip()[:4000]
    return ""


def ingest(reports_dir, commit_sha, branch, run_id, ts=None):
    ts = ts or dt.datetime.utcnow().isoformat()
    files = glob.glob(os.path.join(reports_dir, "**", "*.xml"), recursive=True)
    if not files:
        print(f"No XML reports found under {reports_dir}")
        return

    conn = connect()
    rows, n_cases = [], 0
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for tc in root.iter("testcase"):
            cls = tc.get("classname") or tc.get("class") or ""
            name = tc.get("name") or ""
            test_id = f"{cls}#{name}" if cls else name
            try:
                duration = float(tc.get("time") or 0.0)
            except ValueError:
                duration = 0.0
            rows.append((run_id, commit_sha, branch, ts, test_id,
                         _outcome(tc), duration, _message(tc)))
            n_cases += 1

    conn.executemany(
        "INSERT INTO results (run_id, commit_sha, branch, ts, test_id, "
        "outcome, duration, message) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    print(f"Ingested {n_cases} test cases from run {run_id} ({commit_sha[:8]}).")


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score_tests():
    """Return a list of per-test flakiness records, most flaky first."""
    conn = connect()
    cur = conn.execute(
        "SELECT test_id, commit_sha, outcome, duration, ts "
        "FROM results ORDER BY ts ASC")
    by_test = defaultdict(list)
    for test_id, sha, outcome, duration, ts in cur.fetchall():
        by_test[test_id].append((sha, outcome, duration))
    conn.close()

    records = []
    for test_id, runs in by_test.items():
        outcomes = [o for _, o, _ in runs if o != "skip"]
        n = len(outcomes)
        if n == 0:
            continue

        rerun_flakes = sum(1 for o in outcomes if o == "flake")

        # same-commit divergence: a commit that shows both pass and fail/flake
        per_commit = defaultdict(set)
        for sha, o, _ in runs:
            if o != "skip":
                per_commit[sha].add(o)
        distinct_commits = len(per_commit) or 1
        divergent = sum(1 for s in per_commit.values()
                        if "pass" in s and ({"fail", "flake"} & s))

        # flip rate over the ordered binary sequence (flake counts as a fail)
        binseq = [0 if o == "pass" else 1 for o in outcomes]
        flips = sum(1 for a, b in zip(binseq, binseq[1:]) if a != b)
        flip_rate = flips / (n - 1) if n > 1 else 0.0

        # composite score in [0, 1] — take the strongest signal present
        score = max(
            1.0 if rerun_flakes > 0 else 0.0,
            divergent / distinct_commits,
            flip_rate,
        )
        if score == 0:
            continue  # never diverged -> not flaky on the evidence we have

        avg_dur = sum(d for _, _, d in runs) / len(runs)
        wasted = round(score * avg_dur * n, 2)  # heuristic CI-time at risk (s)

        records.append({
            "test_id": test_id,
            "score": round(score, 3),
            "runs": n,
            "rerun_flakes": rerun_flakes,
            "divergent_commits": divergent,
            "flip_rate": round(flip_rate, 3),
            "avg_duration_s": round(avg_dur, 3),
            "wasted_time_s": wasted,
        })

    records.sort(key=lambda r: r["wasted_time_s"], reverse=True)
    return records


def report(top):
    records = score_tests()[:top]
    if not records:
        print("No flaky tests detected yet (need >1 run per commit to see divergence).")
        return
    print(f"\n{'SCORE':>6}  {'WASTED(s)':>9}  {'RUNS':>4}  {'FLIP':>5}  TEST")
    print("-" * 80)
    for r in records:
        print(f"{r['score']:>6.2f}  {r['wasted_time_s']:>9.1f}  "
              f"{r['runs']:>4}  {r['flip_rate']:>5.2f}  {r['test_id']}")
    print(f"\n{len(records)} flaky tests shown, ranked by estimated wasted CI time.")


# --------------------------------------------------------------------------- #
# Optional LLM root-cause classifier (the "agent" layer)
# --------------------------------------------------------------------------- #
ROOT_CAUSE_CATEGORIES = [
    "async/timing (missing or insufficient wait)",
    "test-order dependency / leaked shared state",
    "external dependency (network, DB, third-party API)",
    "concurrency / race condition",
    "time or date dependency",
    "non-deterministic test data / randomness",
    "environment / resource exhaustion",
]


def _call_anthropic(prompt):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    model = os.environ.get("LLM_MODEL", "claude-sonnet-4-5")
    resp = client.messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "\n".join(b.text for b in resp.content if b.type == "text")


def _call_openai(prompt):
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai package is not installed. Run: pip install openai") from e

    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    resp = client.responses.create(
        model=model,
        input=prompt,
        max_output_tokens=500,
    )
    return resp.output_text


def _call_ollama(prompt):
    import requests

    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.environ.get("LLM_MODEL", os.environ.get("OLLAMA_MODEL", "llama3.2"))
    r = requests.post(
        f"{base.rstrip('/')}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("response", "")


def explain_with_model(prompt):
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if provider == "anthropic":
        return _call_anthropic(prompt)
    if provider == "openai":
        return _call_openai(prompt)
    if provider == "ollama":
        return _call_ollama(prompt)
    raise RuntimeError("Unsupported LLM_PROVIDER. Use one of: anthropic, openai, ollama")


def explain_test_messages(test_id, messages):
    prompt = build_root_cause_prompt(test_id, messages)
    return explain_with_model(prompt)


def build_root_cause_prompt(test_id, messages):
    sample = "\n---\n".join(m for m in messages if m)[:6000]
    cats = "\n".join(f"- {c}" for c in ROOT_CAUSE_CATEGORIES)
    return (
        f"You are a flaky-test triage assistant. A test failed intermittently "
        f"on unchanged code.\n\nTest: {test_id}\n\n"
        f"Observed failure messages / stack traces across runs:\n{sample}\n\n"
        f"Classify the most likely root cause into ONE of these categories:\n{cats}\n\n"
        f"Respond with: (1) the category, (2) a one-sentence justification grounded "
        f"in the trace, (3) a concrete fix for a Java/TestNG/Cucumber suite."
    )


def classify(test_id):
    conn = connect()
    msgs = [m for (m,) in conn.execute(
        "SELECT message FROM results WHERE test_id=? AND message<>'' "
        "ORDER BY ts DESC LIMIT 5", (test_id,)).fetchall()]
    conn.close()
    if not msgs:
        print(f"No failure messages stored for {test_id}.")
        return

    try:
        print(explain_test_messages(test_id, msgs))
    except Exception as e:
        print(f"LLM explain unavailable: {e}")
        print("Here is the prompt you can send to any model:\n")
        print(build_root_cause_prompt(test_id, msgs))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="Minimal flaky test analyzer")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="Parse and store one run's reports")
    pi.add_argument("--reports", required=True, help="Dir of Surefire/JUnit XML")
    pi.add_argument("--commit", required=True)
    pi.add_argument("--branch", default="unknown")
    pi.add_argument("--run-id", required=True)

    pr = sub.add_parser("report", help="Rank flaky tests by wasted CI time")
    pr.add_argument("--top", type=int, default=20)

    pc = sub.add_parser("classify", help="LLM root-cause for one test")
    pc.add_argument("--test", required=True)

    args = p.parse_args()
    if args.cmd == "ingest":
        ingest(args.reports, args.commit, args.branch, args.run_id)
    elif args.cmd == "report":
        report(args.top)
    elif args.cmd == "classify":
        classify(args.test)


if __name__ == "__main__":
    main()
