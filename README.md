# Disaster Relief Request and Resource Matching System

Hack-on-the-Hill Fall 2026, Problem 2. Team from Brightpoint CC, Virginia State University and
National Taipei University.

> **About this copy.** This is a public snapshot of the team's project, published without its
> development history. All demo data is synthetic and every phone number is a fictional 555-01xx
> number. Real-world sample data (public tweets from the HumAID research dataset) is not included;
> `ai/src/sample_humaid.py` downloads it locally if you want to rerun that check.

An AI-assisted tool for relief coordinators. It turns messy multilingual flood reports into
structured needs, links likely duplicates without deleting them, suggests how to share limited
supplies given open roads and time windows, and lets a coordinator review, correct and approve.
**Nothing is dispatched without a person's approval, and every action is logged.**

## How it works

1. **Ingest**: reports arrive as text (SMS, form, call transcript). Phones and emails are scrubbed first.
2. **Extract**: a local LLM (`qwen2.5:7b` in Ollama) reads need, quantity, location, urgency and its own confidence.
3. **Validate**: likely duplicates are linked, never deleted. Low-confidence readings go to a person.
4. **Match**: a priority plan shares stock using open roads and time windows; critical requests first.
5. **Review**: the coordinator sees the evidence and approves, rejects or corrects.
6. **Replan**: closing a road, using up stock or correcting a quantity changes the plan.
7. **Evaluate**: the plan is compared with first-come-first-served on the same stock and roads.

## Run it

Needs Python 3.11+, and [Ollama](https://ollama.com) with `ollama pull qwen2.5:7b`.

```bash
cd backend
pip install -r requirements.txt ollama
export PII_HMAC_KEY=any-long-random-text API_KEY=demo123     # PowerShell: $env:PII_HMAC_KEY="..."; $env:API_KEY="demo123"
python -m unittest discover -s tests -t .                    # 50 tests
python -m scripts.seed_demo --reset                         # 8 synthetic reports, stock, roads
python -m scripts.run_extraction --live                      # the real model reads each report
python -m uvicorn app.main:app --port 8000
```

To check everything at once (packages, tests, Ollama, database, audit chain, server), run
`python -m scripts.check` from `backend/`. It prints PASS, WARN or FAIL for each step, with a fix.

Open http://localhost:8000/dashboard?key=demo123 (API docs at `/docs`). More detail in
[`backend/README.md`](backend/README.md).

## Repository layout

| Path | What it is |
|---|---|
| `ai/` | Extraction (`src/extraction.py`), keyword baseline, duplicate linking, test data |
| `backend/` | Database, privacy scrubbing, audit log, API, and the live dashboard (`app/static/dashboard.html`) |
| `coordinator-dashboard.html` | The original standalone dashboard mock-up with built-in sample data. Not connected to the backend. |
| `DISCLOSURE.md` | Data sources, simulated parts, external models, AI tools used, known limits |

## Results

Extraction on 11 held-out synthetic reports (2 reports that appear in the prompt's examples are excluded):

| Field | Keyword / regex | LLM |
|---|---|---|
| Need type | 9/11 | 11/11 |
| Urgency | 6/11 | 9/11 |
| Quantity | 9/11 | 10/11 |
| Overall (4 fields) | 35/44 (80%) | 41/44 (93%) |

`python ai/src/baseline.py` reads the saved model output in `ai/data/` and scores all 13 reports
(81% vs 94%); the table above leaves out the 2 reports that overlap the prompt.
The sample is small; see `DISCLOSURE.md` for limits.

## Data

The demo scenario and test reports are synthetic and labeled as such. The realism check on 30 public
tweets from the HumAID research dataset is not included here: `ai/src/sample_humaid.py` downloads
them locally (masking phone numbers, emails and @usernames) and its output is never committed. See [`DISCLOSURE.md`](DISCLOSURE.md).
