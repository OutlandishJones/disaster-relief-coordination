"""
Duplicate detection: link likely duplicates, NEVER delete. Same address + different household stays separate.
Run AFTER run_batch.py. Put in newEra/src. Usage: python dedupe.py
First run downloads a ~120MB model (needs internet once).
Output: data/duplicates.json
"""
import json, os
from sentence_transformers import SentenceTransformer, util

THRESH = 0.65  # similarity needed to call two reports duplicates (tune using printed scores)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
rows = [r for r in json.load(open(os.path.join(DATA, "extracted.json"), encoding="utf-8")) if r.get("need_type")]
truth = {r["id"]: r.get("duplicate_of") for r in json.load(open(os.path.join(DATA, "reports.json"), encoding="utf-8"))}

model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
emb = model.encode([r["raw_text"] for r in rows], convert_to_tensor=True)
sim = util.cos_sim(emb, emb)


def same_place(a, b):
    x, y = a.get("neighborhood"), b.get("neighborhood")
    if not x or not y:
        return False
    x, y = x.lower(), y.lower()
    return x in y or y in x


def households_differ(a, b):
    """Different quantity or household size => different household, even at the same address."""
    for f in ("quantity", "household_size"):
        if a.get(f) and b.get(f) and a[f] != b[f]:
            return True
    return False


out = []
print("pairs with same need + same neighborhood:")
for i, a in enumerate(rows):
    best, best_s, related = None, 0.0, []
    for j in range(i):
        b = rows[j]
        if a["need_type"] != b["need_type"] or not same_place(a, b):
            continue
        s = float(sim[i][j])
        if households_differ(a, b):
            verdict = "separate_household"
        elif s >= THRESH:
            verdict = "likely_duplicate"
            if s > best_s:
                best, best_s = b["report_id"], s
        else:
            verdict = "similar_location_low_text_similarity"
        related.append({"report_id": b["report_id"], "similarity": round(s, 2), "verdict": verdict})
        print(f"  {a['report_id']} vs {b['report_id']}: sim={s:.2f} -> {verdict}")
    out.append({
        "report_id": a["report_id"],
        "duplicate_of": best,
        "duplicate_score": round(best_s, 2) if best else None,
        "related": related,   # evidence shown on the coordinator dashboard
    })

json.dump(out, open(os.path.join(DATA, "duplicates.json"), "w", encoding="utf-8"), indent=2)

wrong = [(o["report_id"], o["duplicate_of"], truth.get(o["report_id"]))
         for o in out if o["duplicate_of"] != truth.get(o["report_id"])]
true_dups = sum(1 for v in truth.values() if v)
found = sum(1 for o in out if o["duplicate_of"] and o["duplicate_of"] == truth.get(o["report_id"]))
false_merges = sum(1 for o in out if o["duplicate_of"] and not truth.get(o["report_id"]))
print(f"\ntrue duplicates found: {found}/{true_dups}")
print(f"false merges (wrongly linked): {false_merges}")
for rid, got, exp in wrong:
    print(f"  WRONG {rid}: got {got}, expected {exp}")
print("saved -> data/duplicates.json (nothing deleted)")