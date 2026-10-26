# Phase 2 — T-SQL port

Phase 1 accepted. Read the three notes first; they change what Phase 2 does.

---

## Note 1 — your `identity.py` find is confirmed, and I checked the path next to it

The fix is right: an incomplete record is treated as no record, and the caller
falls back to the database, which is authoritative. Deny-on-unknown, on a
permission path, three lines. Correct call to fix it despite finding no path that
produces a partial record today — the cost of being wrong is asymmetric.

I then checked the obvious adjacent question: **does suspending an account
actually evict the cached identity, or does a disabled user keep working for the
1800s TTL?** It evicts. `disable_user` → `accounts.sessions.revoke_all` → the
session is deleted from Redis and `identity.forget()` is called, and a deleted
session is never authenticated regardless of what the identity cache holds. It is
reasoned through at `identity.py:38-60`. **Nothing to do — recorded so you don't
re-derive it.**

The clean list you reported is as valuable as the hit. Keep reporting both.

## Note 2 — the schema-map ordering assertion is the best thing in Phase 1

Asserting `physical_tables() == list(ALLOWED_TABLES)` *including order*, because
`schema.py` renders the prompt in map order and a reorder would silently
invalidate the prefix-cache measurements in `docs/LATENCY.md` — that is a
coupling most reviewers would miss, and you pinned it before it could bite.

Carry the same care into Phase 2, because **you are about to invalidate those
measurements deliberately**: changing the SQL agent's system prompt from
"PostgreSQL dialect" to T-SQL changes the prompt prefix. Either re-measure and
update `docs/LATENCY.md`, or mark the affected figures as superseded with the
reason. Do not leave a number in a document that the code no longer produces —
that is the exact defect class this project keeps finding.

## Note 3 — the Redis memory flag: lower severity than it reads, and cheap to fix now

I checked the running configuration. Redis is already constrained where it
matters:

```
--appendonly yes --appendfsync everysec
--maxmemory ${REDIS_MAXMEMORY:-256mb}
noeviction          # deliberately NOT allkeys-lru: LRU would evict live sessions
```

So the keyspace is capped at 256 MB by Redis itself. The missing **container**
limit is defence-in-depth against overhead outside the keyspace (AOF rewrite,
copy-on-write), which `docker-compose.prod.yml:77` sizes at 512 MB. Real, worth
closing, but not the unbounded-growth risk "no memory limit" suggests.

**And the fix is cheaper than you assumed. Restart only `redis` and `backup`.**
Neither is `ollama`, so there is **no 15-minute model reload** — that cost only
applies to restarting the model container. And because AOF persistence is on,
restarting Redis does **not** sign anyone out; that persistence exists precisely
so a restart is survivable. Do it now rather than carrying it to Phase 6.

---

## A standing rule, learned twice on this project

You reported `DROP` as ALLOWED, then found your grep had not matched `Msg 3701`,
and confirmed the truth by **re-reading the table** instead of trusting the error
text. That is the second time here that the instrument was wrong rather than the
system — `capacity_test.sh` counting every success as a failure was the first.

**So: verify a security control by observing STATE, not by parsing a MESSAGE.**
A write is refused if the row count is unchanged. A table is gone if it is gone.
Error strings vary by driver, locale and version, and a matcher that silently
fails to match reads exactly like a passing test. Apply this throughout Phase 2 —
it is the phase most full of "did that actually get rejected?" questions.

---

## What Phase 2 does

Port the SQL path to T-SQL, behind the schema map, with both database backends
kept green.

### 1. Make the map load-bearing

You correctly deferred this. Now do it: consumers read `schema_map.json` at
runtime, and `ALLOWED_TABLES` is **derived from the map** rather than a parallel
literal compared by a test. Two lists that must agree are a drift bug waiting to
happen; one source with a derived view cannot drift.

Keep the fail-closed behaviour you built — missing file, bad JSON, empty set, or
a table with no physical name must all still raise.

### 2. The port

Per the corrected surface list:

- `guard.py` — `DIALECT` → `"tsql"`. `_cap_limit` needs no change; sqlglot
  renders `TOP`. Port every existing guard test to T-SQL and **re-assert the
  `OPENROWSET`/`OPENQUERY` denials under the new dialect** — they were fixed
  under Postgres, and the whole point was the dialect they become live in. Add
  `GO` batch separators, `;`-chaining, `xp_cmdshell`, `WAITFOR DELAY`,
  `sp_executesql`, and a four-part name (`server.db.schema.table`).
- `llm_client.py` — prompt and few-shot examples to T-SQL. See Note 2 about
  `docs/LATENCY.md`.
- `schema.py` — introspection via `INFORMATION_SCHEMA` / `sys.tables`.
- `db.py` — pyodbc/mssql-django; keep the pooling rationale and the separate
  read-only principal.
- `executor.py` — statement timeout and read-only enforcement, T-SQL forms.
- **`service.py`** — your find. `psycopg2.OperationalError` → the pyodbc
  equivalents. **This needs a test that forces a real outage**, because the path
  is silent and only runs when the database is already down. Stop the SQL Server
  container and assert the degradation message, rather than mocking an exception
  type you are guessing at.
- `setup_readonly_role.py` — SQL Server login, `db_datareader`, explicit
  `DENY INSERT/UPDATE/DELETE`.
- `check_rag_agent_ro.py` — `HAS_PERMS_BY_NAME`; keep the prove-a-write-fails
  check, and make it assert on state per the standing rule above.

### 3. Both backends stay green

Do not drop the Postgres path. It is the calibration baseline for every
measurement in `docs/` and the only reference for proving the port did not
regress behaviour. The suite should be runnable against either backend, and both
should pass, until the real college schema is in and the migration is proven.

---

## Done when

- Guard rejects every T-SQL injection case listed above, and still allows
  legitimate joins, subqueries and aggregates. **Show the allow cases too** — a
  guard that rejects real questions gets relaxed by whoever is on call next.
- Real questions return correct answers from local MS SQL, matching what the
  Postgres path returns for the same question.
- The outage path is exercised against a genuinely stopped database.
- A write is proven refused by observing unchanged state.
- `ALLOWED_TABLES` has one source.
- `docs/LATENCY.md` either re-measured or explicitly marked superseded.
- Suite green on both backends. Report the count.

## Do not

- Do not rewrite working modules to change one behaviour in them.
- Do not chase latency here; that is Phase 3, and it is where the target is
  actually won.
- Do not remove `faculty_development` — still the calibration baseline.
- Do not go quiet on a surprise. One question costs less than a wrong port.

---

## Reporting

What you changed, what you measured, what you did **not** verify, and anything
you found that was not in scope. The out-of-scope findings have been the most
valuable output of the last two phases — the guard fail-open and the identity
one both came from looking sideways rather than straight ahead.
