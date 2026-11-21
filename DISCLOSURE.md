# Disclosure log

Hack-on-the-Hill Fall 2026, Problem 2. The rules ask teams to identify simulated components,
external models, reused code and AI development tools, and to explain what the team built.

## Data

| Data | Type | Where | Notes |
|---|---|---|---|
| 8 flood reports, stock, roads (demo scenario) | **Synthetic**, written by the team | `backend/scripts/seed_demo.py` | English, Spanish, Mandarin, Vietnamese. Phone numbers are fictional 555 numbers. |
| 13 test reports with ground truth | **Synthetic**, written by the team | `ai/data/reports.json` | Used for the keyword-vs-LLM accuracy table. |
| 30 tweets | **Public** research data, **not included** in this repository | Downloaded locally by `ai/src/sample_humaid.py` | Sample of the HumAID dataset (QCRI), used only as a realism check for the extractor. The script masks phone numbers, emails and @usernames, and its output is git-ignored, so no real posts are published here. Not used in the demo. |

No private or permissioned data is used.

## Simulated components

- Incoming reports, supplies, vehicles-as-stock, neighborhoods and the road network are simulated.
- There is no real SMS, phone or web-form intake. Reports are loaded by a seed script or the API.
- Travel times are fixed numbers on a 5-node map, not live map data.
- Nothing is actually dispatched. "Approved" means recorded as approved.

Outputs are **not** simulated: extraction fields come from the model at run time, and the plan,
the first-come-first-served comparison and the audit log are computed by running code.

## External models

| Model | Used for | How it runs |
|---|---|---|
| `qwen2.5:7b` (via Ollama) | Extracting need, quantity, location, urgency, confidence from report text | Locally on a team laptop. Report text is not sent to an outside service. |
| `paraphrase-multilingual-MiniLM-L12-v2` (sentence-transformers) | Text similarity for duplicate linking in `ai/src/dedupe.py` and `pipeline.py` | Locally |

## Reused code and libraries

FastAPI, Uvicorn, Pydantic, SQLite (Python standard library), Ollama Python client,
sentence-transformers, Hugging Face `datasets` (to sample HumAID). No code was copied from
other projects.

## AI development tools

- **Claude (Anthropic)** was used as a coding and writing assistant. With the team directing and
  testing on a team laptop, it wrote or edited: the extractor-to-backend bridge
  (`backend/app/ai_bridge.py`, `backend/scripts/run_extraction.py`, `backend/tests/test_ai_bridge.py`),
  the planning rules in the dashboard (roads, time windows, critical-first, fair baseline), a fix to
  the extractor's validation (`ai/src/extraction.py`), this file and the README, and the slide design.
- Other AI tools used by team members, including for the rest of `backend/`, the original
  dashboard and `ai/`: _team to fill in before submitting_.

## What the team built

Multilingual extraction with a schema, prompt and review flag; a keyword baseline and accuracy
comparison; duplicate linking that never deletes; a SQLite backend with PII scrubbing, keyed
household tokens, approval gating and a hash-chained audit log; a coordinator dashboard with
evidence, approve / reject / correct, road closures and a priority plan compared against
first-come-first-served.

## Known limits

- The plan is a priority rule computed in the browser, not a server-side optimizer.
- Accessibility needs are extracted and shown but not used in planning.
- Override exists in the backend but has no dashboard button; coordinators approve, reject or correct.
- The comparison reports needs met in time and unmet units, not delay.
- Reports are not verified. Repeat reports from one household are linked and counted once.
- Name scrubbing only catches names known from the intake form. Login is demo-level.
- The accuracy test set is small (11 held-out reports).
