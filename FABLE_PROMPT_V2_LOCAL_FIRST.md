# Build Prompt v2 — local first, then deploy

Supersedes `FABLE_REBUILD_PROMPT.md`. Two things changed: **everything is now
built and proven on the development laptop before the target machine exists**,
and the plan incorporates your Phase 0 findings.

---

## Standing context (keep this in mind for every phase)

An existing Django 5.2 + React system: an offline assistant answering questions
about a college's records. Runs entirely on the institution's own hardware; no
data leaves the machine. ~15k LOC backend across 16 apps, ~3.5k LOC frontend,
**331 tests passing**. Postgres + Redis + Qdrant + Ollama (`qwen2.5:7b`) + Caddy,
orchestrated by Docker Compose.

Pipeline: **router** (SQL / RAG / BOTH) → **sql_agent** (LLM writes SQL, guard
validates, read-only execution) and/or **rag_agent** (vector retrieval) →
**synthesis_agent** → **verification_agent**.

**This is a mature codebase whose comments are usually right and usually written
after a real incident. Read before changing. Smallest correct change wins.**

Eventual target: one CPU-only Windows machine on the college network, reading
the college's **MS SQL Server**, published on the college domain, 20–50 users.

---

## Your Phase 0 is accepted. Three points back.

**1. The guard fail-open — confirmed independently.** I re-ran your three cases
against the live guard after `ca69849`: `OPENROWSET`, `OPENQUERY` and bare
`SELECT 1` all rejected, and legitimate joins and subqueries still pass. The
reasoning in the commit message is correct: a table-valued function parses to an
`exp.Table` with an empty name, the `if t.name` filter dropped it, and
`empty_set - allowed` is empty. Good find, and right to fix it under the dialect
we already understand rather than during the switch.

**2. Your two corrections are accepted.**
- `LIMIT`→`TOP` is nearly free. Verified: sqlglot parses `LIMIT 50` and renders
  `SELECT TOP 50` for `dialect="tsql"` with no code change. My brief called it
  the most error-prone change; that was wrong. One constant.
- `sql_agent/service.py` was missing from the migration surface. It catches
  `psycopg2.OperationalError` to degrade gracefully; `pyodbc` raises a different
  exception type, so the degradation path breaks **silently, and only during an
  outage** — the worst possible time to discover it. Added below.

**3. Your disagreement on `faculty_development` is upheld — do not remove it.**
It is the calibration baseline for the suite and every measurement in `docs/`.
Removing it before the real schema exists destroys the reference you would use to
prove the migration did not regress. Revisit only once real data is flowing.

### And one generalisation, which matters more than the individual bug

The defect had a shape: **an empty result treated as permission**. "No forbidden
table was named" became "everything is allowed." That shape is not unique to one
function.

**In Phase 1, sweep for the same pattern before you port anything.** Every place
that computes a set of violations and allows when the set is empty; every
`if x:` that silently skips a check when `x` is falsy; every `except: pass`
around a validation; every filter applied *before* a security decision rather
than after. In a security control, the safe default is deny-on-unknown, and an
empty set of findings must be distinguishable from "nothing was inspected."

Report what you find as a list, fix what is genuinely fail-open, and say
explicitly if the answer is "nothing else." A clean sweep honestly reported is a
useful result.

---

## The change: build everything locally first

You do not have the target machine, and you flagged the real MS SQL schema and
the DBA's Change Tracking decision as blockers. **Most of that unblocks locally.**

Run **SQL Server 2022 in Docker on the development laptop**
(`mcr.microsoft.com/mssql/server:2022-latest`, Developer edition, free). With
that you can:

- Port the SQL agent to T-SQL and test it for real, not theoretically.
- **Enable Change Tracking yourself** — it is a `ALTER DATABASE ... SET
  CHANGE_TRACKING = ON` away. Phase 3 stops being blocked on the DBA.
- Build and measure the deterministic fast path on CPU, which is what the whole
  latency argument rests on.
- Prove no write ever reaches the college database, against a database you can
  safely try to write to.

**The only thing genuinely unknown is the real column names.** So do not hardcode
them anywhere.

### The schema adapter — the design decision that makes the real schema cheap

Put every reference to a college table or column behind one mapping layer: a
single declarative file naming the logical entity (`faculty.department_id`) and
its physical location in the college database (`dbo.HR_Staff.DeptCode`).

Everything else — the SQL agent's prompt, the fast-path queries, the sync
worker's Change Tracking subscriptions, the allowlist — reads the mapping, never
a literal table name.

The payoff: when the real schema arrives, adapting is **editing one file and
re-running the suite**, not auditing 15k lines. Build a local MS SQL schema that
mirrors the current demo shape, seed it from the existing `seed_demo_data`, and
develop against it. When the real names land, remap.

Local-vs-target honesty: the laptop's numbers are a **lower bound**, not the
target machine's truth. Record laptop specs alongside every measurement, and
re-run on the real machine in Phase 6. Use a smaller model locally
(`qwen2.5:3b`) if RAM is tight, and say so when you report latency.

---

## The migration surface — corrected and complete

| File | What must change |
|---|---|
| `sql_agent/guard.py` | `DIALECT` → `"tsql"`. **Already hardened in `ca69849`.** Port every guard test to T-SQL; add `GO`, `xp_cmdshell`, `WAITFOR DELAY`, `;`-chaining, and re-assert the `OPENROWSET`/`OPENQUERY` denials under the new dialect. `_cap_limit` needs no change — sqlglot renders `TOP`. |
| `sql_agent/llm_client.py` | Prompt says "PostgreSQL dialect"; few-shot examples are Postgres. → T-SQL. |
| `sql_agent/schema.py` | Introspection → `INFORMATION_SCHEMA` / `sys.tables`. |
| `sql_agent/db.py` | psycopg2 → `pyodbc`/`mssql-django`. Keep the pooling rationale and the separate read-only principal. |
| `sql_agent/executor.py` | Statement timeout and read-only enforcement are Postgres-specific. |
| **`sql_agent/service.py`** | **Your find.** `psycopg2.OperationalError` → the pyodbc equivalent. Cover it with a test that forces an outage, or it breaks silently when it matters most. |
| `academics/management/commands/setup_readonly_role.py` | Postgres role/GRANT → SQL Server login + `db_datareader` + explicit `DENY INSERT/UPDATE/DELETE`. |
| `health/management/commands/check_rag_agent_ro.py` | `has_table_privilege` → `HAS_PERMS_BY_NAME`. Keep the "prove a write is rejected" test. |
| `sync_worker/{config,poller,main}.py` | `updated_at` watermarks → Change Tracking. |
| `config/settings/base.py` | Second connection + database router. |
| `gunicorn.conf.py` | Postgres references. |

**Two databases, by ownership.** College MS SQL: academic records, **read-only,
never written**. Our Postgres: accounts, sessions, conversation history, audit,
cached answers. A Django router splits them; `academics` becomes `managed=False`
against MS SQL. Migrations must never target the MS SQL connection. This keeps
accounts/audit/orchestrator untouched, and college maintenance windows do not
sign everyone out.

---

## Constraints that do not move

1. **Never fine-tune or train on any data.** Stock weights. Grounding is
   retrieval at inference time only.
2. **Nothing leaves the machine.** No external APIs, telemetry, or cloud
   inference.
3. **Never invent a fact.** A failed lookup is reported as a failure, never as
   "no such record exists". This distinction is implemented and load-bearing.
4. **No write ever reaches the college database.**
5. **The suite stays green.** 331 today. A phase is not done otherwise.

---

## Phases — stop and report at each one

**Phase 1 — Fail-open sweep + local MS SQL.** The sweep described above. Then
SQL Server in Docker, the schema adapter, demo schema mirrored and seeded, both
databases wired with the router.
*Done when: the sweep is reported, the suite is green against Postgres, and the
same data is queryable from local MS SQL.*

**Phase 2 — T-SQL port.** The surface table above, behind the adapter. Read-only
principal created locally and proven — actually attempt a write and confirm it is
refused.
*Done when: guard rejects every T-SQL injection case, real questions return
correct answers from MS SQL, and the outage path is tested with pyodbc's
exceptions.*

**Phase 3 — Deterministic fast path.** The one that makes the latency target
reachable. Most college questions are structured lookups — "how many faculty in
X", "fees for Y", "when is exam Z" — with exact answers in exact rows. Match
intent, run a parameterised query, render a templated answer, **no LLM in the
request path**. Milliseconds, no hallucination possible, trivially concurrent.
The LLM handles only the genuinely open-ended remainder.
*Done when: common families answer in <500ms with no LLM call, and 20 concurrent
local users are served without queueing. Report p50/p95 with laptop specs.*

**Phase 4 — Change Tracking sync.** Enable CT locally, replace watermark polling,
default interval 5s, only changed rows re-embedded, cache invalidation via the
existing cross-process mechanism in `orchestrator/cache.py` — reuse it. Worker
keeps its state locally and never writes to the college database.
*Done when: a row changed in MS SQL reaches answers within seconds, and killing
the worker mid-run loses and duplicates nothing.*

**Phase 5 — Removals.** Delete `web_agent` (confirmed): on an internet-facing
system it is SSRF surface plus a prompt-injection channel from third-party
content. Exclude `experiments/` from the deployed image. **Keep
`faculty_development`.** Ensure demo seeding cannot run against production.
*Done when: coverage has not dropped and the suite is green.*

**Phase 6 — Target machine.** WSL2 + Docker Engine on the Windows box; run the
same stack. Benchmark Ollama in WSL2 vs native Windows and record real numbers.
Re-run the load test on real hardware. Complete the HSTS ramp after the
certificate is confirmed — `CADDY_HSTS_MAX_AGE` is what browsers receive;
`DJANGO_HSTS_SECONDS` sits behind the proxy. Ramp both together.
*Done when: `scripts/verify_deployment.sh` passes on the real machine and
measured p50/p95 meet the agreed target.*

**Phase 7 — Real schema + handover.** Remap the adapter to the college's actual
tables; re-run everything. Then the runbook the college's own IT will operate
from: start, stop, health, backup, restore, what to do when the model is down.

---

## Security additions for internet exposure (Phase 6)

Already present and to be preserved: CSP, HSTS, CSRF, per-IP rate limits, login
lockout, secure cookies, read-only principal, prompt-injection stripping, audit
log, secret guards that refuse to boot on default credentials.

To add: MS SQL reachable only from the backend, never routable from the internet
tier; credentials outside the repo; per-account question quotas (an
authenticated user exhausts a CPU-bound queue far more cheaply than an anonymous
one); every service unprivileged; off-machine backups with a **tested** restore;
dependencies pinned by hash; audit who asked what, never log answer bodies or
credentials; agreed retention enforced by the existing purge command.

---

## How to work

1. Read before writing. The answer to "why is this like this" is usually three
   lines above.
2. Smallest correct change. No refactoring for taste, no renaming what works, no
   reformatting files you are not otherwise editing.
3. Reuse the 331 tests. Extend; do not replace.
4. Edit documentation lines that became untrue; do not regenerate documents.
5. Stop and ask when an ambiguity changes the design. One question is cheaper
   than a wrong architecture.
6. Do not chase the CPU latency goal by tuning concurrency. Measured today:
   3.3 answers/min, p50 87s, 21 of 50 served, `LLM_MAX_CONCURRENCY=1`. Twenty
   parallel users is ~6 minutes of queue against a 25s timeout. The fast path is
   the answer; tuning is not.
7. Measurements, not adjectives. "p50 340ms over 200 requests", not "much
   faster".
8. Say what you did not verify. An unverified claim is worse than a gap, because
   it stops anyone else from checking.

---

## Branch

`ca69849` landed on `fix/audit-followup-31aug`, which carries six earlier commits
(a NameError that crashed every follow-up question, an HSTS ramp that reached no
browser, a capacity script measuring a department that does not exist, plus the
env-fallback fix and an audit addendum). All verified, 331 green.

**Recommended: merge that branch to `master` first, then branch per phase from
`master`.** It is verified work and it is the baseline every later phase is
measured against; leaving it on a side branch means the migration is built on
something that was never integrated. Confirm with the user before merging, and
note the repository has unpushed commits — push after merging.

---

## Definition of done

- Common questions: **p95 under 1 second**, 20 concurrent users, zero "busy".
- Open-ended questions correct within the agreed budget.
- Zero fabricated facts on an agreed test set; failed lookups always reported as
  failures.
- A row changed in MS SQL reaches answers within seconds.
- No write ever reaches the college database — proven, not assumed.
- Suite green, new code covered, `verify_deployment.sh` passing on the real
  machine, a restore actually performed.
- The college's IT can run it from the runbook without you.

---

**One closing note.** The user has asked for something "100% perfect". The more
useful thing is to be worth trusting: build what genuinely holds, measure it
honestly, and state the limits plainly. Your Phase 0 did exactly that — you
checked an assumption instead of accepting it, found a real hole, and corrected
two of my claims with evidence. Keep working that way; it is the most valuable
property this codebase has.
