# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

`enterprise-agentops` is a FastAPI service that wraps an LLM tool-calling agent with
two governance layers that a plain agent loop does not have:

1. **Risk-gated tool execution** — every tool is registered with a `ToolRisk`
   (`READ` / `WRITE` / `DANGEROUS`). Anything above `READ` cannot execute until a
   human approves it out-of-band.
2. **Durable audit trail** — every tool request, execution, and approval decision is
   written to SQLite so the run can be reconstructed after the fact.

The migration-batch tools are a demo domain (an Oracle data migration that fails on
batch 43); the interesting code is the governance plumbing around them.

## Commands

Everything runs through `uv`; the project targets Python 3.13.

```bash
uv sync                                    # install deps (incl. dev group)
uv run python -m app.init_db               # create SQLite tables — required before first run
                                           # honours AGENTOPS_DATABASE_URL; idempotent
uv run uvicorn app.main:app --reload       # serve on :8000
uv run pytest                              # full suite
uv run pytest tests/test_agent.py::test_agent_blocks_write_tool_without_approval
uv run ruff check .                        # lint  (ruff is a dev dep; no config = defaults)
uv run ruff format .                       # format
```

`OPENAI_API_KEY` must be set for anything that actually calls the model. The tests
do not need it — they inject fakes.

## Architecture

Request flow for `POST /agent/run`:

```
main.py            builds the singletons at import time and wires them together
  └─ AgentService.run(message)                       app/agent.py
       ├─ ModelProvider.generate_with_tools(...)     app/model_provider.py
       │    OpenAI Responses API, parallel_tool_calls=False
       ├─ audit: TOOL_REQUESTED
       ├─ ToolRegistry.execute(name, args)           app/tool_registry.py
       │    raises ApprovalRequired if risk != READ and not approved
       ├─ audit: TOOL_EXECUTED  →  append to trace
       └─ loop until the model returns no function_call
```

Key structural facts that are not obvious from any single file:

- **The agent loop is single-threaded and synchronous by design.**
  `parallel_tool_calls=False` means `AgentService.run` only ever looks for the
  *first* `function_call` in `response.output`. Enabling parallel tool calls would
  silently drop calls.
- **Approval is an exception, not a return value.** `ToolRegistry.execute` raises
  `ApprovalRequired`; `AgentService.run` catches it, persists a
  `PendingApprovalRecord`, and returns early with `approval_required` populated and
  the partial `trace`. The conversation state is *not* persisted — resuming via
  `POST /agent/approvals/{id}` executes the single pending tool and returns its
  result directly. It does not re-enter the model loop or produce a new answer.
- **Everything in `main.py` is module-level global state.** The registry, stores,
  and `AgentService` are constructed once at import. Registering a new tool means
  adding a `tool_registry.register(Tool(...))` block in `main.py` alongside the
  function in `app/tools.py`.
- **Tool JSON schemas are hand-written**, not derived from the Python signatures.
  `Tool.parameters` in `main.py` must stay in sync with the function in
  `app/tools.py` by hand — a mismatch surfaces as a `TypeError` at call time,
  since `ToolRegistry.execute` does `tool.function(**arguments)`.
- **Stores take an injected `Database`** (`app/database.py`). `Database` owns one
  engine + session factory for one URL; `ApprovalStore(database=...)` and
  `AuditStore(database=...)` default to the lazy process-wide `get_database()`.
  `main.py` builds one `Database()` and shares it. There is no session-per-request
  and no FastAPI `Depends`; each store method opens and closes its own session.
- **The DB URL is configurable** via `AGENTOPS_DATABASE_URL`, defaulting to
  `sqlite:///./agentops.db`. Nothing connects at import time — `get_database()` is
  lazy and `create_engine` does not open a connection, so importing app modules
  creates no file.
- **Schema creation is explicit.** `app/init_db.py` → `init_database(database=None)`
  calls `Database.create_all()`. Nothing auto-creates tables on import or on first
  request.
- **JSON is stored as text.** `arguments_json` / `details_json` are `Text` columns
  serialized with `json.dumps`; the `.arguments` property and
  `AuditStore.list_events` decode on read.

### Audit event types

`TOOL_REQUESTED`, `TOOL_EXECUTED`, `APPROVAL_REQUIRED`, `APPROVAL_GRANTED`,
`APPROVAL_DENIED`. These are bare strings, not an enum — grep `audit_store.record`
before adding a new one. `GET /audit/events` returns them newest-first.

## Gotchas

- **Tests are isolated from the development database.** `tests/conftest.py` exposes
  a function-scoped `database` fixture backed by a fresh SQLite file under pytest's
  `tmp_path`. Every test starts empty and `./agentops.db` is never touched. Always
  take the fixture and pass `database=database` into the stores — a bare
  `ApprovalStore()` in a test would fall back to the development database.
- **This is a non-package project** (`[tool.uv] package = false`). There is no
  build backend and no console script; `uv sync` installs dependencies only. All
  code lives in `app/`, importable because `pythonpath = ["."]`.
- Model id is hardcoded as `gpt-5.4-mini` in both `OpenAIModelProvider` methods.

## Conventions

- Imports are `from app.x import y` (absolute, package-rooted). `pythonpath = ["."]`
  in `pyproject.toml` makes this work under pytest.
- Pydantic models in `app/models.py` are the HTTP boundary; `AgentService` returns
  plain `dict`s and `main.py` splats them into the response models
  (`AgentResponse(**result)`). Keep the dict keys and the model fields aligned.
- New model backends subclass `ModelProvider` (`app/model_provider.py`) and must
  implement both `generate` and `generate_with_tools`; the tests' fakes rely on the
  response shape being duck-typed as `.output` (list of items with `.type`, `.name`,
  `.arguments`, `.call_id`) and `.output_text`.
- The code style is unusually vertical — one argument per line, trailing commas
  everywhere. Match it.
