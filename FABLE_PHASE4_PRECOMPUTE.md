# Phase 4 — pre-computation, and two corrections

Concurrency work accepted. 100 users at 553 ms, prediction held, and the audit
rows checked rather than assumed.

The best judgement in it was noticing that **raising threads alone relocates the
failure**: 24 threads against a pool of 8 doesn't throttle, it throws
`DatabaseUnavailable` on the fast path, which then falls through to the model —
a 200 ms answer becoming 60 s under exactly the load the fix was for. Moving all
three limits together was correct, and the failure would have looked like a
success.

---

## Correction 1 — derive the admission bound from what it actually governs

`LLM_MAX_ADMITTED = threads // 3 = 8` is derived from the wrong quantity. Thread
count does not determine how many people can be **served**; the queue timeout and
the answer time do.

With `LLM_MAX_CONCURRENCY=1` and p50 ≈ 87 s:

```
position 1  waits   0s  -> served
position 2  waits  87s  -> exceeds a 25s timeout, refused
```

So admitting 8 means **7 people hold a thread for 25 seconds and are refused
anyway.** They were never servable. That is the same cost the admission bound
exists to remove, just smaller — and it is invisible because the refusal message
looks identical either way.

Re-derive it:

```
LLM_MAX_ADMITTED = ceil(LLM_QUEUE_TIMEOUT / p50_answer_seconds) + 1
```

25 s timeout → **2**. 120 s timeout → **2–3**. Keep the strict-inequality
assertion against `GUNICORN_THREADS` as a ceiling — it is a valid upper bound,
just not the derivation.

Take `p50_answer_seconds` from measurement, not a constant, and recompute when it
changes. Pre-computation (below) will change it.

## Correction 2 — queue position makes the timeout decision easy

You correctly left `LLM_QUEUE_TIMEOUT=120` as a product decision. It stops being
a hard one once the user can see where they are.

A 120-second wait is unacceptable against a blank screen and reasonable against
*"You're 2nd in line — about 90 seconds."* One is a hang; the other is
information, and it lets the user decide to give up rather than guess.

With the admission bound you know the position exactly. **Implement position
display, then set the timeout to 120 s.** Without it, keep 25 s.

## Correction 3 — push

`07015f7` is still local. That commit is what makes the system survive twenty
simultaneous users. It should not exist on one laptop.

---

## The work: pre-computation

**The goal.** Descriptive questions are slow because a model writes them at
request time. But most of them are about **bounded entity sets** — 8 departments,
10 programmes, 12 branches. Generate those answers in the background after each
sync, store them, and serve them as lookups.

An 87-second question becomes a ~100 ms one. The model still runs — at 3am,
not while a person waits.

### What to pre-compute — from evidence, not guesses

You have the miss log from the coverage work: 886 requests over 195 distinct
questions, and you already found the top misses were "Describe…", "Summarise…",
"Compare…", "Which would you characterise as…".

**Derive the pre-computation set from those actual misses**, not from what seems
likely. Same discipline that took coverage 56.9% → 72.8%.

### Guard against combinatorial explosion

Single-entity shapes are safe: *entities × shapes*. Pairwise shapes are not —
"compare X and Y" over 8 departments is 28 pairs; over 100 entities it is 4,950.

- **Singles**: pre-compute all.
- **Pairs and above**: only the top N by observed traffic, with a hard cap.
- Everything else falls through to the live model path.

State the cap explicitly and record why that number.

### Verification matters more here, not less

A wrong live answer is served once. **A wrong pre-computed answer is served
hundreds of times before anyone notices**, and it carries the authority of being
instant.

So every pre-computed answer must be verified against its source rows **before it
is stored**, and one that fails verification must not be stored at all — the
question falls through to the live path instead. This is the one place where
paying for full verification is clearly worth it, because the cost is amortised
over every future serve.

### Freshness and invalidation

- Each stored answer records the data version it was generated from.
- A sync change affecting entity X regenerates only X's answers — reuse the
  existing cross-process invalidation in `orchestrator/cache.py`.
- **Debounce.** A table changing constantly must not trigger constant
  regeneration; batch and rate-limit, and say what the bound is.
- A stored answer whose source rows are gone must be deleted, not served.

### First-run cost — be honest about it

8 entities × 3 shapes × 87 s ≈ 12 minutes. Fine. But 100 entities × 3 shapes is
about 7 hours, and on a customer install that matters.

So: prioritise by expected traffic, generate the rest lazily on first request
(then keep it), and **report the projected first-run time before starting** so
nobody thinks the install has hung.

### Serving

Pre-computed answers are a distinct route — `route='PRECOMPUTED'` — audited like
any other, with provenance shown exactly as a live answer would show it. Do not
fold them into the existing cache route; you need to measure them separately.

---

## Then re-run the mixed load test — in this order

Not before. At today's coverage, 100 users means ~27 model requests of which
almost all are refused; that measures a gap you already know about.

After pre-computation, run **100 users with the real traffic mix** (the 886-request
distribution, distinct accounts, already signed in) and report:

- p50 / p95 overall and per route
- how many were served by each of Tier 0, fast path, pre-computed, and the model
- how many were refused, and whether the refusal was immediate

---

## Done when

- Coverage re-measured on the **same 886-request corpus** so it is comparable to
  72.8%. Report distinct and traffic-weighted, both.
- Every pre-computed answer verified before storage; failures fall through rather
  than being stored.
- A row change regenerates only the affected answers, proven by observation.
- A deleted entity's answers disappear.
- `LLM_MAX_ADMITTED` derived from the timeout and measured p50.
- Queue position displayed; timeout set accordingly.
- Mixed-mix load test run and reported per route.
- Tests green, count reported, and `07015f7` plus this work pushed.

## Standing rules

- Verify by observing state, never by parsing a message.
- Report measurements, not adjectives.
- Say what you did not verify.
- Ask whether a number is *plausible* before asking whether two things agree.
- Report the clean results alongside the findings — the negative results have
  been as useful as the hits.
