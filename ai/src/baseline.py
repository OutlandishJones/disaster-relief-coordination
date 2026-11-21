"""
Regex/keyword baseline (no AI) + side-by-side comparison with the LLM.
Run AFTER run_batch.py (needs data/extracted.json).
Put in the same folder as run_batch.py (newEra/src). Usage: python baseline.py
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
reports = json.load(open(os.path.join(DATA, "reports.json"), encoding="utf-8"))
llm = {r["report_id"]: r for r in json.load(open(os.path.join(DATA, "extracted.json"), encoding="utf-8"))}

# --- simple keyword rules (English + a few Spanish/Hindi words, to be fair) ---
NEED_WORDS = {
    "water": ["water", "agua", "pani", "paani"],
    "food": ["food", "comida", "khana", "rice", "bread"],
    "medicine": ["medicine", "medicina", "insulin", "dawai", "medicin"],
    "blanket": ["blanket", "mantas", "manta", "kambal"],
    "shelter": ["shelter", "refugio"],
    "transport": ["transport", "vehicle", "bus", "truck"],
}
PLACES = ["rampur", "green park", "barrio sur"]  # known neighborhood list
HIGH_WORDS = ["urgent", "urgente", "emergency", "hurry", "critical", "diabet",
              "insulin", "cant walk", "can't walk", "since yesterday", "wheelchair"]


def baseline(text: str) -> dict:
    t = text.lower()
    need = next((k for k, ws in NEED_WORDS.items() if any(w in t for w in ws)), None)
    m = re.search(r"\d+", t)
    qty = int(m.group()) if m else None
    place = next((p for p in PLACES if p in t), None)
    urg = "high" if any(w in t for w in HIGH_WORDS) else "medium"
    return {"need_type": need, "quantity": qty, "neighborhood": place, "urgency": urg}


def same(field, truth, pred):
    if truth is None or pred is None:
        return truth is None and pred is None
    if field == "neighborhood":
        a, b = str(truth).lower(), str(pred).lower()
        return a in b or b in a
    return str(truth).lower() == str(pred).lower()


FIELDS = ["need_type", "quantity", "neighborhood", "urgency"]
score = {"baseline": {f: 0 for f in FIELDS}, "llm": {f: 0 for f in FIELDS}}
n = len(reports)
base_out = []
for r in reports:
    b = baseline(r["text"])
    base_out.append({"report_id": r["id"], **b})
    l = llm.get(r["id"], {})
    for f in FIELDS:
        score["baseline"][f] += same(f, r["truth"][f], b[f])
        score["llm"][f] += same(f, r["truth"][f], l.get(f))

json.dump(base_out, open(os.path.join(DATA, "baseline_extracted.json"), "w", encoding="utf-8"), indent=2)

print(f"{'field':14s} {'baseline':>10s} {'LLM':>10s}")
for f in FIELDS:
    print(f"{f:14s} {100*score['baseline'][f]/n:9.0f}% {100*score['llm'][f]/n:9.0f}%")
tb = sum(score["baseline"].values()) / (n * len(FIELDS))
tl = sum(score["llm"].values()) / (n * len(FIELDS))
print(f"{'OVERALL':14s} {100*tb:9.0f}% {100*tl:9.0f}%")