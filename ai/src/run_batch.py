"""
Run all reports through extract(), save results, print accuracy vs ground truth.
Put this file in the SAME folder as extraction.py.
Usage: python run_batch.py            (all reports)
       python run_batch.py 3          (first 3 only, quick test)
"""
import json, sys, time, os
from extraction import extract

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = [
    os.path.join(HERE, "data", "reports.json"),
    os.path.join(HERE, "..", "data", "reports.json"),
    os.path.join(HERE, "reports.json"),
]
path = next((p for p in CANDIDATES if os.path.exists(p)), None)
if not path:
    sys.exit("reports.json not found. Put it in newEra/data/")
OUT = os.path.join(os.path.dirname(path), "extracted.json")

reports = json.load(open(path, encoding="utf-8"))
if len(sys.argv) > 1:
    reports = reports[: int(sys.argv[1])]

FIELDS = ["need_type", "quantity", "neighborhood", "urgency"]
correct = {f: 0 for f in FIELDS}
results, review_count = [], 0


def same(field, truth, pred):
    if truth is None or pred is None:
        return truth is None and pred is None
    if field == "neighborhood":
        t, p = str(truth).lower(), str(pred).lower()
        return t in p or p in t
    return str(truth).lower() == str(pred).lower()


start = time.time()
for r in reports:
    t0 = time.time()
    out = extract(r["text"], r["id"])
    out["truth"] = r.get("truth")
    results.append(out)
    if out.get("needs_review"):
        review_count += 1
    bad = []
    for f in FIELDS:
        if same(f, r["truth"][f], out.get(f)):
            correct[f] += 1
        else:
            bad.append(f"{f}: expected {r['truth'][f]!r}, got {out.get(f)!r}")
    status = "OK " if not bad else "ERR"
    print(f"{status} {r['id']} ({time.time()-t0:.1f}s) review={out.get('needs_review')}")
    for b in bad:
        print("     ", b)

json.dump(results, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

n = len(reports)
print("\n=== ACCURACY (LLM) ===")
for f in FIELDS:
    print(f"{f:14s} {correct[f]}/{n}  ({100*correct[f]/n:.0f}%)")
print(f"needs_review   {review_count}/{n}")
print(f"total time     {time.time()-start:.0f}s")
print(f"saved -> {OUT}")