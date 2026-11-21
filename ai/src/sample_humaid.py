"""
Pull a small sample of REAL tweets from HumAID, save them, and run them through extract().
Put in newEra/src.  Setup: pip install datasets
Usage: python sample_humaid.py          (20 requests + 10 non-requests)
Output: data/humaid_sample.json, data/humaid_extracted.json
"""
import json, os, random, re
from datasets import load_dataset
from extraction import extract

N_REQ, N_OTHER = 20, 10

# These are real public tweets: mask phone numbers, emails and @usernames before saving them or
# sending them to the model. The output files are git-ignored: real posts never go in the repo.
HANDLE = re.compile(r"(?<![\w@])@\w{1,15}")     # @usernames of real people
PHONE = re.compile(r"(?<![\w(])\(?\+?\d[\d\s().-]{6,}\d(?![\w])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
def mask(text):
    return HANDLE.sub("@[USER]", PHONE.sub("[PHONE]", EMAIL.sub("[EMAIL]", text or "")))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")

try:
    ds = load_dataset("QCRI/HumAID-all", split="test", verification_mode="no_checks")
except Exception as e:
    print("load_dataset failed, using parquet fallback:", str(e)[:100])
    import pandas as pd
    ds = pd.read_parquet(
        "hf://datasets/QCRI/HumAID-all@refs%2Fconvert%2Fparquet/default/test/0000.parquet"
    ).to_dict("records")
random.seed(42)
req = [r for r in ds if r["class_label"] == "requests_or_urgent_needs"]
oth = [r for r in ds if r["class_label"] in ("sympathy_and_support", "injured_or_dead_people",
                                             "not_humanitarian", "other_relevant_information")]
sample = random.sample(req, N_REQ) + random.sample(oth, N_OTHER)

rows = [{"id": f"H{i+1:02d}", "channel": "tweet", "text": mask(r["tweet_text"]), "label": r["class_label"]}
        for i, r in enumerate(sample)]
json.dump(rows, open(os.path.join(DATA, "humaid_sample.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)

results = []
for r in rows:
    out = extract(r["text"], r["id"])
    out["humaid_label"] = r["label"]
    results.append(out)
    print(f"{r['id']} [{r['label'][:18]:18s}] need={out.get('need_type')} urg={out.get('urgency')} "
          f"review={out.get('needs_review')} | {r['text'][:70]!r}")

json.dump(results, open(os.path.join(DATA, "humaid_extracted.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)

is_req = [o for o in results if o["humaid_label"] == "requests_or_urgent_needs"]
non_req = [o for o in results if o["humaid_label"] != "requests_or_urgent_needs"]
print(f"\nneeds_review on real requests:     {sum(bool(o.get('needs_review')) for o in is_req)}/{len(is_req)}")
print(f"needs_review on non-requests:      {sum(bool(o.get('needs_review')) for o in non_req)}/{len(non_req)}")
print("saved -> data/humaid_sample.json, data/humaid_extracted.json")