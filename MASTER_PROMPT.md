# MASTER PROMPT — Private On-Premise Data Assistant

Paste this whole file as your first message to Claude Code. Work through it in
order. Stop only at the checkpoints marked **STOP**.

---

## 1. What you are building

A product that installs inside a company, connects to their existing database,
adapts to it automatically, and answers staff questions about their own data —
instantly, exactly, and with the source shown.

Nothing leaves the building. **The language model is never trained, fine-tuned or
adapted on customer data.** Adaptation happens in indexes, vocabularies and
query templates — never in weights.

That single decision is the product. It makes answers exact instead of plausible,
makes deletion real, makes the system auditable, and makes it sellable to a
regulated buyer. Do not compromise it for any reason at any point.

## 2. Starting point — do not start from scratch

A working reference implementation already exists: Django + React, Postgres,
Qdrant, Ollama, Redis, Caddy, a sync worker, and **374 passing tests**. It has
been through a production audit and a MS SQL migration, and its code comments
record why things are the way they are — usually written after a real incident.

**Read it before changing it.** Reuse the proven parts: the SQL guard, the
orchestrator, the answer cache with cross-process invalidation, the outage
degradation paths, the audit log, the verification fast path, the read-only
principal model.

Your job is to **generalise the data layer** so it works against any customer
database, not to rewrite what already works. The smallest correct change wins.
No refactoring for taste. No renaming what works.

## 3. Non-negotiable constraints

1. **Never train, fine-tune or adapt the model on customer data.** Stock weights.
2. **No data egress.** No external APIs, telemetry, or cloud inference.
3. **Never invent a fact.** Every claim traces to rows. When a lookup fails, say
   the lookup failed — never that the record does not exist. These two sentences
   mean opposite things and confusing them is the worst defect this system can
   produce.
4. **Never write to the customer database.** Read-only principal, verified.
5. **Tests stay green.** A phase is not done otherwise.

## 4. The physics — settle this before you plan

Measured on CPU-only reference hardware:

| | |
|---|---|
| Prompt prefill | 33 tok/s |
| Generation | 7–8 tok/s |
| First token | ~13 s |
| Full model answer, p50 | 87 s |
| Cold prefill, 1,776-token prompt | 54 s |
| Fast path (no model), p95 | **468 ms** |
| Cache hit | 126–472 ms |

**Do not try to make the model fast enough to answer everything. It is not
possible on CPU and no prompt, quantisation or concurrency setting changes it.**

The design goal is the opposite: **keep the model off the critical path for as
much traffic as possible.** A parameterised SQL aggregate over an indexed column
is 1–20 ms and cannot hallucinate. Every question moved from the model to a
deterministic path is a ~100× latency win *and* an accuracy win.

Reference implementation reached **72.8% of real traffic** answered with no model
at all. Target 90–95%.

## 5. What published research says — this changes your decisions

| Finding | What to do |
|---|---|
| General Qwen2.5-Coder-7B scores **39.1%** on BIRD text-to-SQL; SQL-specialised 7B models (XiYanSQL-QwenCoder-7B **62.13%**, OmniSQL-7B **87.9%** Spider) beat it by ~23 points at identical size and speed | **Use a SQL-specialised model for the SQL agent.** Free accuracy. |
| Schema linking is "statistically indistinguishable from no linking" | **Do not build schema linking.** Independently confirmed here: adding foreign keys moved join accuracy 5/8 → 6/8, which is noise. |
| Self-consistency: 5× tokens, p95 10.6s → 50s, for **+0.13 points** | **Never.** |
| Self-correction: +1–4 points, ~2× cost | **Do this.** Let the model see its SQL error and retry once. |
| Text-to-SQL scores 91% on clean benchmarks, **21%** on realistic ~800-column schemas | Accuracy comes from a governed semantic layer, not model scale. **The deterministic path is the product.** |

## 6. Architecture

### 6.1 Install-time adaptation

Pointed at a database with a read-only credential:

1. **Introspect as the principal that will actually query.** Tables, columns,
   types, keys, foreign keys, indexes, row counts.
   > Catalog and `INFORMATION_SCHEMA` views are **privilege-filtered on every
   > major engine** — they return *fewer rows*, not an error. The reference
   > system shipped for months with **zero foreign keys reaching the model**
   > because that query was filtered and silently returned nothing. Assert that
   > what introspection sees is plausible, and fail loudly when it isn't.

2. **Classify every column** — identifier (distinct ≈ row count), measure
   (numeric, aggregatable), dimension (low cardinality — **this is the matchable
   vocabulary**), temporal, prose (→ embeddings), sensitive (→ excluded by
   default, opt-in recorded).

3. **Build the entity vocabulary** from the distinct values of dimension columns.
   This is what makes it adapt to any company: a hospital gets wards, a factory
   gets production lines. Nothing hardcoded.
   > **Match on word boundaries, never substrings.** This bug appeared three
   > separate times here — `"Science"` inside `"Computer Science"`, `"age"`
   > inside `"average"`. Longest-first ordering hides it until it doesn't.

4. **Generate question families from the schema shape**: count, aggregate,
   group-by, top-N, attribute lookup, existence, temporal. Each = intent pattern
   + parameterised SQL + answer template, generated per applicable column.

5. **Embed prose columns** for the retrieval path.

6. **Emit a coverage report** — what it can answer, with examples and honest
   gaps. This is the install artifact the customer sees.

7. **Short human confirmation.** Auto-derivation cannot know that `DPT_CD` means
   "department code" or that staff say "profs" for faculty. Ship a review screen:
   confirm entity names, add synonyms, confirm exclusions. **Do not claim
   zero-touch install — it will fail in the field.**

### 6.2 Continuous adaptation

Sync worker watches the customer database — Change Tracking (SQL Server),
logical replication or watermark (Postgres), binlog (MySQL). Default 5s.

On change: re-embed only changed prose rows, refresh dimension vocabularies,
invalidate affected cached answers cross-process, update statistics. Schema
changes trigger re-introspection and are **flagged for review, never applied
silently**.

Never write to the customer database — not even a state table. Keep state
locally. Resume with no gaps and no duplicates.

### 6.3 Query path — four tiers

```
Tier 0  answer cache ...............  1-10 ms
Tier 1  deterministic fast path ....  <500 ms   no model
Tier 2  SQL agent (model writes SQL)  seconds   + self-correction
Tier 3  retrieval + synthesis ......  seconds+
```

**The router must cost less than the answer.** Rules and embeddings first; a
model only for genuinely ambiguous questions.

### 6.4 Pre-computation — the trick that removes most slow answers

Prose questions about **bounded entity sets** are predictable. There are only 8
departments, 10 programmes, 12 branches. After each sync, generate those
descriptions **in the background** and store them.

An 87-second question becomes a 100 ms lookup. The model still runs — at 3am,
not while a person waits. Do this for every finite list.

### 6.5 Concurrency — the fast path must never be blocked by the model path

**This is the defect that would break a live demo, and it exists in the
reference implementation today.**

The web tier has a small thread pool. A request waiting for a model slot holds
its thread while it waits. Four model questions arriving together consume every
thread — and a 10 ms fast-path question then cannot get served at all. The
system's own compose file states this principle for Redis timeouts ("four hung
requests take the site down completely") and the model path violates it.

**Fix:** a real queue between the web tier and inference. Model requests are
enqueued and the thread is released immediately; results stream back over SSE.
Fast-path requests then can never be blocked, no matter how deep the queue.

Also required: request coalescing (20 people asking the same uncached question
→ one computation), per-user quotas not just per-IP, round-robin fair
scheduling, and immediate honest backpressure when the queue exceeds what can be
served within the timeout.

## 7. The failure patterns this codebase has already paid for

Every one of these was a real, shipped defect. Treat them as requirements.

**Absence rendered as a benign value — four instances:**
- Empty set of disallowed tables read as "everything permitted" (guard failed
  open on `OPENROWSET`, `OPENQUERY`, and bare `SELECT 1`)
- Missing field in a cached record → framework default `is_active=True` → a
  suspended account rebuilt as active
- Blank column key in a driver result → read downstream as "no data"
- A privilege-filtered view returning **fewer rows instead of an error**

> Rule: an empty result must be distinguishable from "nothing was inspected."
> In a security control, deny on unknown. A mean over zero rows is **not zero**.

**Partial identifier checks.** `t.name` is only the final component, so
`server.db.schema.table` reduced to an allowlisted name while pointing at a
linked server. Validate the whole identifier. Reject catalog qualifiers; accept
schema qualifiers only when declared. Deny `OPENROWSET`, `OPENQUERY`,
`OPENDATASOURCE`, `dblink` — these are *reads*, so write-denial doesn't stop
them. Check projections, not just `FROM` sources.

**Verify by state, not by message.** A `DROP` was reported as allowed because a
matcher didn't match `Msg 3701`. Confirm a write was refused by observing the row
count, never by parsing an error string. A matcher that silently fails to match
reads exactly like a passing test.

**Audit the fast path too.** Every fast-path answer went out unaudited because a
type mismatch made the audit write raise, and the exception was caught. An
accountability control failing silently while the feature it guards appears to
work.

**Pools propagate poison.** An `IntegrityError` left an aborted connection that
connection reuse handed to the *next* request — breaking requests that didn't
cause it.

**More context can make a model confidently wrong.** After foreign keys were
added, one question moved from *erroring* to returning a confident `0` against a
true `3`, because the model followed a real-but-wrong relationship. Errors are
visible; wrong numbers are not. Verify Tier 2 answers against retrieved rows and
degrade to "I could not answer this reliably" rather than to a plausible number.

**Warm on boot.** Cold prefill costs 54 s. Issue a synthetic query at startup;
report "warming", not "ready", until it completes.

**Load tests lie by default.** One harness used `regenerate:true` to bypass the
cache — which also bypassed the fast path it was measuring. Another counted 10
rate-limit refusals as failures because 20 simulated users shared one account.
**Test with distinct accounts and the real traffic mix.**

## 8. Build order — STOP at each checkpoint

**Phase 0 — Survey.** Read the reference implementation. Confirm the tier
boundaries and what is reusable. Produce a written plan. **STOP.**

**Phase 1 — Concurrency fix.** Queue between web tier and inference; fast path
provably unblockable. Prove it: saturate the model path and show fast-path p95
unchanged. **STOP.**

**Phase 2 — Database adapter layer.** One interface — `introspect`,
`changes_since`, `setup_readonly`, `statement_timeout`, `dialect`. Implement for
Postgres and SQL Server. Introspection verified **as the read-only principal**.
**STOP.**

**Phase 3 — Install-time adaptation.** Classification, entity vocabulary, family
generation, coverage report, confirmation screen. **STOP.**

**Phase 4 — Fast path + pre-computation.** Target 90%+ coverage measured against
a realistic question set. **STOP.**

**Phase 5 — Model path.** SQL-specialised model, self-correction, verification,
refusal on unverifiable. No schema linking. No self-consistency. **STOP.**

**Phase 6 — Sync worker.** Change tracking, incremental re-embed, cache
invalidation, resumption proven by killing it mid-run. **STOP.**

**Phase 7 — Security and load.** Read-only proven by state observation, guard
suite against the real executor (deny **and** allow cases), per-user quotas,
audit coverage including fast path, 50-user load test with distinct accounts and
real mix. **STOP.**

**Phase 8 — Packaging.** Installer, health, backup with tested restore, runbook
the customer's own IT can operate from.

## 9. How to work

- Read before writing. The answer to "why is this like this" is usually three
  lines above.
- Smallest correct change. No reformatting files you aren't otherwise editing.
- Extend the existing tests; don't replace them.
- Edit documentation lines that became untrue; don't regenerate documents.
- **Report measurements, not adjectives.** "p50 340 ms over 200 requests", not
  "much faster".
- **Say what you did not verify.** An unverified claim is worse than a gap,
  because it stops anyone else from checking.
- Stop and ask when an ambiguity changes the design.
- When a result looks fine, ask whether the *number itself* is plausible. Two
  implementations agreeing is only evidence if the agreed value could be true —
  zero foreign keys in a relational schema was agreement on an impossible value.

## 10. Definition of done

- 20 concurrent users, real traffic mix: **p95 under 1 second**, zero refusals,
  measured with distinct accounts.
- Model-path answers stream first words within ~10 s and complete within the
  agreed budget.
- **Zero fabricated facts** on an agreed test set. Failed lookups always reported
  as failures.
- A row changed in the customer database reaches answers within seconds; a
  deleted row disappears.
- No write ever reaches the customer database — proven by observed state.
- Install against an unseen database produces a working system plus an honest
  coverage report.
- Tests green; new code covered; restore actually performed.

## 11. Ask before Phase 1

1. Which database engines must ship first?
2. Target hardware — cores, RAM, and is a GPU available at any tier?
3. Expected concurrent users, and the real question mix if any exists?
4. Is the product a separate repository or a generalisation of the existing one?
5. Who operates it after install — the customer's IT, or you?

---

**Final instruction.** The user will ask for something "perfect". Be worth
trusting instead: build what genuinely holds, measure it honestly, and state the
limits plainly. Every valuable finding in this project's history came from
checking something nobody asked about, or doubting a result that looked fine.
Two of those defects were latent for the entire life of the project and would
have shipped to real users. Keep that habit — it is the most valuable property
this codebase has.
