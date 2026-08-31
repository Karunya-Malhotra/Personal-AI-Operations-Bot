# Personal Ops Bot

A personal AI assistant whose interface is chat and whose system of record is
PostgreSQL. See `docs/ARCHITECTURE.md` for the design and `docs/adr/` for the
decisions behind it.

**Current milestone: M1B — Conversation core.** A persistent, traceable
conversation with a real LLM behind a provider boundary. No tools yet.

What works: everything from M1A (configuration, structured logging, migrations,
the dev/prod database guard, health endpoints), plus persisted conversations and
messages, an `LLMProvider` abstraction with Anthropic, Gemini and scripted
adapters selected by config, the Agent Runtime state machine with `agent_runs` /
`llm_calls` tracing and correlation ids, a startup sweep for runs a crash left in
flight, and a CLI that holds a conversation across restarts.

**What M1B deliberately does not do.** These are M1C and later, and none of them
are stubbed or partially present -- a capability the system does not have should
not appear to exist:

| Deferred | Arrives |
|---|---|
| Tool calling, the tool registry | M1C |
| The policy engine and provenance propagation | M1C |
| Confirmations (durable suspend/resume of a turn) | M1D |
| Telegram, WhatsApp | M1E / M7 |
| Memory extraction, embeddings, semantic retrieval | M4 |
| Background jobs, scheduling, workflows | M2 / M3 |
| Web research and scraping | M6 |
| Media processing | M5 |
| Proactive behaviour | M2+ |

Two limits worth knowing inside M1B itself: the context window counts
*messages*, not tokens (a token-aware budget needs a per-provider tokeniser and
lands with M4), and the reaper closes an interrupted turn rather than resuming
it -- resumption needs the M1D confirmation machinery.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env
make up        # start PostgreSQL
make install   # create .venv from uv.lock (exact pinned versions)
make migrate   # create system_settings and stamp the database as 'dev'
make cli       # an echo REPL, with boot checks
make api       # http://127.0.0.1:8000/health
make check     # lint + types + import contracts + tests
```

No `source .venv/bin/activate` step: every target runs through
`uv run --locked`, so `make test` uses the locked versions whether you run it
here or CI runs it.

## Language model

`LLM_PROVIDER` selects the adapter: `anthropic`, `gemini`, or `fake`. Only the
selected provider's key is read, and startup fails with a clear message if it is
missing.

`fake` is the default and is a first-class mode, not a test-only hack: it runs
the whole application against a scripted model with no key, no network and no
cost. Start there.

Switching vendors is an `.env` edit. Nothing above `app/providers/llm/` knows
which one answered -- an import contract enforces that no module outside that
package may import a vendor SDK.

## Dependencies

`pyproject.toml` declares what we depend on, as ranges, and is the file you
edit. `uv.lock` records the exact versions those ranges resolved to -- every
transitive dependency, pinned and hashed -- and is committed.

Ranges alone are not reproducible: `fastapi>=0.115` means a clone today and a
clone next month can install different code from identical sources, so a build
can break with no commit to blame. The lockfile is what makes "it works on my
machine" a checkable claim.

```bash
make lock      # re-resolve after editing dependencies; commit the result
```

`--locked` on every target means a stale `uv.lock` fails the build instead of
being silently re-resolved.

## Tests

```bash
make test      # unit tests always; integration tests need a real PostgreSQL
```

Integration tests get their database one of two ways. By default they start one
with testcontainers, which needs Docker. If Docker is unavailable -- or you
already have a server running -- point them at it instead:

```bash
AIOPS_TEST_DATABASE_URL="postgresql+asyncpg://aiops:aiops@127.0.0.1:5432/postgres" make test
```

Either way each test module gets a freshly created, empty database, because
some tests assert on what a database looks like *before* migrations run.

## Layout

```
app/core/           pure kernel: errors, Clock, Environment. No frameworks.
app/config/         Settings (pydantic-settings). Read only by bootstrap.
app/observability/  structlog setup, correlation ids, redaction.
app/db/             engine, session scope, models, environment guard.
app/api/            FastAPI routers.
app/domains/        business logic. No LLM, no prompts. (conversations)
app/providers/llm/  outward adapters: anthropic, gemini, fake, pricing, factory.
app/agent/          the Agent Runtime: state machine, context builder, reaper.
app/bootstrap.py    composition root; boot-time invariants.
app/main.py         api entrypoint.
app/cli.py          cli entrypoint.
```

Dependencies point inward only, and `lint-imports` enforces it -- seven
contracts, each verified to actually fail when violated rather than assumed to
bind. The load-bearing ones: `domains` and `providers` are independent, no
module outside `app/providers/llm/` may import a vendor SDK, and `core` stays
free of frameworks and drivers.
