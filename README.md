# GridWise LLM — Smart Campus Energy Optimizer

BUP CSE Fest 2026 Hackathon (Online Preliminary). One HTTP API that reads a 24-hour campus energy
scenario plus 1–3 natural-language operator notes, uses an **LLM to interpret the notes** into
structured directives, validates them with **deterministic guardrails**, and computes the
**minimum-cost 24-hour grid/solar/battery schedule** with a linear program.

## Architecture

```
request ─► schema check ─► LLM chain ─► guardrails ─► LP optimizer ─► final replay check ─► JSON
           (400/422)       (see below)   (validate,     (SciPy HiGHS,     (energy balance,
                                          windows→hours) min grid cost)    battery, directives)
```

- **LLM role:** interprets every operator note into exactly one of the 6 supported directive types
  (`solar_reduction`, `minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`,
  `max_grid_window`, `no_op`). All notes of a request go in **one** call. The LLM returns time windows
  (`[[start, end]]`, end exclusive); code converts them to hours. The LLM never does scheduling or math.
- **LLM provider chain** (all free tiers, OpenAI-compatible endpoints via the `openai` Python SDK):
  1. Google Gemini `gemini-3.5-flash-lite` (primary)
  2. Mistral `ministral-14b-latest`
  3. Mistral `ministral-8b-latest`
  4. Groq `openai/gpt-oss-120b`
  5. Google Gemini `gemini-3.1-flash-lite`
  6. Rule-based parser — **last resort only** (all LLMs down / rate-limited / time budget used)

  On 429 / timeout / 5xx the next provider is tried; unparseable JSON or guardrail rejections get one
  corrective retry with the error fed back. A 24 s LLM budget keeps every request under 30 s.
  Fully LLM-produced interpretations are cached (identical notes → 0 extra calls).
- **Guardrails** (`app/guardrails.py`): directive type must be one of the 6; `no_op` forced to
  `applies=false` + `null`; others `applies=true`; hours unique, ascending, integers 0–23 (windows
  expanded deterministically, midnight wrap supported); `factor` in [0,1]; reserve in [0, capacity];
  grid cap ≥ 0; one entry per note in `note_index` order; extra keys dropped.
- **Optimizer** (`app/optimizer.py`): LP with variables per hour: grid, solar used, net battery flow
  (+charge / −discharge). The battery is lossless, so one net variable per hour is exact and guarantees
  one action per hour. Constraints: energy balance, effective solar, charge/discharge limits
  (0 in no-charge / no-discharge windows), battery bounds with reserves, grid caps, end-of-day
  neutrality. Objective: minimise Σ grid × tariff. Solved with SciPy `linprog` (HiGHS).
- **Final replay** (`app/validator.py`): the plan is re-checked hour by hour like the judge does;
  totals are recalculated from `hourly_plan`.

## Endpoints

| Method | Path | Response |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| POST | `/optimize-energy` | interpretation + 24-hour plan (schema from the Problem Statement) |
| GET | `/docs` | interactive API docs with a prefilled example |

Errors: **400** malformed JSON / structurally invalid request, **422** semantically invalid
(battery out of range) or infeasible scenario, **500** controlled `{"error":"internal error"}`
(no stack traces, no secrets).

## Environment variables

Copy `.env.example` to `.env` and fill in the keys (never commit `.env`). Providers without a key are skipped.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | — | Google AI Studio key (free, no card) |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | primary model |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.1-flash-lite` | last LLM in the chain |
| `GEMINI_REASONING_EFFORT` | `low` | Gemini thinking effort |
| `MISTRAL_API_KEY` | — | console.mistral.ai key (free Experiment plan) |
| `MISTRAL_MODEL` | `ministral-14b-latest` | second provider |
| `MISTRAL_FALLBACK_MODEL` | `ministral-8b-latest` | third provider |
| `GROQ_API_KEY` | — | console.groq.com key (free) |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | fourth provider |
| `LLM_TIMEOUT_S` | `8` | per-call LLM timeout (s) |
| `PORT` | `8000` | HTTP port |

## Local quickstart

```bash
git clone https://github.com/Tamim2276/BUP_Preli.git
cd BUP_Preli
python -m venv .venv
source .venv/bin/activate            # Windows Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env                 # then paste at least GEMINI_API_KEY (and optionally MISTRAL/GROQ keys)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl -X POST http://127.0.0.1:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d "$(python -c "import json;print(json.dumps(json.load(open('tests/public_samples.json'))['cases'][5]['input']))")"
```

Trimmed response:

```json
{
  "scenario_id": "SAMPLE-06",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [10, 11], "factor": 0.5}, "explanation": "..."},
    {"note_index": 1, "applies": true, "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [14, 15]}, "explanation": "..."},
    {"note_index": 2, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null, "explanation": "..."}
  ],
  "hourly_plan": [{"hour": 0, "grid_kwh": 105.0, "solar_used_kwh": 0.0, "battery_action": "charge",
                   "battery_kwh": 20.0, "battery_energy_after_kwh": 120.0}, "... 23 more ..."],
  "total_grid_kwh": 2395.0,
  "total_cost_bdt": 34090.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Applied 2 operator directive(s) ..."
}
```

## Public-sample test

With the server running:

```bash
python tests/run_samples.py                                   # local server
python tests/run_samples.py https://<your-deployment-url>     # deployed server
```

Expected result (verified):

```
schema 10/10 | interpretation 10/10 | valid 10/10 | optimal 10/10
latency: median ~1.1s | p95 ~2.8s
ALL PASS
```

Reference costs matched exactly: SAMPLE-01 38365, 02 42885, 03 35480, 04 40495, 05 33950,
06 34090, 07 38550, 08 37665, 09 34873, 10 41620.

Other tests (from the repo root):

| Command | Checks | LLM calls |
|---|---|---|
| `python tests/test_schemas.py` | request validation, 400/422 | 0 |
| `python tests/test_optimizer_offline.py` | optimizer + replay checker, 300 random scenarios | 0 |
| `python tests/test_interpreter.py` | guardrails, provider chain with simulated failures, cache | 0 |
| `python tests/test_api.py` | full API in-process, error codes | 0 |
| `python tests/test_paraphrases.py [--provider=gemini\|mistral\|groq]` | 31 reworded notes | ~11 |
| `python tests/test_robustness.py` | bad input, 20-request burst, invalid key | ~20 |

Measured accuracy on the 31 reworded notes: Gemini 31/31, ministral-14b 31/31, Groq gpt-oss-120b 31/31.

## Docker (fallback)

```bash
docker pull <dockerhub-user>/gridwise-llm:v1
docker run --rm -p 8000:8000 \
  -e GEMINI_API_KEY=... -e MISTRAL_API_KEY=... -e GROQ_API_KEY=... \
  <dockerhub-user>/gridwise-llm:v1
curl http://127.0.0.1:8000/health
```

The image listens on `0.0.0.0:8000` (override with `-e PORT=...`) and contains **no secrets**;
keys are passed at runtime. Built by GitHub Actions (`.github/workflows/docker.yml`).

## Dependencies and credits

FastAPI, Uvicorn, Pydantic, SciPy (HiGHS), NumPy, OpenAI Python SDK (used as the client for Gemini,
Mistral and Groq OpenAI-compatible endpoints), python-dotenv, requests. LLMs: Google Gemini API,
Mistral AI API, Groq API (free tiers). AI coding assistants were used during development.

## Known limitations

- Free-tier rate limits: Gemini ≈15 req/min, ministral-14b 30/min, ministral-8b 188/min, Groq ≈5/min
  (token-limited). Bursts beyond all of them fall back to the rule-based parser (less accurate on
  unusual wording).
- LLM latency depends on the providers (typically 1–3 s).
- Windows crossing midnight are expanded as start→23 plus 0→end.
- The cache is in memory and resets on restart.

## Secret handling

Keys only via environment variables / host secret settings. `.env` is gitignored and excluded from
the Docker image; no keys are logged or returned in responses (errors log only the exception type).
