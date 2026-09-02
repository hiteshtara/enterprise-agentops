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
uv run python -m app.init_db               # create tables + seed 24 demo batches
                                           # honours AGENTOPS_DATABASE_URL; idempotent
uv run uvicorn app.main:app --reload       # serve on :8000
uv run pytest                              # full suite
uv run pytest tests/test_agent.py::test_agent_blocks_write_tool_without_approval
uv run ruff check .                        # lint  (ruff is a dev dep; no config = defaults)
uv run ruff format .                       # format
```

`OPENAI_API_KEY` is needed only for real model calls. Tests inject fakes, and
`app.main` imports without a key because the OpenAI client is built lazily.

## Architecture

Request flow for `POST /agent/run`:

```
main.py            wiring + routes only; tools come from app/tool_setup.py
  └─ AgentService.run(message)                       app/agent.py
       ├─ ModelProvider.generate_with_tools(...)     app/model_provider.py
       │    OpenAI Responses API, parallel_tool_calls=False
       ├─ audit: TOOL_REQUESTED
       ├─ ToolRegistry.execute(name, args)           app/tool_registry.py
       │    raises ApprovalRequired if risk != READ and not approved
       ├─ audit: TOOL_EXECUTED  →  append to trace
       ├─ serialise_tool_result(result) → JSON back to the model
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
- **Tools are registered in `app/tool_setup.py`, not `main.py`.** Add new tools to
  `build_tool_registry(migration_store=...)`, which takes its dependencies as
  arguments so callers choose the database. `main.py` is module-level global state
  (one `Database`, the stores, the registry, the agent) but is now wiring + routes
  only.
- **Tool JSON schemas are hand-written**, not derived from the Python signatures.
  `Tool.parameters` must stay in sync with the callable by hand — a mismatch
  surfaces as a `TypeError` at call time, since `ToolRegistry.execute` does
  `tool.function(**arguments)`. `query_migration_batches` avoids drift by generating
  its enum and limit bounds from the same constants its validators use.
- **A `Tool.function` may be a bound method.** `query_migration_batches` is wired
  directly to `MigrationBatchStore.query`, which is how the tool reaches a database
  without any module-level store global.
- **Stores take an injected `Database`** (`app/database.py`). `Database` owns one
  engine + session factory for one URL; `ApprovalStore(database=...)` and
  `AuditStore(database=...)` default to the lazy process-wide `get_database()`.
  `main.py` builds one `Database()` and shares it. There is no session-per-request
  and no FastAPI `Depends`; each store method opens and closes its own session.
- **The DB URL is configurable** via `AGENTOPS_DATABASE_URL`, defaulting to
  `sqlite:///./agentops.db`. Nothing connects at import time — `get_database()` is
  lazy and `create_engine` does not open a connection, so importing app modules
  creates no file.
- **Schema creation and seeding are both explicit and opt-in.** `init_database(
  database=None, seed=False)` calls `Database.create_all()`; seeding only happens
  when `seed=True` (the `__main__` block passes it). `seed_migration_batches()`
  inserts only missing `batch_id`s, so it never duplicates or overwrites. Nothing
  runs on import.
- **The OpenAI client is lazy.** `OpenAIModelProvider.client` is a property that
  constructs `OpenAI()` on first access, so importing `app.main` needs no API key.
  Credential validation is unchanged — it just happens at first call. Don't move
  client construction back into `__init__`; a test asserts this.
- **JSON is stored as text.** `arguments_json` / `details_json` are `Text` columns
  serialized with `json.dumps`; the `.arguments` property and
  `AuditStore.list_events` decode on read.
- **Tool results are JSON-encoded for the model** by `serialise_tool_result` in
  `app/agent.py`, which falls back to `str()` only for non-serialisable values. It
  used to JSON-encode `dict` alone, which sent list-returning tools to the model as
  a Python `repr` (single quotes, `None`). Keep new tool return types JSON-safe.

### Audit event types

`TOOL_REQUESTED`, `TOOL_EXECUTED`, `APPROVAL_REQUIRED`, `APPROVAL_GRANTED`,
`APPROVAL_DENIED`. These are bare strings, not an enum — grep `audit_store.record`
before adding a new one. `GET /audit/events` returns them newest-first.

### The no-arbitrary-SQL rule

This is the hard constraint of the project, not a style preference. **Never add a
tool that accepts SQL text, a `WHERE`/`ORDER BY` fragment, a column list, or a table
name from the model.** `execute_sql(sql: str)` is explicitly forbidden — it would
turn prompt injection into SQL injection, make `ToolRisk.READ` a lie (a READ-tagged
tool could write), widen the blast radius past the one table, and reduce the audit
trail to opaque query strings.

The pattern to follow instead (see `app/migration_store.py` + `app/tool_setup.py`):

1. The model chooses only typed, closed-domain arguments — a string from an `enum`,
   an integer with `minimum`/`maximum`. `additionalProperties: false` always.
2. The SQLAlchemy statement is composed in Python, in the store.
3. Validation is enforced twice — in the JSON schema the model sees, and again at
   execution time — from shared constants so the two cannot drift.
4. Invalid values raise (`ValueError` for a bad status or out-of-range limit,
   `TypeError` for a non-int limit) rather than being coerced or ignored.

`tests/test_migration_tool.py::test_tool_schema_exposes_no_sql_surface` enforces this
generically: it rejects any free-text string argument on the tool. New database tools
should be covered by a similar assertion.

## Gotchas

- **Tests are isolated from the development database.** `tests/conftest.py` exposes
  a function-scoped `database` fixture backed by a fresh SQLite file under pytest's
  `tmp_path`. Every test starts empty and `./agentops.db` is never touched. Always
  take the fixture and pass `database=database` into the stores — a bare
  `ApprovalStore()` in a test would fall back to the development database.
- **This is a non-package project** (`[tool.uv] package = false`). There is no
  build backend and no console script; `uv sync` installs dependencies only. All
  code lives in `app/`, importable because `pythonpath = ["."]`.
- **Tool exceptions propagate to a 500.** `AgentService.run` does not catch tool
  failures, so a rejected status or limit surfaces as an unhandled error rather than
  being fed back for the model to correct. The JSON-schema enum makes this unlikely
  in practice; see the deferred-work note before relying on it.
- **`app/tools.py` still holds the legacy in-memory `MIGRATION_BATCHES` dict** used
  by `get_migration_status`. That tool is a hardcoded lookup and is *not* backed by
  the database; `query_migration_batches` is the authoritative path. Don't confuse
  the two.
- Model id defaults to `gpt-5.4-mini` via `DEFAULT_MODEL`, overridable per instance.

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
