"""
One-shot pipeline: reports.json -> extraction -> duplicate linking -> data/processed.json
Backend/dashboard people read processed.json only.
Put in newEra/src. Usage: python pipeline.py   (or: python pipeline.py 5  for first 5)
"""
import json, os, sys
from sentence_transformers import SentenceTransformer, util
from extraction import extract

THRESH = 0.65
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
reports = json.load(open(os.path.join(DATA, "reports.json"), encoding="utf-8"))
if len(sys.argv) > 1:
    reports = reports[: int(sys.argv[1])]

# 1. extraction
rows = []
for r in reports:
    out = extract(r["text"], r["id"])
    out["channel"] = r.get("channel")
    rows.append(out)
    print(f"extracted {r['id']} review={out.get('needs_review')}")

ok = [r for r in rows if r.get("need_type")]
failed = [r["report_id"] for r in rows if not r.get("need_type")]

# 2. duplicate linking (never deletes)
model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
sim = util.cos_sim(model.encode([r["raw_text"] for r in ok], convert_to_tensor=True),
                   model.encode([r["raw_text"] for r in ok], convert_to_tensor=True))


def same_place(a, b):
    x, y = a.get("neighborhood"), b.get("neighborhood")
    if not x or not y:
        return False
    x, y = x.lower(), y.lower()
    return x in y or y in x


def households_differ(a, b):
    return any(a.get(f) and b.get(f) and a[f] != b[f] for f in ("quantity", "household_size"))


for i, a in enumerate(ok):
    best, best_s, related = None, 0.0, []
    for j in range(i):
        b = ok[j]
        if a["need_type"] != b["need_type"] or not same_place(a, b):
            continue
        s = float(sim[i][j])
        if households_differ(a, b):
            v = "separate_household"
        elif s >= THRESH:
            v = "likely_duplicate"
            if s > best_s:
                best, best_s = b["report_id"], s
        else:
            v = "similar_location_low_text_similarity"
        related.append({"report_id": b["report_id"], "similarity": round(s, 2), "verdict": v})
    a["duplicate_of"] = best
    a["duplicate_score"] = round(best_s, 2) if best else None
    a["related"] = related
    if best:
        a["needs_review"] = True  # a human should confirm links

# failed extractions are kept (never dropped) and always go to review
for r in rows:
    if not r.get("need_type"):
        r.setdefault("duplicate_of", None)
        r.setdefault("related", [])
        r["needs_review"] = True

json.dump(rows, open(os.path.join(DATA, "processed.json"), "w", encoding="utf-8"),
          indent=2, ensure_ascii=False)
print(f"\nsaved {len(rows)} reports -> data/processed.json | failed extractions: {failed or 'none'}")
print(f"needs_review: {sum(1 for r in rows if r.get('needs_review'))}/{len(rows)}")