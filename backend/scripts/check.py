"""One-command health check. Prints PASS / WARN / FAIL / SKIP for each step.

    cd backend
    python -m scripts.check                # everything, including the test suite (~5 s)
    python -m scripts.check --skip-tests   # quick: environment, Ollama, database, server

Exit code is 1 if any step FAILS, so it can also run in CI. Plain ASCII output, so it reads the
same in Windows PowerShell.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from importlib.util import find_spec

MODEL = "qwen2.5:7b"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
SERVER = os.environ.get("CHECK_SERVER", "http://localhost:8000").rstrip("/")
DB = os.environ.get("DB_PATH", "data/relief.db")
results = []


def report(status, step, detail="", fix=""):
    results.append(status)
    print(f"{status:<5} {step}" + (f": {detail}" if detail else ""))
    if fix and status in ("FAIL", "WARN"):
        print(f"      -> {fix}")


def get(url, headers=None, timeout=3):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def check_python():
    v = sys.version_info
    ok = v >= (3, 10)
    report("PASS" if ok else "FAIL", "Python", f"{v.major}.{v.minor}.{v.micro}", "Install Python 3.10 or newer")


def check_packages():
    need = ["fastapi", "uvicorn", "pydantic", "ollama", "httpx"]
    missing = [m for m in need if find_spec(m) is None]
    if missing:
        report("FAIL", "Python packages", "missing " + ", ".join(missing),
               "pip install -r requirements.txt ollama httpx")
    else:
        report("PASS", "Python packages", "all installed")


def check_env():
    missing = [k for k in ("PII_HMAC_KEY", "API_KEY") if not os.environ.get(k)]
    if missing:
        report("WARN", "Settings", "not set: " + ", ".join(missing),
               'PowerShell: $env:PII_HMAC_KEY="..."; $env:API_KEY="demo123"  (same values as the server)')
    else:
        report("PASS", "Settings", "PII_HMAC_KEY and API_KEY set")


def check_tests():
    env = {**os.environ, "AUTO_EXTRACT": "0", "PYTHONWARNINGS": "ignore"}
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       capture_output=True, text=True, env=env)
    out = p.stdout + p.stderr
    ran = re.search(r"Ran (\d+) test", out)
    skipped = re.search(r"skipped=(\d+)", out)
    n, k = (ran.group(1) if ran else "?"), int(skipped.group(1)) if skipped else 0
    if p.returncode != 0:
        fails = re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)
        report("FAIL", "Tests", f"{n} run, failing: {', '.join(fails[:5]) or 'see output'}",
               "python -m unittest discover -s tests -t . -v")
    elif k:
        report("WARN", "Tests", f"{n} run, {k} skipped", "Usually FastAPI or httpx missing: pip install fastapi httpx")
    else:
        report("PASS", "Tests", f"{n} run, all passed")


def check_ollama():
    try:
        _, _, body = get(OLLAMA + "/api/tags")
    except (urllib.error.URLError, OSError):
        report("FAIL", "Ollama", f"not reachable at {OLLAMA}", "Open the Ollama app (or run: ollama serve)")
        return
    names = [m.get("name", "") for m in json.loads(body or b"{}").get("models", [])]
    if any(n == MODEL or n.startswith(MODEL) for n in names):
        report("PASS", "Ollama", f"running, {MODEL} installed")
    else:
        report("FAIL", "Ollama", f"running, but {MODEL} not installed", f"ollama pull {MODEL}")


def check_database():
    if not os.path.exists(DB):
        report("FAIL", "Database", f"{DB} not found",
               "python -m scripts.seed_demo --reset, then python -m scripts.run_extraction --live")
        return
    sys.path.insert(0, os.getcwd())
    from app import audit, db
    conn = db.connect(DB)
    reports = conn.execute("SELECT COUNT(*) FROM reports WHERE status!='superseded'").fetchone()[0]
    unread = conn.execute("SELECT COUNT(*) FROM reports WHERE status='new'").fetchone()[0]
    models = [r[0] for r in conn.execute("SELECT DISTINCT model FROM extractions")]
    if "synthetic-seed" in models:
        report("FAIL", "Database", "hand-written readings present (model='synthetic-seed')",
               "python -m scripts.seed_demo --reset, then python -m scripts.run_extraction --live")
    elif unread:
        report("WARN", "Database", f"{reports} reports, {unread} not read by the model yet",
               "python -m scripts.run_extraction --live  (or start the server: it reads new reports itself)")
    else:
        report("PASS", "Database", f"{reports} reports, all read by {', '.join(models) or 'no model'}")
    from app import region
    zones = {r[0] for r in conn.execute("SELECT DISTINCT zone FROM extractions WHERE zone IS NOT NULL")}
    other = zones - region.zone_ids()
    if other:
        report("WARN", "Region", f"database has areas from another region: {', '.join(sorted(other))}",
               f"REGION is {region.region_name()!r}. Reseed for it: python -m scripts.seed_demo --reset")
    else:
        r = region.load()
        report("PASS", "Region", f"{r['name']} (REGION={r['id']})")
    ok, bad = audit.verify_chain(conn)
    report("PASS" if ok else "FAIL", "Audit log", "chain intact" if ok else f"broken at row {bad}",
           "The log was edited outside the app. Investigate before trusting it.")
    conn.close()


def check_server():
    try:
        get(SERVER + "/health")
    except (urllib.error.URLError, OSError):
        report("SKIP", "Server", f"not running at {SERVER}")
        return
    key = os.environ.get("API_KEY", "")
    try:
        _, ctype, body = get(SERVER + "/dashboard/state", {"X-API-Key": key} if key else {})
    except urllib.error.HTTPError as e:
        report("FAIL", "Server", f"/dashboard/state returned {e.code}",
               "401 means API_KEY here doesn't match the server's")
        return
    s = json.loads(body)
    detail = f"{len(s.get('reports', []))} reports on the dashboard, {s.get('held_back', 0)} held back"
    report("PASS", "Server", detail)
    if "charset=utf-8" not in ctype.lower():
        report("WARN", "Server charset", ctype or "none",
               "Restart the server with the latest code (non-English text may look garbled in PowerShell)")


def main():
    print("Disaster relief backend check\n")
    check_python()
    check_packages()
    check_env()
    if "--skip-tests" in sys.argv:
        report("SKIP", "Tests", "--skip-tests")
    else:
        check_tests()
    check_ollama()
    check_database()
    check_server()
    fails, warns = results.count("FAIL"), results.count("WARN")
    print(f"\n{'READY' if not fails else 'NOT READY'}: {fails} fail, {warns} warn")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
