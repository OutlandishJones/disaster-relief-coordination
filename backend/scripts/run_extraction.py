"""Run Aminul's extractor against the backend database.

LIVE (the demo path; needs Ollama running with qwen2.5:7b, and `pip install ollama pydantic`):
    cd backend
    python -m scripts.seed_demo --reset --no-extractions
    python -m scripts.run_extraction --live            # extracts every report with status 'new'
    python -m scripts.run_extraction --live --redo     # re-extract all (adds new rows, keeps history)

IMPORT saved real model output (from_cache=1, labelled as such in the audit log):
    python -m scripts.run_extraction --import ../ai/data/extracted.json --model ollama:qwen2.5:7b
"""
import argparse
import os

from app import ai_bridge, db


def main(argv=None):
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true")
    g.add_argument("--import", dest="import_path")
    p.add_argument("--redo", action="store_true")
    p.add_argument("--model", default="ollama:qwen2.5:7b", help="model label for --import")
    a = p.parse_args(argv)

    path = os.environ.get("DB_PATH", "data/relief.db")
    conn = db.connect(path)
    db.init_db(conn)
    if a.live:
        extract, model = ai_bridge.load_extractor()
        done = ai_bridge.run_pending(conn, extract, model, redo=a.redo)
        print(f"\n{len(done)} reports extracted by {model} into {path}")
    else:
        ids = ai_bridge.import_outputs(conn, ai_bridge.load_json(a.import_path), a.model)
        print(f"\nImported {len(ids)} saved extractor outputs (from_cache=1) into {path}")
    review = conn.execute("SELECT COUNT(*) FROM reports WHERE status='needs_review'").fetchone()[0]
    print(f"needs_review: {review}")


if __name__ == "__main__":
    main()
