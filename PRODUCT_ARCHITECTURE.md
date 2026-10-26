# Product Architecture

**Installs, connects to any company database, adapts to it, answers in under a second.**

On-premise. No data egress. **The model is never trained, fine-tuned, or adapted
on customer data.** Adaptation happens in indexes, vocabularies and templates —
never in weights. That single decision is what makes the product exact,
auditable, deletable, multi-tenant and legally sellable.

---

## 1. The claim, stated so it survives a procurement review

| Claim | True? |
|---|---|
| Installs on-premise, no data leaves the building | Yes — architectural, not a setting |
| Connects to any supported company database | Yes — MS SQL, PostgreSQL, MySQL, Oracle via adapters |
| Adapts to that database automatically | Yes — auto-derived draft + a short confirmation step |
| Answers in under a second | **For structured questions.** Prose questions take longer on CPU |
| Answers are exact and cited | Yes — they come from rows, not from a model's memory |
| Updates within seconds of a database change | Yes — change tracking, not retraining |
| A deleted record disappears from answers | Yes — because it was never in the weights |

**Do not sell "any question in one second."** Sell: *instant, exact, cited
answers on your live data, with nothing leaving your building.* That claim is
defensible under scrutiny. The other one collapses the first time someone asks
"summarise the trends across departments."

---

## 2. Why answers are fast — the actual budget

A structured answer is a parameterised SQL aggregate over an indexed column. The
work is genuinely small:

```
inbound HTTP + auth + session          ~5-15 ms
intent match + entity resolution        ~1-5  ms
parameterised query (indexed COUNT/AVG) ~1-20 ms
answer template render                  <1    ms
audit write (async)                      0    ms on the response path
                                        ─────────
                                        ~10-40 ms typical
```

The measured p95 on the reference implementation is **468 ms**, and a cache hit
is **126–472 ms** end to end. The headroom is large because no model runs.

**A model in the request path costs 100–1000× that.** Reference hardware
(CPU-only): prompt prefill 33 tok/s, generation 7–8 tok/s, a 1,776-token system
prompt, first-token latency ~13 s, p50 for a full model-generated answer ~87 s.
That is not a tuning problem; it is arithmetic. So the architecture's entire job
is **to keep the model off the critical path for as much traffic as possible.**

---

## 3. Install-time adaptation — "it learns the database"

This is the differentiating subsystem. Pointed at a database with a read-only
credential, it produces everything needed to answer questions about it.

### 3.1 Introspect — **as the principal that will actually query**

Read tables, columns, types, primary keys, **foreign keys**, indexes, row counts.

> **Non-negotiable:** run introspection as the read-only principal, never as an
> admin. Catalog and `INFORMATION_SCHEMA` views are privilege-filtered on every
> major engine — they return *fewer rows*, not an error, when the principal lacks
> permission. The reference implementation shipped for months with **zero
> foreign keys reaching the model** because the FK query was privilege-filtered
> and silently returned nothing. Introspection must assert that what it sees is
> what the query engine will see, and fail loudly when a catalog read returns
> implausibly little.

### 3.2 Classify every column

| Class | Detection | Used for |
|---|---|---|
| **Identifier** | PK/FK, or distinct ≈ row count | Joins, lookups — never aggregated |
| **Measure** | numeric, non-key, wide range | SUM / AVG / MIN / MAX |
| **Dimension** | low cardinality (< ~5% distinct, or < 200 values) | Filtering, grouping — **the matchable vocabulary** |
| **Temporal** | date/time types | As-of, trends, latest, ranges |
| **Prose** | text, high avg length, low repetition | Embedded for semantic retrieval |
| **Sensitive** | name/pattern match — national ID, contact, financial, health | **Excluded by default**, opt-in with a recorded justification |

Cardinality is what makes this work: `department` with 8 distinct values is a
dimension; `student_id` with 40,000 is an identifier. Same type, opposite roles.

### 3.3 Build the entity vocabulary

The **distinct values of dimension columns become the matchable vocabulary**.
"Computer Science", "Engineering", "High Development Need" — these are the terms
that appear in real user questions, and they come from the customer's own data.

This is what makes "adapts to any company" real rather than aspirational: point
it at a hospital and the vocabulary becomes wards and procedures; at a factory,
production lines and defect codes. Nothing is hardcoded.

**Match on word boundaries, never substrings.** The reference implementation hit
this three separate times — `"Science"` matching inside `"Computer Science"`,
`"age"` matching inside `"average"`. Longest-first ordering hides it until it
doesn't. Boundaries, always.

### 3.4 Derive the question families

Given a table with measures, dimensions and temporal columns, the answerable
families are **derivable from the shape**:

| Family | Template | Example |
|---|---|---|
| Count | `COUNT(*) [WHERE dim = ?]` | "How many faculty in Computer Science?" |
| Aggregate | `AVG/SUM/MIN/MAX(measure) [WHERE dim = ?]` | "Average teaching effectiveness?" |
| Group | `dim, AGG(measure) GROUP BY dim` | "Average publications by department?" |
| Top-N | `ORDER BY measure DESC LIMIT n` | "Top 5 departments by headcount?" |
| Attribute | `SELECT cols WHERE identifier = ?` | "Who heads Mechanical?" |
| Existence | `EXISTS(...)` | "Is there a Robotics programme?" |
| Temporal | `WHERE date BETWEEN ? AND ?` | "Enrolments this semester?" |

Each family = **intent pattern + parameterised SQL + answer template**. Each is
generated per applicable column, so a 12-table schema yields hundreds of
answerable questions with no model involved and no hand-authoring.

The reference implementation hand-wrote eleven families and reached **72.8% of
real traffic**. Deriving them from the schema is the same idea, generalised.

### 3.5 Embed the prose

Prose columns → embeddings → vector store, for questions that need description
rather than calculation.

### 3.6 Confirm with a human — 30 minutes, once

**Do not claim zero-touch. It will fail in the field and cost you the account.**

Auto-derivation gets types, cardinality and relationships right. It cannot know
that `DPT_CD` means "department code" or that staff say "profs" for faculty. So
install ends with a short review screen:

- Confirm/rename detected entities in business language
- Add synonyms ("staff", "teachers", "profs" → `faculty`)
- Confirm sensitive-column exclusions
- Confirm which tables are in scope

Ten minutes with a DBA turns a good draft into a correct one. Frame it as
onboarding, not configuration.

### 3.7 Emit a coverage report

The install ends by telling the customer **what it can answer**, with worked
examples and honest gaps. This is the best artifact in the whole product: it
sets expectations truthfully on day one and doubles as the acceptance test.

---

## 4. Continuous adaptation — the sync worker

Watches the customer database and updates **indexes, not weights**.

| Engine | Mechanism |
|---|---|
| SQL Server | Change Tracking (`CHANGETABLE(CHANGES ...)`) |
| PostgreSQL | Logical replication, or `updated_at` watermark |
| MySQL | Binlog, or watermark |
| Oracle | Change Notification, or `ORA_ROWSCN` |

Default interval **5 s**, configurable. On change:

1. Re-embed changed prose rows — **only the changed ones**, never a scheduled
   full re-index.
2. Refresh dimension vocabularies when new distinct values appear.
3. Invalidate affected cached answers (cross-process, so every worker sees it).
4. Update cardinality statistics; if a column's class changes, flag for review.

**Schema drift:** a new table or column triggers re-introspection of that object
and regeneration of affected families, then flags it for confirmation. Schema
changes are the one thing that should never be applied silently.

**Rules:** never write to the customer database — not even a state table. Keep
worker state locally. Resume from the last token with no gaps and no duplicates.
Survive the source being unreachable and catch up on return.

**A delete is a delete.** The row leaves the index and leaves every answer. This
is the compliance property that fine-tuning cannot offer at any price, and it is
worth a line in the sales material.

---

## 5. Query path — four tiers, explicit budgets

```
question
   │
   ├─ Tier 0  answer cache ......................  1-10 ms   exact match, sync-invalidated
   ├─ Tier 1  deterministic fast path ...........  <500 ms   intent → parameterised SQL → template
   ├─ Tier 2  SQL agent (model writes SQL) ......  seconds   structured, no family matches
   └─ Tier 3  retrieval + synthesis .............  seconds+  prose, comparison, judgement
```

**The router must not cost more than the answer.** Rules and embedding
similarity first; a model only for genuinely ambiguous questions. A router that
calls an LLM to decide whether to call an LLM has spent the budget.

**Tier 1 is the product.** Every point of traffic moved from Tier 2/3 into
Tier 1 is a 100× latency improvement *and* an exactness improvement, because a
parameterised query cannot hallucinate.

**Tier 2 carries a real risk, and it must be designed for.** In measurement, the
model reached only **6/8** on join questions where it had every relationship
hint — and one failure returned a confident `0` against a true `3`. It had
previously *errored* on that question. More context made it quietly wrong
instead of visibly broken. So: Tier 2 answers are verified against retrieved rows
before display, and an unverifiable answer degrades to "I could not answer this
reliably" rather than to a plausible number.

**Concurrency.** Tiers 0–1 are stateless and scale with CPU cores. Tiers 2–3 are
serialised behind a small semaphore because model inference saturates the
machine. With 72.8% of traffic in Tiers 0–1, twenty concurrent users produce
roughly five model requests — which is a queue, not a stampede. **Load-test with
the real traffic mix, not with fast-path questions**, or the result flatters
itself.

---

## 6. Correctness and trust

- **Every claim traces to rows.** Answers cite what they came from.
- **Never report absence when the lookup failed.** "The records don't cover X"
  and "I couldn't reach the records" are different sentences and must never be
  confused. This distinction is the single most important safety property in the
  system.
- **Fast numeric verification always**: every number in an answer is checked
  against the retrieved rows at negligible cost. Model-based verification is
  optional and off by default on CPU — it doubles latency.
- **A mean over zero rows is not zero.** Empty aggregates render as "no matching
  records", never as `0`. Absence must never be rendered as a benign value —
  this class of bug appeared four separate times in the reference
  implementation, each time in a different disguise.

---

## 7. Security and tenancy

- **Read-only principal**, always. Explicit `DENY` on write. Verify by observing
  state — attempt a write and confirm the row count is unchanged. Never by
  parsing an error message; a matcher that silently fails to match reads exactly
  like a passing test.
- **SQL guard** on every model-generated statement: single read-only SELECT,
  allowlisted tables, capped row limit, dialect-aware parse.
  - **Validate the whole identifier.** `server.db.schema.table` reduces to an
    allowlisted final component while pointing at a linked server. Reject
    catalog qualifiers; accept schema qualifiers only when declared.
  - **Deny remote-source functions** — `OPENROWSET`, `OPENQUERY`,
    `OPENDATASOURCE`, `dblink`. These are *reads*, so no amount of write-denial
    excludes them.
  - **Reject unnamed data sources and zero-table selects.** An empty set of
    disallowed tables means nothing was inspected, not that everything is
    permitted.
- **Per-query access control** — the capability fine-tuning forfeits. Map roles
  to allowed tables, columns and row filters; enforce at query construction. The
  model cannot leak what the query never selected.
- **Audit every answer, including Tier 0 and Tier 1.** A fast path that skips the
  audit log is an accountability control failing silently while the feature it
  guards appears to work.
- **Sensitive columns excluded by default**, opt-in recorded with a reason.
- **Multi-tenant by construction**: weights are stock and identical everywhere;
  customer data lives only in that customer's indexes. Two installs cannot
  contaminate each other because there is nothing in the model to contaminate.

---

## 8. Database portability

One adapter interface, four implementations:

```
introspect()              → tables, columns, types, keys, cardinality
changes_since(token)      → changed rows + new token
setup_readonly(principal) → engine-specific grants and denials
statement_timeout(sec)    → engine-specific enforcement
dialect                   → for SQL parse/render
```

Use a dialect-aware SQL parser so one guard serves every engine.

**Timeouts differ in kind, not just syntax.** PostgreSQL's `statement_timeout` is
enforced by the server autonomously and fires even if the client dies. ODBC's
query timeout depends on the client sending a cancel — if the client crashes, the
query is orphaned and keeps consuming the customer's CPU. Where the server cannot
enforce it, say so in the runbook and ask the customer's DBA for a resource
limit. Do not describe the guarantee as equivalent when it isn't.

---

## 9. Deployment

- Single-node on-premise; containers on Linux, or WSL2 on a Windows host.
- **CPU is the baseline.** A GPU is the only thing that meaningfully moves
  Tier 2/3 latency, and it changes nothing about Tiers 0–1.
- **Warm on boot.** Cold prefill of a 1,776-token system prompt measured
  **54 seconds**. Issue a synthetic query at startup so the first real user never
  pays it. Health must report "warming", not "ready", until it completes.
- TLS terminated at the edge; HSTS ramped only after certificate issuance is
  confirmed. Verify which layer actually emits the header — where a proxy sets it,
  the application's setting never reaches the browser.
- Backups of the product's own store, off-machine, with a **tested** restore.
  The customer database is theirs and is never written.
- Health endpoint distinguishing degraded from down, per component.

---

## 10. Coverage grows with use — without training

The system logs every question it could not answer in Tier 0/1, clusters the
misses, and **proposes new question families to the operator**, with the SQL it
would run and the answer shape it would render. The operator approves; coverage
rises.

The reference implementation did this by hand and went **56.9% → 72.8% of real
traffic** in one pass, by noticing the top misses were all single scalars.
Productising that loop means the system gets better the more it is used —
genuine improvement, no weights touched, every gain inspectable and reversible.

Measure coverage two ways and report both: **distinct questions** (47.2%) and
**traffic-weighted** (72.8%). The second is what users experience; the first
stops you over-fitting to your loudest asker. Never fold synthetic load-test
traffic into either.

---

## 11. What this product deliberately does not do

- **No fine-tuning, ever.** Not at install, not continuously, not optionally.
- **No data egress.** No telemetry, no cloud inference, no external APIs.
- **No unverifiable answers.** It refuses rather than guesses.
- **No claim of instant answers to open-ended questions** on CPU hardware.

Each of these is a feature in a procurement conversation. "The model does not
learn your data" is the sentence that gets this past a data-protection officer.

---

## 12. Honest limits

- Prose, comparison and judgement questions need the model, and on CPU that is
  seconds to a minute. A GPU is the only fix.
- Auto-derivation needs a short human confirmation to reach business-language
  accuracy.
- Very wide schemas (hundreds of tables) need scoping at install; not everything
  should be in the answerable set.
- Questions requiring reasoning across many tables at once remain the weakest
  area — the model reaches ~6/8 even with full relationship information.
- Schema changes need review before they take effect. This is deliberate.

---

## 13. Build order

1. **Adapter interface + introspection** — as the read-only principal, with the
   plausibility assertions.
2. **Column classification + entity vocabulary** — boundary matching from day one.
3. **Family generation + Tier 1** — the product's core.
4. **Sync worker** — change tracking, incremental re-embed, cache invalidation.
5. **Guard + Tier 2** — dialect-aware, full-identifier validation.
6. **Tier 3** — retrieval and synthesis.
7. **Install wizard + coverage report** — what makes it a product rather than a
   deployment.
8. **Miss-mining loop** — what makes it improve.

Steps 1–4 alone are a saleable product for structured questions. Ship that first.

---

## 14. The sentence that sells it

> It connects to your database, learns its structure in minutes, and answers
> questions about your live data in under a second — exactly, with the records
> it used shown alongside. Your data never leaves your building, and the model
> is never trained on it, so a deleted record is genuinely gone.

Every clause is true, verifiable, and hard for a fine-tuning competitor to match.
