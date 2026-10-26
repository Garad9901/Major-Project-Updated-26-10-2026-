# Phase 2c — the dialect switch, and the measurement it invalidates

Acceptance criterion accepted: `service.ask()` returns 1916 on both backends, and
1,916 was written down before the migration existed. That is the right standard
of evidence and you applied it unprompted.

Four notes. The second one contradicts something you said.

---

## Note 1 — the foreign-key find is bigger than you framed it

You called it "a plausible contributor to the wrong-table join defect." Go
further. **It is now the leading explanation.** Audit entry 801 was the model
joining `faculty` when it should have used `faculty_development`; it was
diagnosed as a table-notes wording problem and fixed by rewriting the note. The
actual condition was that the model had **never been shown a single
relationship** and was inferring every join key from column names. The wording
fix worked, which is why nobody looked further.

Three consequences you should carry forward:

1. **Every accuracy measurement ever taken on the SQL agent measured a
   handicapped agent.** Every figure in `docs/` describing how often it writes a
   correct query was produced by a model with no join information. Those numbers
   are not wrong, but they are a floor, not a ceiling.
2. **So re-measure, and expect improvement.** This is the rare defect whose fix
   should show up as a quality gain. Quantify it — a before/after on a set of
   join-requiring questions. If it moves materially, that belongs in the audit
   report as its own finding, and it is the most useful thing you could hand the
   user for their write-up.
3. **It may shift Phase 3's split.** The deterministic fast path exists partly
   because the LLM path is unreliable on joins. If the model is materially better
   at joins now, the boundary between "answer deterministically" and "give it to
   the model" moves. Do not re-plan Phase 3 yet — just measure honestly and let
   the number decide.

**And the detection method generalises.** You found it because two backends
agreed on zero, and zero was implausible. That is worth naming: **agreement
between two implementations is evidence only when the agreed value is itself
plausible.** Both paths returning 0 foreign keys for a 12-table relational schema
was a fact about the question, not the answer. Keep asking "is this number
possible?" before "do these two match?"

## Note 2 — `docs/LATENCY.md` is already invalid, by your own Phase 1 argument

You wrote: *"docs/LATENCY.md is still accurate — I haven't touched the SQL
agent's prompt."*

But in Phase 1 you wrote, justifying the ordering assertion: *"schema.py renders
the prompt in map order and a reorder would invalidate the prefix-cache
measurements in docs/LATENCY.md."*

If **reordering** the same tables invalidates those figures, then **adding twelve
`REFERENCES` clauses** to what `build_schema_text` emits certainly does. The
schema text *is* the prompt prefix those measurements are about. The FK fix
changed it. The figures stopped being true when `e3a7c27` landed, not when
`llm_client.py` changes.

This is the defect class you have been catching all week, arriving in your own
report. Not a serious one — but leaving a document asserting numbers the code no
longer produces is exactly what you corrected in the audit report and in the
HSTS ramp.

**Mark them superseded now**, with the reason, and do the re-measurement after
the dialect switch so it covers both changes at once. One re-measurement is fine;
a phase spent claiming accuracy you no longer have is not.

## Note 3 — check the timeout diagnosis before you write it into the runbook

Your characterisation may be too pessimistic, and the difference changes how
urgent the Resource Governor item is.

pyodbc's timeout maps to ODBC's `SQL_ATTR_QUERY_TIMEOUT`. When it fires, the
driver is specified to send an **attention** signal, and SQL Server generally
honours that by aborting the running batch — so it is not purely "we stop
waiting while the server keeps working." If so, a timed-out query does not
usually leave the college's CPU burning.

The real gap is narrower and worth stating precisely: **if the client dies** —
backend crash, container kill, network partition — there is nobody left to send
the attention, so the query is orphaned. Postgres's `statement_timeout` still
fires in that case because the server enforces it autonomously. That is a genuine
difference and Resource Governor is still the right answer; it is just a
different failure mode from the one you described.

**Settle it by observation rather than by me or by documentation.** Start a
deliberately expensive query (a cross join over the 13,000-row table will do —
avoid `WAITFOR DELAY`, the guard rejects it) with a short pyodbc timeout. When
the client gives up, query `sys.dm_exec_requests` **from a second connection** and
see whether the request is still running. Then kill the client process mid-query
and check again. Two observations, and you will know which sentence to put in the
runbook.

Same rule as the `Msg 3701` correction: state, not documentation, and not me.

## Note 4 — the pattern now has four instances, and the fourth is a new shape

Three were *absence rendered as a benign value*:

- guard: empty table set → "nothing disallowed" → **allowed**
- identity: missing field → Django's `is_active=True` default → **active**
- driver: blank column key → reads downstream as **"no data"**

The foreign keys are a different and more dangerous shape: **a privilege-filtered
view returned fewer rows instead of an error.** `constraint_column_usage` did not
fail for `rag_agent_ro`; it succeeded, quietly, with less. No exception, no
warning, no log line — and the code could not tell "this schema has no foreign
keys" from "you may not see them."

**That shape has an obvious next place to look, and you are about to walk into
it.** SQL Server's `INFORMATION_SCHEMA` views and catalog views are **also**
subject to metadata visibility rules: a principal sees metadata only for
securables it has permission on. So when you introspect foreign keys in the
T-SQL path, **verify it as `rag_agent_ro`, not as `sa`.**

If you test that introspection with the `sa` connection from the mirror, you will
see all twelve, conclude it works, and ship the identical defect into the new
backend — the Postgres bug was precisely "correct as owner, silent as the
read-only role." You said "both report 12". Confirm that the T-SQL 12 was counted
by the **read-only principal**, and if it was `sa`, re-count.

Also worth a look while the shape is fresh: anywhere else a catalog or
`INFORMATION_SCHEMA` view is read for a decision, on either backend.

---

## The work

1. **`llm_client.py`** — T-SQL dialect, T-SQL few-shot examples. You are right
   that the current pass succeeded only because `SELECT COUNT(*) ... WHERE` is
   portable; a question needing `TOP`, `+` concatenation or `DATEADD` would have
   failed. Test one of each so that is no longer a hypothesis.
2. **`guard.py`** — the injection suite re-run under the real executor, not the
   parser alone. A statement the parser rejects and a statement the server
   refuses are different guarantees, and the suite should now be able to
   demonstrate both. Show the **allow** cases alongside the deny cases.
3. **FK introspection in T-SQL** — verified as the read-only principal, per
   Note 4.
4. **Re-measure `docs/LATENCY.md`** — covering both the FK change and the dialect
   change. Mark the old figures superseded with the reason.
5. **The accuracy re-measurement from Note 1** — before/after on join-requiring
   questions, now that the model can see relationships.

Both backends stay green. `faculty_development` stays.

---

## Done when

- The model is asked for the dialect it is actually talking to, proven with a
  query needing `TOP`, one needing string concatenation, and one needing date
  arithmetic.
- The guard suite runs against the real executor, deny **and** allow cases shown.
- FK hints confirmed present when connected as `rag_agent_ro` on both backends.
- `docs/LATENCY.md` states numbers the code currently produces.
- The join-accuracy delta is quantified, whatever it turns out to be.
- Suite green on both; report the count and what you did not verify.

---

## One note on how this is going

Four of the most valuable findings in this migration — the guard fail-open, the
identity default, the foreign keys, and the `Msg 3701` correction — came from
checking something you were not asked to check, or doubting a result that looked
fine. Two of them were latent for the entire life of the project and would have
shipped to the college.

Keep the habit of reporting the clean results alongside the hits, and keep
saying plainly what is not done. The "not done" sections have been as useful as
the findings, because they are what makes the rest of the report trustworthy.
