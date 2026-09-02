# Enterprise AgentOps

A FastAPI service that wraps a self-built LLM tool-calling agent in the controls an
enterprise actually needs: risk-tiered tools, human approval for anything that
writes, a durable audit trail, and database access the model cannot abuse.

The agent runtime is implemented directly against the OpenAI Responses API. There is
no LangChain, LangGraph, or LlamaIndex — the loop is deliberately hand-written so the
governance boundaries are explicit and inspectable.

## Architecture

```
                       HTTP client
                            |
            +---------------v----------------+
            |          FastAPI app           |   app/main.py
            |  /health                       |   (composition + routes only)
            |  /agent/run                    |
            |  /agent/approvals/{id}         |
            |  /audit/events                 |
            +---------------+----------------+
                            |
            +---------------v----------------+
            |         AgentService           |   app/agent.py
            |  - owns the reasoning loop     |
            |  - knows NO individual tool    |
            +--+----------+----------+-------+
               |          |          |
     schemas() |   execute|          | record()
               |          |          |
    +----------v--+  +----v---------------+  +--v---------------+
    | ToolRegistry|  |  Risk gate         |  |   AuditStore     |
    |  (built by  |  |  READ -> run       |  | TOOL_REQUESTED   |
    | tool_setup) |  |  WRITE/DANGEROUS   |  | TOOL_EXECUTED    |
    +------+------+  |    -> ApprovalReq. |  | APPROVAL_*       |
           |         +----+---------------+  +--------+---------+
           |              |                           |
           |              v                           |
           |     +--------+---------+                 |
           |     |  ApprovalStore   |                 |
           |     | pending_approvals|                 |
           |     +--------+---------+                 |
           |              |                           |
    +------v--------------v---------------------------v-------+
    |                      Database                           |  app/database.py
    |        injectable engine + session factory              |
    |          sqlite:///./agentops.db (default)              |
    +---------------------------+-----------------------------+
                                |
                    +-----------v------------+
                    |  MigrationBatchStore   |  app/migration_store.py
                    |  SQLAlchemy expressions|
                    |  built in Python only  |
                    +------------------------+
```

## The agent loop

`AgentService.run` is a synchronous loop, one tool call per iteration:

1. Send the conversation plus `tool_registry.schemas()` to the model.
2. If the response contains no `function_call`, return the answer, the execution
   trace, and `approval_required: null`.
3. Otherwise audit `TOOL_REQUESTED`, then ask the registry to execute the call.
4. A `READ` tool runs immediately. A `WRITE` or `DANGEROUS` tool raises
   `ApprovalRequired`; the loop persists a pending approval, audits
   `APPROVAL_REQUIRED`, and returns early without executing anything.
5. On success, audit `TOOL_EXECUTED`, append to the trace, feed the JSON result back
   to the model, and iterate.

`parallel_tool_calls=False` is intentional. The loop reads only the first
`function_call` in a response, so one call per iteration keeps the audit trail and
the approval gate strictly ordered. Enabling parallel calls would silently drop work.

## Tool registry and risk governance

Every tool is registered with a `ToolRisk`:

| Risk | Meaning | Behaviour |
|---|---|---|
| `READ` | Observes state | Executes immediately |
| `WRITE` | Changes state | Blocked until a human approves |
| `DANGEROUS` | Destructive / high blast radius | Blocked until a human approves |

The gate lives in `ToolRegistry.execute`, not in `AgentService`. The agent never
learns the name of any specific tool — it discovers everything through
`tool_registry.schemas()`. Registration is assembled in `app/tool_setup.py` by
`build_tool_registry(migration_store=...)`, which receives its dependencies rather
than reaching for global state.

Registered tools: `calculator`, `get_migration_status`, `restart_migration` (WRITE),
`query_migration_batches`.

## Human approval flow

```
POST /agent/run  {"message": "Restart migration batch 43."}

  -> model requests restart_migration
  -> ToolRegistry raises ApprovalRequired (risk = WRITE)
  -> row written to pending_approvals, APPROVAL_REQUIRED audited
  -> 200 {"answer": "Approval required before executing restart_migration.",
          "trace": [],
          "approval_required": {"approval_id": "...", "tool": "restart_migration",
                                "arguments": {"batch_id": 43}, "risk": "WRITE"}}

POST /agent/approvals/{approval_id}  {"approved": true}

  -> tool executes with approved=True
  -> APPROVAL_GRANTED + TOOL_EXECUTED audited, pending row deleted
  -> 200 {"approval_id": "...", "approved": true, "tool": "restart_migration",
          "result": {...}}
```

Denying instead deletes the pending row, audits `APPROVAL_DENIED`, and executes
nothing. Approvals survive a restart because they live in SQLite, not memory.

Resolving an approval executes exactly the one pending call and returns its result.
It does not re-enter the model loop, and conversation state is not persisted.

## Audit logging

Every tool request and every approval decision is appended to `audit_events`:
`TOOL_REQUESTED`, `TOOL_EXECUTED`, `APPROVAL_REQUIRED`, `APPROVAL_GRANTED`,
`APPROVAL_DENIED`. `GET /audit/events` returns them newest first. There is exactly
one audit mechanism; nothing writes an audit record outside `AuditStore`.

A database query produces both a `TOOL_REQUESTED` record (with the exact arguments
the model chose) and a `TOOL_EXECUTED` record (with the rows returned), so any answer
the agent gives can be reconstructed from the audit table alone.

## Safe database tool design

`query_migration_batches` gives the agent real access to the migrations database
while making SQL injection structurally impossible.

**The model chooses arguments. It never writes a query.**

```jsonc
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "enum": ["SUCCESS", "FAILED", "RUNNING", "PENDING"]
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100
    }
  },
  "required": [],
  "additionalProperties": false
}
```

That schema is the entire attack surface. The only two values the model can influence
are a string drawn from a closed enum and an integer in `[1, 100]`. The SQLAlchemy
statement — table, columns, `WHERE`, `ORDER BY`, `LIMIT` — is composed in Python
inside `MigrationBatchStore.query`, which the model cannot reach.

Both constraints are enforced twice: once by the JSON schema the model sees, and again
by `validate_status` / `validate_limit` at execution time. The schema is generated
from the same constants the validators use, so the two cannot drift apart. An
unrecognised status is rejected with an error naming the allowed values rather than
being silently ignored or passed through.

### Why arbitrary LLM-generated SQL is prohibited

A tool like `execute_sql(sql: str)` is deliberately **not** offered. Accepting query
text — or a `WHERE` fragment, a column list, an `ORDER BY` clause, or a table name —
from a model reintroduces every problem the rest of this system exists to prevent:

- **Prompt injection becomes SQL injection.** Untrusted text anywhere in the context
  (a batch's error message, a user's question) can steer the model into emitting
  `DROP TABLE` or `UNION SELECT` against another table. Nothing downstream can tell
  a malicious generated query from a legitimate one, because both are just valid SQL.
- **The risk tier stops meaning anything.** `ToolRisk.READ` is only a truthful label
  if the tool provably cannot write. A tool that runs model-authored SQL is
  `DANGEROUS` no matter what it is registered as, and the approval gate is bypassed
  because the string arrived inside a READ call.
- **The blast radius is the whole database**, not one table. A constrained tool can
  only ever return migration batch rows; a SQL executor can read approvals, audit
  records, or anything else the connection can see.
- **Auditing degrades.** `{"status": "FAILED", "limit": 5}` is reviewable at a glance
  and reproducible. An arbitrary SQL string has to be parsed and reasoned about
  before anyone can say what the agent actually did.
- **Denylists do not hold.** Blocking `DROP`/`DELETE` by string matching loses to
  comments, casing, encodings, and stacked statements. Not accepting SQL at all has
  no bypass.

The cost is that each new question shape needs a new typed parameter or a new tool.
That is the intended trade: capability is added deliberately, in reviewed Python,
instead of being improvised by a model at runtime.

### Authoritative data vs. model reasoning

The database is the source of truth for *what happened*. The model is only allowed to
decide *which question to ask* and to phrase the result.

- Batch outcomes, error strings, record counts, and timings come from
  `migration_batches` and are passed to the model verbatim as JSON.
- The tool description tells the model to query rather than recall, so it does not
  answer from pretraining or from earlier turns.
- The execution trace returned by `/agent/run` carries the exact rows the tool
  returned, so a caller can verify the final answer against the data instead of
  trusting the model's summary.

### Query flow

```
POST /agent/run  {"message": "Show me failed migration batches."}
        |
        v
  model emits function_call
     query_migration_batches {"status": "FAILED", "limit": 20}
        |
        v
  audit TOOL_REQUESTED
        |
        v
  ToolRegistry.execute -> risk READ -> no approval needed
        |
        v
  MigrationBatchStore.query(status="FAILED", limit=20)
     validate_status / validate_limit
     select(MigrationBatchRecord)
        .where(status == "FAILED")
        .order_by(created_at DESC, batch_id DESC)
        .limit(20)
        |
        v
  list[dict] of real rows
        |
        v
  audit TOOL_EXECUTED  (arguments + rows)
        |
        v
  rows serialised as JSON -> back to the model
        |
        v
  final natural-language answer + execution trace
```

### Example prompts

- "Show me failed migration batches."
- "Show me the 5 most recent successful migrations."
- "What migration batches failed?"
- "Which migrations are still running?"
- "How many recent failed batches are there?"

## Database

`Database` (`app/database.py`) owns one engine and session factory for one URL and is
injected into the stores, so tests can point at an isolated file without patching
globals. The URL comes from `AGENTOPS_DATABASE_URL`, defaulting to
`sqlite:///./agentops.db`. Nothing connects at import time.

| Table | Purpose |
|---|---|
| `migration_batches` | Authoritative batch records read by `query_migration_batches` |
| `pending_approvals` | Human-in-the-loop approvals awaiting a decision |
| `audit_events` | Append-only record of tool and approval activity |

`migration_batches` columns: `id`, `batch_id` (unique, indexed), `status` (indexed),
`records`, `duration_seconds`, `error` (nullable), `created_at` (ISO-8601 UTC string).

Schema creation and seeding are both explicit and idempotent — neither happens on
import. `init_database()` creates tables; seeding is opt-in via `seed=True`, and
`seed_migration_batches()` inserts only batch IDs that are missing, so re-running it
never duplicates or overwrites rows.

## Running locally

```bash
uv sync

export OPENAI_API_KEY=sk-...            # only needed for real model calls
uv run python -m app.init_db            # create tables + seed 24 demo batches
uv run uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

```bash
curl -s localhost:8000/health
curl -s localhost:8000/agent/run -H 'content-type: application/json' \
  -d '{"message": "Show me failed migration batches."}'
curl -s localhost:8000/audit/events
```

Point at a different database with `AGENTOPS_DATABASE_URL=sqlite:///./scratch.db`.

## Running tests

```bash
uv run pytest -v          # deterministic; never calls OpenAI
uv run ruff format .
uv run ruff check .
```

Tests never touch `./agentops.db`. A `database` fixture builds a fresh SQLite file
under pytest's `tmp_path` for each test, and tests seed their own data explicitly.
Model behaviour is supplied by fake `ModelProvider` implementations, so no API key is
required — and `app.main` imports without one, because the OpenAI client is
constructed lazily on first use.
