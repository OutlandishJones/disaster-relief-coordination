   # newEra: Disaster Relief Coordination (AI extraction)
   Run order:
   1. Install Ollama, then: ollama pull qwen2.5:7b
   2. pip install ollama pydantic sentence-transformers
   3. cd src
   4. python pipeline.py        (reads data/reports.json, writes data/processed.json)
   5. python baseline.py        (keyword baseline vs LLM accuracy table)
   Output for backend/dashboard: data/processed.json