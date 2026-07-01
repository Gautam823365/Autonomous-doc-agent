# Autonomous Document Agent

A small autonomous agent that takes a natural-language request, plans its
own steps, executes them, and produces a polished Word (`.docx`) document
— built for the 60-minute Python AI Engineer challenge.

```
POST /agent  {"request": "..."}
   │
   ├─ 1. planner.create_plan()    → autonomous TODO list + document/section plan
   ├─ 2. executor.execute_plan()  → generates content for each planned section
   ├─ 3. doc_generator.build_docx() → renders final .docx
   └─ 4. AgentResponse            → plan, task results, assumptions, download link
```

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env        # optional — works with zero keys, see below
uvicorn app.main:app --reload --port 8000
```

In another terminal:

```bash
python test_client.py       # runs the two required test inputs
# or
curl -X POST http://localhost:8000/agent \
  -H "Content-Type: application/json" \
  -d '{"request": "Create a project plan for migrating our database to AWS"}'
```

The response includes a `download_url` (e.g. `/download/<file>.docx`) you can
open in a browser, plus the agent's full task list and execution trace.

### LLM provider (free / local — no paid keys required)

- **Primary:** [Groq](https://console.groq.com/keys) free tier (`GROQ_API_KEY` in `.env`). Fast, generous free quota, OpenAI-compatible API.
- **Secondary:** [Ollama](https://ollama.com) running locally (`ollama pull llama3.1`) — zero network dependency.
- **Tertiary:** a deterministic, keyword-classified template generator built into the agent itself.

You don't need any of these configured to run the demo — see below.

## Architecture

| Module | Responsibility |
|---|---|
| `app/models.py` | Pydantic contracts for every stage of the pipeline (request, plan, task results, response). |
| `app/llm_client.py` | Multi-provider LLM client with retry + fallback (the mandatory improvement — see below). |
| `app/planner.py` | Autonomous planning: classifies the request into a document type, decides sections, surfaces assumptions, builds the TODO list. |
| `app/executor.py` | Executes the plan step-by-step (one task = one section), recovering per-section if generation fails. |
| `app/doc_generator.py` | Renders the plan + generated content into a styled `.docx` via `python-docx`. |
| `app/main.py` | FastAPI app: `POST /agent`, `GET /download/{file}`, request validation/guardrails. |

**Why this shape:** planning and execution are separated on purpose. The
planner's only job is to decide *what* the document should contain (and to
own the "autonomous decision-making" the assignment asks for); the executor's
only job is to *carry out* that plan reliably. That separation is what lets
the retry/fallback logic live in one place (`llm_client.py`) and apply
uniformly to both stages, instead of being duplicated.

## Mandatory Engineering Improvement: Retry & Fallback Logic

Free/local LLMs are the whole premise of this assignment, but they're also
the least reliable part of the stack — rate limits, cold local models,
dropped connections. I chose retry & fallback because a "smart" plan that
crashes the instant Groq returns a 429 isn't actually autonomous.

**What it does, concretely (`app/llm_client.py`):**
1. Every LLM call is retried up to `N` times with exponential backoff
   (handles transient errors: timeouts, rate limits, connection resets).
2. If the primary provider (Groq) is exhausted, the client automatically
   fails over to a structurally different provider (local Ollama) — so a
   Groq outage and a "no Ollama installed" failure don't compound.
3. If *every* provider is unavailable, the agent doesn't 500 — `planner.py`
   and `executor.py` each fall back to deterministic generation (the
   planner still classifies the request by keyword into the right document
   type; the executor substitutes a clearly-labeled placeholder for just
   the affected section) so a complete, valid `.docx` is always produced.
4. Every `TaskResult` in the API response records which provider actually
   served it (`groq`, `ollama`, or `fallback_template`) and whether it
   needed recovery — so failures are visible and debuggable, not silent.

**Why this, not the alternatives:** Tool calling and RAG would add
capability but don't address the actual fragility of "free tier LLM as a
required dependency." Retry/fallback is the improvement that makes the
*rest* of the system (autonomous planning, multi-step execution) trustworthy
under real-world conditions — which matched the assignment's own framing
("free or locally runnable LLM... so the assignment can be completed
without purchasing API credits").

## Two Test Inputs

`test_client.py` runs both automatically:

1. **Standard:** "Create a project plan for migrating our customer database
   from on-premise MySQL to AWS RDS PostgreSQL, for a team of 4 engineers
   over 6 weeks, to present to engineering leadership." → clean, unambiguous
   request; agent plans a 5-section Project Plan and executes directly.
2. **Complex/ambiguous:** "We need something for the board about the new
   product thing... I don't have exact figures yet... not sure if it's a
   proposal or a report, you decide." → missing data, undecided document
   type, vague scope. The agent picks a document type itself, generates
   plausible mock figures, and records its assumptions explicitly (both in
   the API response and in an "Agent Assumptions" section of the `.docx`
   itself) rather than blocking on clarification.

Both were run end-to-end against this codebase with zero LLM providers
configured (sandboxed environment, no outbound access to Groq/Ollama) to
validate the tertiary fallback path — both produced complete, valid `.docx`
files, with the planner correctly classifying "project plan" vs "proposal"
purely from keywords. With a real `GROQ_API_KEY` set, the same flow produces
fully LLM-written content instead of placeholders — no code changes needed.

## Talking Points for the Video

**Live demo (3–4 min):** start the server, run `test_client.py`, narrate the
printed task list + step-by-step execution trace for both requests, then
open both generated `.docx` files to show the title page, assumptions box,
and section content.

**What you built (2–3 min):** walk through the four-module architecture
table above; emphasize that the planner produces its *own* TODO list (the
`tasks` field) rather than following a hardcoded script, and that this list
is shown to the user, not just used internally.

**Debugging insight (1–2 min):** a true debugging story from building this:
LLMs frequently wrap JSON responses in markdown code fences or prose
("Here's the plan:\n```json\n{...}```"), which broke naive `json.loads()`
calls in the planner. Root cause: treating the LLM's output as if it were a
structured API response instead of untrusted text. Fix: `_extract_json()`
in `llm_client.py` strips code fences first, then falls back to regex-
extracting the largest `{...}`/`[...]` span before parsing — and any
failure there is caught by the same retry/fallback chain rather than
crashing the request.

**Tradeoff discussion (1–2 min):** *Autonomous Planning vs. Deterministic
Workflows.* The planner lets the LLM freely decide section structure and
count, which produces better-tailored documents but makes output shape
unpredictable (sometimes 4 sections, sometimes 8) — harder to build a fixed
UI or strict schema validation around. The alternative (a fixed set of
section templates the LLM only fills in) is far more predictable and easier
to test, at the cost of every document looking the same regardless of the
request. This codebase leans autonomous for content (planner decides
sections) but keeps a deterministic safety net (the fallback templates) so
the *worst case* is still bounded and predictable — a middle ground rather
than picking one extreme.

## Project Structure

```
agent_project/
├── app/
│   ├── main.py            # FastAPI app & orchestration
│   ├── planner.py         # autonomous planning
│   ├── executor.py        # step execution
│   ├── llm_client.py      # retry & fallback LLM client (mandatory improvement)
│   ├── doc_generator.py   # python-docx rendering
│   └── models.py          # pydantic schemas
├── test_client.py         # the two required test inputs
├── requirements.txt
├── .env.example
└── README.md
```
