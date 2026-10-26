# Build Prompt — College Assistant, production hardening for on-premise Windows + MS SQL

Paste everything below the line into Fable 5.1 as the opening instruction.
Work through it phase by phase. Do not paste the phases all at once.

---

## 0. What you are working on

An existing, working, well-tested Django + React system: an offline AI assistant
that answers questions about a college's records through a chat UI. It runs
entirely on the institution's own hardware. No data leaves the machine.

**Do not treat this as a greenfield build. It is a mature codebase with a large
regression suite and unusually detailed design comments. Your job is a targeted
migration and hardening pass, not a rewrite.** Read before you change. Where a
comment explains why something is the way it is, that comment is usually right
and was usually written after a real incident.

### Current state (measured, not estimated)

| | |
|---|---|
| Backend | Django 5.2 + DRF, ~15k LOC across 16 apps |
| Frontend | React 18 + Vite, ~3.5k LOC, 8 components |
| Tests | **327, all passing** against a live Postgres + Redis |
| Data store | PostgreSQL 16 |
| Vector store | Qdrant |
| Model runtime | Ollama, `qwen2.5:7b`, embeddings `nomic-embed-text` |
| Cache/sessions | Redis |
| Edge | Caddy 2 (TLS, CSP, security headers) |
| Deployment | Docker Compose, Linux, Alpine images |
| Extra service | `sync_worker` — polls Postgres, embeds changes into Qdrant |

### The answer pipeline

`orchestrator/service.py` (923 LOC) coordinates: **router** (classify question →
SQL / RAG / BOTH) → **sql_agent** (LLM writes SQL, guard validates, executes
read-only) and/or **rag_agent** (vector retrieval) → **synthesis_agent** (LLM
writes the answer) → **verification_agent** (LLM checks claims against sources).

Concurrency is gated by a semaphore: `LLM_MAX_CONCURRENCY=1`, queue timeout 25s.
Over that limit users get a graceful "the assistant is busy" message.

### Module sizes (code / tests)

```
orchestrator  3362/1181   academics 2376/224   accounts 2115/751
config        1236/0      verification_agent 1049/422   web_agent 964/304
common         738/353    experiments 635/0    sql_agent 578/164
health         576/207    institution 535/161  router_agent 527/165
synthesis_agent 522/271   administration 343/314  rag_agent 226/0  audit 152/0
```

---

## 1. Target deployment

- **One bare Windows machine, CPU only, no GPU.** It hosts everything: model
  runtime, backend, vector store, web tier.
- **Reads the college's existing MS SQL Server**, which is updated daily by other
  college systems. That database is the source of truth and is **not** ours to
  restructure.
- **Published on the college domain, reachable from the internet.**
- **20–50 concurrent users.** 20 chatting simultaneously must work well.
- Answers must be fast, accurate, and trustworthy. This is a product in daily
  use by students and staff, not a demo.

---

## 2. Non-negotiable constraints

1. **Never fine-tune, train, or adapt the model on any data.** Stock weights,
   unmodified. All grounding is retrieval at inference time. No LoRA, no
   embeddings training, no feedback loops that alter weights.
2. **No college data leaves the machine.** No external APIs, no telemetry, no
   cloud inference, no analytics SDK, no CDN fetch of user content.
3. **The assistant never invents a fact.** Every factual claim traces to a
   record it retrieved. When a lookup fails, it says the lookup failed — it must
   never report "no such record exists". This distinction is already implemented
   and load-bearing; preserve it exactly.
4. **Writes to the college MS SQL are forbidden.** The assistant connects with a
   read-only principal and nothing else.
5. **Keep the existing test suite green.** 327 tests pass today. A phase is not
   done while any test fails. Add tests for what you change.

---

## 3. Read this before you plan anything

**The stated latency goal is not reachable on CPU by tuning. Confront the
arithmetic first; do not spend credits discovering it.**

Measured on the current CPU-only hardware, 50 concurrent users asking 20
distinct questions:

| | |
|---|---|
| Served | **21 / 50** |
| Told "busy" | 29 |
| Errored | 0 |
| p50 / p95 / p99 | **87.2s / 178.1s / 342.7s** |
| Throughput | **3.3 answers/min** |
| Fastest possible first token | ~13s |

A 7B model on CPU produces single-digit tokens per second. Twenty people asking
at once, each needing a multi-stage pipeline with 2–4 LLM calls, is roughly
**six minutes of queue** — against a 25s queue timeout. Raising
`LLM_MAX_CONCURRENCY` does not help: the cores are already saturated: it makes
everyone slower instead of some people waiting.

ChatGPT-class latency comes from GPUs. There is no prompt, cache, or
quantisation trick that closes a 100× gap on the same silicon.

**So the goal must be met a different way, and this is the central design idea
of this whole engagement:**

> **Most questions a college assistant receives are structured lookups, not open
> reasoning. "How many faculty are in Computer Science?", "What are the fees for
> B.Tech?", "When is the DBMS exam?" have exact answers in exact rows. Serve
> those deterministically — no LLM in the request path at all — and the LLM
> becomes the exception rather than the rule.**

A deterministic lookup answers in **milliseconds**, is **always correct**, cannot
hallucinate, and is **perfectly concurrent**. If 80% of traffic is served that
way, twenty simultaneous users are trivially handled and the LLM queue only ever
holds the genuinely open-ended remainder.

This is the highest-leverage change available and it should shape everything
else. Design for it in Phase 3.

**Present the following options to the user with your recommendation before
writing migration code:**

- **A. Deterministic-first (recommended).** Intent matching + parameterised
  queries + templated natural-language answers for the common question families.
  LLM only for open-ended questions. Massive latency and accuracy win, no
  hardware spend.
- **B. Add a GPU.** A single mid-range card takes the LLM path from ~87s to a
  few seconds. Solves it in hardware. Costs money and contradicts "CPU only".
- **C. Smaller model.** `qwen2.5:3b` roughly doubles speed. Still seconds, not
  milliseconds, and costs answer quality on hard questions.
- **D. Accept queueing.** Keep the honest "busy" message. Cheapest, but does not
  meet the stated requirement.

A and C combine well. A alone probably meets the requirement. Say so plainly if
you agree, and say so plainly if you do not.

---

## 4. Target architecture

### 4.1 Host the Linux stack under WSL2 — do not port to native Windows

The single biggest credit saving available. The current stack is Docker Compose
over Alpine with `sh` scripts, a Caddyfile, and healthchecks — all tested.
Rewriting that as native Windows services means: no official Redis for Windows,
service-wrapper plumbing for every process, rewriting every script, and
abandoning a working deployment path.

**Install WSL2 + Docker Engine on the Windows box and run the existing compose
stack unchanged.** Windows is the host; the tested stack survives intact.

- Ollama: run inside WSL2, or natively on Windows reached via
  `host.docker.internal` if CPU throughput measures better. **Measure both, pick
  the faster, record the numbers.**
- Set WSL2 memory/CPU limits in `.wslconfig` explicitly. Default caps will
  starve the model.
- Windows Firewall: expose only 80/443. Nothing else reaches the network.

Only if WSL2 is refused by college IT should you cost out the native-Windows
port — and say clearly that it is weeks of work and loses test coverage.

### 4.2 Two databases, not one

Do not migrate the application's own storage to MS SQL. Split by ownership:

| Store | Holds | Access |
|---|---|---|
| **MS SQL (college)** | departments, faculty, programs, courses, offerings, schedules, exams, fees, students, enrolments, attendance, results, payments | **read-only, ours to query and never to write** |
| **PostgreSQL (ours)** | accounts, sessions, conversation history, audit log, query profiles, cached answers | full control, local |

Reasons: the college DB is not ours to add tables to; keeping our own store
means their maintenance windows do not sign everyone out; and it preserves the
existing accounts/audit/orchestrator models untouched, which is most of the
credit saving in this phase.

Implement with a Django database router. `academics` models → MS SQL (managed =
False, read-only). Everything else → Postgres. Migrations must never target the
MS SQL connection.

### 4.3 MS SQL migration surface — the exact files

These are Postgres-coupled today and every one must be handled. This list is
complete; do not go looking for more.

| File | What must change |
|---|---|
| `backend/sql_agent/guard.py` | `DIALECT = "postgres"` → `"tsql"`. **`_cap_limit` rewrites `LIMIT`, which does not exist in T-SQL — must become `TOP` or `OFFSET/FETCH`.** This is the single most error-prone change in the migration; test it hard. |
| `backend/sql_agent/llm_client.py` | Prompt says "PostgreSQL dialect" — change to T-SQL, and update the few-shot examples to T-SQL syntax. |
| `backend/sql_agent/schema.py` | Introspection queries → `INFORMATION_SCHEMA` / `sys.tables`. |
| `backend/sql_agent/db.py` | psycopg2 → `pyodbc` / `mssql-django`. Keep the connection-pool reasoning and the separate read-only principal. |
| `backend/sql_agent/executor.py` | Statement timeouts and read-only enforcement are Postgres-specific. |
| `backend/academics/management/commands/setup_readonly_role.py` | Postgres role + GRANT → SQL Server login mapped to `db_datareader` with explicit `DENY INSERT/UPDATE/DELETE`. |
| `backend/health/management/commands/check_rag_agent_ro.py` | `has_table_privilege` → `HAS_PERMS_BY_NAME` / `fn_my_permissions`. Keep the "prove writes are rejected" test — it is valuable. |
| `sync_worker/config.py`, `poller.py`, `main.py` | `updated_at` watermark polling → **MS SQL Change Tracking** (see 4.4). |
| `backend/config/settings/base.py` | `DATABASES` gains the second connection + router. |
| `backend/gunicorn.conf.py` | Postgres reference in comments/preload. |

The SQL guard (`guard.py`) is a security control, not a formatter: it parses the
model's SQL and rejects anything that is not a single read-only SELECT within an
allowlist. **When you change its dialect you are modifying a security boundary.
Port every existing guard test to T-SQL and add T-SQL-specific injection cases
(`;`, `GO` batch separators, `xp_cmdshell`, `OPENROWSET`, `WAITFOR DELAY`).**

### 4.4 Sync every few seconds — use Change Tracking, not polling

The current worker polls `updated_at` watermarks. Against a live college
production database, every-few-seconds polling of a dozen tables is load you do
not own and should not impose.

Use **SQL Server Change Tracking** (`CHANGETABLE(CHANGES ...)`) — built for
exactly this, cheap, and returns only what changed since a version number. Ask
the college DBA to enable it on the relevant tables. Fall back to a `rowversion`
column watermark if they refuse; fall back to timed polling only as a last
resort, and lengthen the interval if so.

Requirements:
- Poll interval configurable, default 5s.
- Only changed rows are re-embedded — never a full re-index on a schedule.
- On change: invalidate the affected cached answers. **Cross-process cache
  invalidation already exists in `orchestrator/cache.py` — reuse it, do not
  reinvent it.**
- The worker must survive the college DB being unreachable and resume from its
  last version without gaps or duplicates.
- Never write to the college database, including no state table. Keep worker
  state locally.

### 4.5 Answer quality

Quality comes from retrieval and grounding, never from training.

- Keep the two-source design (structured SQL + semantic retrieval). It is why
  the system can both count exactly and describe fluently.
- Keep the fast, non-LLM numeric verification: it checks the answer's numbers
  against the retrieved rows at near-zero cost.
- **Make the LLM verification pass configurable and default it off** under the
  CPU constraint. It is an entire extra model round-trip per answer. Measure the
  accuracy it actually buys before deciding — do not delete it, gate it.
- Preserve prompt-injection defence on all retrieved content. Data from the
  database is data, never instructions.
- Always show which records an answer came from. Trust in a college setting is
  built on being checkable.

---

## 5. Remove — with justification, not on sight

The user asked to strip anything not required. Each of these needs a short
recommendation from you and the user's confirmation before deletion.

| Candidate | Case for removing |
|---|---|
| **`web_agent`** (964 LOC + 304 tests) | Fetches external web pages. On an internet-facing system reading a college database, this is inbound SSRF surface plus a prompt-injection channel from third-party content, and it adds latency. If the college does not need answers sourced from public web pages, **delete it entirely** — the security win is real. Ask first. |
| **`experiments/`** (635 LOC) | A research harness. Exclude from the deployed image. Keep in the repository. |
| **Demo commands + `seed_demo_data`** | Keep for testing, but they must be impossible to run against production. |
| **`faculty_development`** (13k-row survey) | Dataset-specific to the old demo data. Almost certainly irrelevant to the real college schema. Confirm, then remove the models, the exclusion special-cases in the sync worker, and the table notes in the SQL agent. |
| **LLM verification pass** | Gate, do not delete. See 4.5. |

Do not remove: the outage-degradation paths, the audit log, the rate limits, the
lockout, the read-only principal, the SQL guard, the cache-invalidation
machinery. Every one exists because of a specific failure and several are the
system's security controls.

---

## 6. Security — the bar for internet-facing student data

Already implemented and to be preserved: CSP with no `unsafe-inline` on scripts,
HSTS, CSRF, per-IP rate limits, login lockout, secure cookies, read-only DB
principal, prompt-injection stripping, audit logging, secret guards that refuse
to boot on default credentials, `X-Forwarded-For` spoofing resistance.

To add for public exposure:

1. **Network isolation.** The MS SQL connection must be reachable only from the
   backend, never routable from the internet tier. Firewall it explicitly.
   Credentials in a Windows-ACL'd file or DPAPI, never in the repo.
2. **TLS.** Let's Encrypt via Caddy on the college domain. Complete the HSTS ramp
   after certificate issuance is confirmed — `CADDY_HSTS_MAX_AGE` is the value
   browsers actually receive; `DJANGO_HSTS_SECONDS` is behind the proxy and does
   not reach them. Ramp both together: 3600 → 86400 → 31536000.
3. **Authentication.** Every user is a real, individually-issued account. No
   shared logins. Consider college SSO / LDAP if available — one fewer password
   store is a security win. Enforce password change on first login.
4. **Abuse.** Per-account question quotas, not just per-IP. An authenticated user
   can exhaust a CPU-bound queue far more cheaply than an anonymous one.
5. **Least privilege.** Every service runs as an unprivileged account. No process
   runs as Administrator or root.
6. **Backups.** Automated, off-machine, restore-tested. An untested backup is not
   a backup. Only our Postgres needs backing up — the college DB is theirs.
7. **Supply chain.** Pin every dependency by hash. Scan on build. No unpinned
   `latest` image tags.
8. **Log hygiene.** Audit who asked what and when, for accountability. Never log
   answer bodies containing personal data, and never log credentials.
9. **Retention.** Agree how long conversation history is kept, and enforce it
   with the existing purge command on a schedule.

---

## 7. Phased plan — stop at every checkpoint

**Complete one phase, report, and wait. Do not run phases together.** Each phase
must end with the full suite green.

**Phase 0 — Survey and decisions.** No code. Read the codebase, confirm the
Postgres-coupling list in 4.3 is complete, and put the Section 3 options and the
Section 5 removals to the user with your recommendation. Produce a written plan.
*Done when: the user has chosen a latency strategy and confirmed the removals.*

**Phase 1 — WSL2 + Docker on the Windows host.** Stand up the existing stack
unchanged on the target machine. Benchmark Ollama in WSL2 vs native Windows and
record real numbers. *Done when: the current system runs on the Windows box and
`scripts/verify_deployment.sh` passes.*

**Phase 2 — MS SQL read path.** Second database connection, router, `academics`
models mapped read-only to the real schema, SQL agent ported to T-SQL, read-only
principal created and proven. *Done when: the guard rejects every T-SQL injection
case, and real questions return correct answers from real college data.*

**Phase 3 — Deterministic fast path.** The core work. Identify the common
question families from the real schema, implement intent matching to
parameterised queries with templated answers, route the remainder to the LLM.
*Done when: the common families answer in <500ms with no LLM call, and 20
concurrent users are served without queueing.*

**Phase 4 — Change Tracking sync.** Replace watermark polling; wire cache
invalidation; prove resumption after an outage. *Done when: a row changed in MS
SQL is reflected in answers within seconds, and killing the worker mid-run loses
nothing.*

**Phase 5 — Removals and slimming.** Execute the Section 5 deletions the user
confirmed. Suite stays green. *Done when: nothing unused ships and coverage has
not dropped.*

**Phase 6 — Security and load.** Section 6 items; then a real 50-user load test
on the real hardware with the fast path live. *Done when: measured p50/p95 and
served-count meet the agreed target, published as a table.*

**Phase 7 — Handover.** Update the operator documentation to match what now
exists. Runbook for the college's own IT: start, stop, health, backup, restore,
what to do when the model is down.

---

## 8. How to work — credit discipline

The user is paying per unit of work. Waste is a defect.

1. **Read before writing.** This codebase explains itself in comments. The
   answer to "why is this like this" is usually three lines above.
2. **Smallest change that is correct.** No refactoring for taste. No renaming
   that works. No restyling. No reformatting files you are not otherwise editing.
3. **Never rewrite a working module to change one behaviour in it.**
4. **Reuse the tests.** 327 exist. Extend them; do not replace them with your own
   framework or conventions.
5. **Do not regenerate documentation wholesale.** Edit the lines that became
   untrue.
6. **When you hit an ambiguity that changes the design, stop and ask.** One
   question costs far less than a wrong architecture.
7. **Do not chase the CPU latency goal by tuning.** Section 3 settles it. If you
   find yourself adjusting concurrency numbers hoping for 20× — stop, that is the
   failure mode this document exists to prevent.
8. **Report measurements, not adjectives.** "p50 340ms over 200 requests", not
   "much faster".
9. **Say what you did not verify.** An unverified claim is worse than a gap,
   because it stops anyone else from checking.

---

## 9. Ask the user these before Phase 1

1. Can WSL2 be enabled on the Windows machine? (If no, the plan changes
   substantially and costs far more.)
2. Exact machine specs — cores, RAM, disk? Is a GPU genuinely impossible?
3. Can the DBA enable Change Tracking on the relevant MS SQL tables?
4. The real MS SQL schema — table and column names. The current models are demo
   shapes and almost certainly do not match.
5. Does the college have SSO/LDAP the assistant should authenticate against?
6. Is `web_agent` needed — should the assistant ever answer from public web
   pages, or from college records only?
7. Are students end users, or only staff? It changes the data-protection posture.
8. What is the actual acceptable answer time? "Under a second for common
   questions, under 30 seconds for complex ones" is achievable. "Always instant"
   is not.

---

## 10. Definition of done

- 20 concurrent users, common questions, **p95 under 1 second**, zero "busy".
- Complex open-ended questions answered correctly within the agreed budget.
- Zero fabricated facts across an agreed test set; failed lookups always
  reported as failures, never as absence.
- A row changed in the college MS SQL is reflected in answers within seconds.
- No write ever reaches the college database — proven, not assumed.
- Full suite green. New code covered.
- `scripts/verify_deployment.sh` passes on the real machine.
- Backups run and a restore has actually been performed.
- The college's own IT can operate it from the runbook without you.

---

**A closing instruction.** The user has asked for something "100% perfect". Be
worth trusting instead: build what genuinely holds, measure it honestly, and say
plainly where the limits are. A system whose failures are visible and correctly
described is what makes it safe to put in front of students — and this codebase
already understands that better than most. Keep that property. It is the most
valuable thing in it.
