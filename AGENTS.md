# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

**AgentGuard** is the product. `enterprise-agentops` is the repository name and does
not change.

AgentGuard is a model-independent control plane that sits between an LLM agent and
real enterprise systems — *give AI agents access to real business systems without
giving them uncontrolled power*. The model reasons and **proposes** actions;
AgentGuard decides whether they may actually execute, and records what happened.

Implemented today, that is two governance layers a plain agent loop does not have:

1. **Risk-gated tool execution** — every tool is registered with a `ToolRisk`
   (`READ` / `WRITE` / `DANGEROUS`). Anything above `READ` cannot execute until a
   human approves it out-of-band.
2. **Durable audit trail** — every tool request, execution, failure, and approval
   decision is written to SQLite so a run can be reconstructed after the fact.

The migration-batch tools are a **demo domain** (an Oracle migration failing on batch
43), not the product. Keep the runtime generic: `AgentService`, `ToolRegistry`, and
the stores must never know that migrations exist.

## Durable principles

These outlive any single milestone. Breaking one is an explicit decision, not a
drive-by refactor.

1. **Model intent is never authorization.** The model may *propose*
   `restart_service(service="payments")`. Whether that tool exists, whether the
   arguments are valid, who may call it, its risk tier, whether approval is required,
   and how it is audited are decided by AgentGuard — never inferred from the fact
   that the model asked.
2. **Never execute LLM-authored SQL**, or any other general-purpose execution
   surface. See [the no-arbitrary-SQL rule](#the-no-arbitrary-sql-rule).
3. **Validate tool arguments deterministically**, in Python — in the schema the model
   sees *and* again at execution time, from shared constants.
4. **Sensitive actions require human approval**, and the tool must not run before the
   decision is recorded.
5. **Every meaningful event is auditable** through the single `AuditStore`. Never add
   a second audit mechanism.
6. **Never expose stack traces** to the model or the API client.
7. **Tools receive the minimum authority necessary.** Prefer a narrow, typed,
   constrained tool over a general-purpose one.
8. **No secrets in source control. No development database in tests. No import-time
   side effects** in providers, stores, or seeding.
9. **Stay model-independent.** OpenAI is the first `ModelProvider`, not the design.
   Nothing OpenAI-specific belongs in `AgentService`; the loop consumes only the
   duck-typed `.output` / `.output_text` shape.

## Roadmap direction

Runtime foundation, governance, safe enterprise data access, and loop resilience are
represented in the code today. What follows, and the seams to leave open:

- **Web console** is the next product surface. The HTTP API is the contract it will
  consume, so treat response shapes as public.
- **Durable runs** (`Run` / `RunStep` / `ToolExecution`) will replace today's
  stateless request, with statuses like `RUNNING` / `WAITING_FOR_APPROVAL` /
  `COMPLETED` / `FAILED`. Approval will eventually **resume the original run** rather
  than execute one tool and return its raw result — so the current shape of
  `AgentService.resolve_approval` is provisional, not a contract to defend.
- **Policy engine.** `ToolRisk` is deliberately the *first* policy model, not the
  last; it grows into contextual policy (arguments, user, role, amount, environment,
  prior evidence). Keep that decision inside the `ToolRegistry.execute` gate rather
  than scattering risk checks into tools, routes, or the agent loop.
- **AuthN/RBAC, connectors (GitHub / AWS / REST / MCP), observability, evaluation,
  PostgreSQL + Alembic, AWS deployment** come later. Don't pre-build them — but don't
  foreclose them either. Concretely: avoid hard-coding single-user, single-agent, or
  SQLite-only assumptions, and keep `Database` the only place a URL is resolved.

## Commands

Backend runs through `uv` (Python 3.13); the console through `npm` in `frontend/`.

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

```bash
cd frontend && npm install
npm run dev                                # console on :5173 (API must be on :8000)
npm run test                               # vitest; API module is mocked
npm run typecheck && npm run lint && npm run build
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
       └─ loop until the model returns no function_call, an approval is
          required, a tool fails unexpectedly, or max_iterations is hit
```

Key structural facts that are not obvious from any single file:

- **The agent loop is single-threaded, synchronous, and bounded.**
  `parallel_tool_calls=False` means `AgentService.run` only ever looks for the
  *first* `function_call` in `response.output`. Enabling parallel tool calls would
  silently drop calls. The loop is `for _ in range(self.max_iterations)`, never
  `while True` — see the loop-failure semantics below before changing it.
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

### Loop-failure semantics

`AgentService.run` classifies every way an iteration can end. The classification is
the contract — match it when adding behaviour.

| Outcome | Audit | Loop | Returned to caller |
|---|---|---|---|
| Model returns no `function_call` | — | ends | the answer |
| `ApprovalRequired` (WRITE/DANGEROUS) | `APPROVAL_REQUIRED` | parks run | `approval_required` populated |
| `ValueError` / `TypeError` from a tool | `TOOL_FAILED` | **continues** | — model retries |
| `ToolCall.argument_error` set by the provider | `TOOL_FAILED` | **continues** | — model retries |
| Any other exception from a tool | `AGENT_FAILED` | ends; run `FAILED` | generic message |
| Iteration budget exhausted | `AGENT_MAX_ITERATIONS` | ends; run `FAILED` | "stopped after N iterations" |

Invariants that tests enforce — don't break them silently:

- **`max_iterations` bounds the number of model calls**, default
  `DEFAULT_MAX_ITERATIONS = 10`, injected via the constructor and validated `>= 1`.
  Every path that `continue`s a failed call still consumes an iteration, so a model
  stuck in a correction loop terminates. No tool executes after the budget is spent.
- **`ApprovalRequired` is not a failure.** It must never produce `TOOL_FAILED`, and
  it is caught *before* the recoverable-error handler. It is not a `ValueError`, so
  the ordering is defensive rather than load-bearing — keep it that way.
- **`trace` holds successful executions only.** Failures live in the audit log. This
  is deliberate: `ToolTrace` requires a `result`, so putting failures in the trace
  would change the `AgentResponse` contract.
- **Recoverable failures reach the model as `{"error": {"type", "message"}}`** built
  by `tool_failure_output` — the exception type and `str(exc)`, never a traceback.
- **Non-recoverable failures never reach the model at all.** The run ends and the
  caller gets `AGENT_FAILED_ANSWER`; the exception type and message go to the audit
  log only. The `except Exception` doing this carries a `# noqa: BLE001` and is an
  intentional safety net, not an oversight.
- **`RECOVERABLE_TOOL_ERRORS = (ValueError, TypeError)`.** A tool that raises
  something else is treated as broken. Note `ToolRegistry.execute` raises
  `ValueError` for an unknown tool name, so a hallucinated tool is recoverable and
  the model can correct it.
- **Model-provider exceptions are not caught.** An OpenAI outage propagates and
  FastAPI returns 500 — an infrastructure failure, deliberately distinct from a tool
  failure.

### Durable-run invariants

- **Every `/agent/run` creates a `Run`.** `AgentService` owns the lifecycle:
  `RUNNING` → `COMPLETED` / `FAILED`, or → `WAITING_FOR_APPROVAL` → `RUNNING` →
  `COMPLETED`, or → `CANCELLED` on rejection. Status is a `RunStatus` enum value,
  never free text.
- **Approval resumes the original run.** `resolve_approval` reloads the persisted
  conversation, executes the pending tool, appends the result, and re-enters the
  loop. The caller never resends the prompt. An approval can be resolved once.
- **Only JSON is persisted.** A run's conversation is `list[ModelMessage]` serialised
  through `to_dict()`. Never persist a provider SDK object, and never pickle.
- **`run_steps` and `audit_events` are different histories.** Steps are what the
  runtime did and what resumption needs (including `MODEL_RESPONSE`); audit events
  are what a reviewer needs. Don't merge them or mirror one into the other.
- **Approvals are never deleted.** Resolution sets `status` / `decision` /
  `resolved_at` so history stays queryable.
- **Audit events carry an indexed `run_id` column**, not a key buried in
  `details_json`, so the timeline can be filtered per run.
- **The iteration budget is per drive, not per run.** A resumed run starts a fresh
  `max_iterations` budget; each segment is bounded, the whole run is not.

### Provider-neutral protocol

- **`AgentService` and `ToolRegistry` must never reference a vendor SDK type.** They
  speak only `app/protocol.py`: `ToolDefinition`, `ToolCall`, `ModelResponse`,
  `ModelMessage`. `ToolRegistry.definitions()` returns `ToolDefinition`, not JSON.
- **`ModelProvider` owns all translation.** `OpenAIModelProvider` converts in both
  directions; a new vendor is a new subclass and nothing else.
- **A provider never raises on model-authored garbage.** Unparsable tool arguments
  become a `ToolCall` with empty `arguments` and an `argument_error`, which the
  runtime turns into a recoverable `TOOL_FAILED`.

### Audit event types

`TOOL_REQUESTED`, `TOOL_EXECUTED`, `TOOL_FAILED`, `APPROVAL_REQUIRED`,
`APPROVAL_GRANTED`, `APPROVAL_DENIED`, `AGENT_FAILED`, `AGENT_MAX_ITERATIONS`. These
are bare strings, not an enum — grep `audit_store.record` before adding a new one.
`GET /audit/events` returns them newest-first.

Every event written during a run carries that run's `run_id`.

`TOOL_REQUESTED` is written *before* execution, so a failed call has a
`TOOL_REQUESTED` with no matching `TOOL_EXECUTED`. The one exception is a tool call
the provider could not parse: that is detected before the audit, so the path records
`TOOL_FAILED` with `arguments: null` and no `TOOL_REQUESTED`.

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

### Connector invariants

- **A connector never lets the model name a provider identifier.** The model chooses
  a slug from a closed enum; the connector resolves ids server-side. Never add a
  `property_id`-style argument to a tool schema.
- **Connector results are constructed, never forwarded.** Build the response object
  field by field from named upstream fields. No passthrough, no `**rest`, no
  `dict(response)` — an upstream payload that grows a guest name must not be able to
  reach the trace, audit, metrics or browser.
- **Fail closed, structurally.** A provider failure returns a result that omits the
  answer key entirely (no `available`), rather than a default value. Argument
  validation raises (recoverable, the model corrects); provider failure returns
  unknown (not an argument error).
- **A connector is optional.** Missing credentials omit its tools from the registry
  rather than registering them broken — the registry is what the model is told it can
  do. AgentGuard's own demo must keep working with no connector configured.
- **Credentials come from one resolver function** and are never stored on an
  instance, logged, audited, or returned. Importing the app must not read one.
- **Read-only connectors have no write method**, not merely an unused one. Absence is
  the safety property.
- **Supported provider APIs only.** A connector talks to the vendor's documented,
  credentialed API. Never a private application endpoint, a browser cookie, a human
  session token, or a scraped dashboard API — those authenticate as a person, have no
  contract, and turn the audit trail into a record of something that can silently
  change shape. Browser inspection is legitimate for *discovery*; it is never the
  production authentication model. Reversing this is an explicit architecture and
  security decision, recorded in the connector's reference doc.

### Retrieved-context invariants

Retrieval puts past data in front of a model. These hold wherever that happens.

- **Retrieved history is example, never fact.** State the authority order
  explicitly — current authoritative data, then the current conversation, then
  retrieved history, then the model's own knowledge — and carry it with the
  content, not only in a prompt somewhere upstream. A retrieved answer may never
  override a current rule, a live tool result, or something said in this thread.
- **Sanitize before persistence, patterns before identities.** Structural
  patterns take whole values; known identities then remove the bare names no
  pattern can see. The reverse order breaks the patterns — redacting a name
  inside an address leaves the domain behind. The schema itself should have no
  column that could hold an identifier or a raw payload.
- **Retrieval is enrichment, never a dependency.** An absent, empty or failing
  index leaves the feature working exactly as it did without it.
- **Personal history is not a model-browsable tool.** The application decides
  what is relevant and supplies it. A tool that lets a model search other
  people's conversations is a different product with a different risk profile.
- **Retrieved content is not an audit event.** Audit records what was done, not
  everything that was read.

### Irreversible-action invariants

Outbound actions that a third party can see cannot be un-sent. These hold for any
such action, not only Lodgify messaging — see `docs/LODGIFY_API.md` for the evidence
that produced them.

- **Never auto-retry an externally-visible write.** Without a provider idempotency
  key, a timeout is ambiguous: the action may already have happened. Retrying risks
  doing it twice, to a real person. No retry middleware, no "transient error" retry.
- **Three outcomes, not two.** `CONFIRMED_SENT`, `CONFIRMED_FAILED`, and
  `UNKNOWN_SEND_STATE` for the ambiguous case. Unknown is not failure and is **not
  safe to retry** — it requires human review, and the result must tell the model so
  in words, not merely by status.
- **Verify by re-reading, not by trusting the response.** A provider that returns no
  identifier cannot be correlated from its own response. Snapshot before, act once,
  re-read after, diff. Never assume one action produced one row, and when correlation
  is ambiguous return unknown rather than guessing.
- **A field that looks like a delivery signal usually is not.** Confirm what a status
  field means against independent evidence before any code branches on it. Interpret
  delivery only from a field proven to carry it.
- **Fields that decide who an action appears to come from, or whether anyone is
  notified, are pinned server-side.** The model never supplies a sender type, a
  notification flag, or anything else that changes an action's visibility or
  attribution.
- **The approved text is the sent text.** Whatever a human approved is transmitted
  byte-for-byte. Validation rejects; it never silently rewrites after approval.

### Observability invariants

- **Provider usage is normalised inside `ModelProvider`.** `ModelUsage` /
  `model_name` / `provider_request_id` come out already neutral; no vendor field
  name or SDK object may reach `AgentService`.
- **Durations use a monotonic clock** (`app/timing.Stopwatch`), never wall-clock
  subtraction. The clock is injectable so tests never sleep. Timestamps are stored
  for display and ordering only.
- **Unknown never becomes zero.** An unreported token count or an unpriced model
  stays `None` through the store, the API and the UI. `$0.00` claims a call was
  free; `$—` says we do not know.
- **Pricing lives only in `app/pricing.py`**, as explicit entries with an "as of"
  date. Never infer a price by prefix or family, and never fetch pricing at runtime.
- **Approval wait is not execution time.** Tool timing wraps the callable only; a
  run parked for a human records no tool execution.
- **Audit and observability stay separate stores.** A metric never becomes an audit
  event, and audit is not a time-series store.
- **Metric schema changes go through Alembic** like any other, and metric columns
  are nullable so an unknown can be stored as one.

### Schema migration invariants

- **Alembic is authoritative for persistent schema evolution.** Never evolve a
  database that holds data you keep with `create_all()` — it adds tables but not
  columns.
- **`create_all()` is for ephemeral test databases only.** The pytest fixtures build
  a throwaway SQLite file per test directly from the models; that is deliberate and
  fine. `app/init_db.py` is a deprecated compatibility wrapper.
- **Migrations never seed business or demo data.** Structure only. Seeding is a
  separate explicit step (`python -m app.seed_data`).
- **Every generated migration must be read before it is run or committed.**
  `--autogenerate` is a first draft: it misses data migrations and renders some
  changes destructively.
- **Stores contain no migration logic**, and `alembic/env.py` is the only place
  Alembic is configured. It resolves the URL through
  `app.database.resolve_database_url()` so migrations and the app cannot disagree —
  but a caller-supplied `sqlalchemy.url` wins, which is what keeps tests off the
  development database.
- **New model modules must be imported in `alembic/env.py`**, or autogenerate will
  propose dropping their tables.

### Identity and authorization invariants

- **`app/identity.py` is the only place role→permission is decided.** Never write
  `if user.role == "ADMIN"`; call `user.can(Permission.X)` or use a `requires(...)`
  dependency. Adding a role means adding a row to `ROLE_PERMISSIONS`.
- **Identity comes from the token, never from a request body.** No route may read a
  `user_id`, `email`, or `role` field from a payload. `AgentService` receives an
  opaque `actor_user_id` from the route and never parses a header or a token.
- **Approval authority is derived from the approval's own risk tier** via
  `authorization.permission_for_risk`. An unknown tier falls back to
  `APPROVE_DANGEROUS` — fail closed. Rejecting requires the same permission as
  approving: blocking an action is the same authority as releasing it.
- **Authorization is checked before anything is resolved or executed.** A denied
  attempt leaves the approval `PENDING`, executes no tool, and writes an
  `AUTHORIZATION_DENIED` audit event naming the actor and the missing permission.
- **Only hashes are persisted, and no response can emit one.** `user_to_dict` is the
  single API projection of a user and has no branch that includes `password_hash`.
- **`app/security.py` owns all cryptography.** bcrypt for passwords, PyJWT for
  tokens; never hand-roll either, and never surface a library error to a caller —
  every failure becomes `InvalidToken` / "Not authenticated."
- **`AGENTGUARD_BCRYPT_ROUNDS` exists only to keep tests fast.** Production uses the
  default cost factor; never lower it in a deployed environment.
- **UI permission checks are convenience, not security.** The console hides what a
  role cannot do; the backend re-checks every request and is the only authority.

### Console architecture

`frontend/` is React 19 + TypeScript on Vite, plain CSS, react-router. No UI kit, no
global state library — don't add either without a concrete reason.

- **`src/api/agentguard.ts` is the only place the console calls the backend.** No
  component calls `fetch`. `src/api/types.ts` mirrors `app/models.py` by hand; a
  drift shows up as a typecheck failure.
- **The console never executes a tool.** Approve/Reject post to
  `/agent/approvals/{id}` and the backend decides. Never add a client-side path that
  reaches a tool directly — that would put execution authority in the browser.
- **Only `ApiError` messages reach the screen.** `client.ts` replaces 5xx bodies with
  a generic line, and `ErrorState` renders anything that is not an `ApiError` as a
  generic message. Never surface a raw exception or a server body to a user.
- **`GET /tools` exposes governance metadata; `definitions()` does not.**
  `ToolRegistry.describe()` includes `risk` for the console;
  `ToolRegistry.definitions()` deliberately omits it, because the model must not be
  told how a tool is governed. Keep those two methods distinct.
- **CORS is an explicit local-dev origin list**, never a wildcard. Production serves
  console and API from one origin.
- **The URL is the console's state store for anything shareable** — the active run
  on the Agent page and every filter. Never mirror that into component state that
  can drift; read it with `useUrlFilter` / `useSearchParams`. The browser stores no
  run content, only its id: the view is rebuilt from `GET /runs/{id}`.
- **Polling belongs to `useAsync({intervalMs, pollWhile})`**, which stops on
  terminal status, skips hidden tabs, and cleans up on unmount. Don't hand-roll a
  `setInterval` in a component, and don't poll a page with nothing active.
- **Derived run statistics are counted from persisted steps only.** There is no
  token, cost or per-call latency data in the backend yet; never display a number
  the database cannot support.
- **Tone classes are shared** between badges and other elements; a badge tone carries
  a pill background, so bare-text usages must clear it (see `.stat-value.tone-*`).

## Gotchas

- **Tests are isolated from the development database.** `tests/conftest.py` exposes
  a function-scoped `database` fixture backed by a fresh SQLite file under pytest's
  `tmp_path`. Every test starts empty and `./agentops.db` is never touched. Always
  take the fixture and pass `database=database` into the stores — a bare
  `ApprovalStore()` in a test would fall back to the development database.
- **This is a non-package project** (`[tool.uv] package = false`). There is no
  build backend and no console script; `uv sync` installs dependencies only. All
  code lives in `app/`, importable because `pythonpath = ["."]`.
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
