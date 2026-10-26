# Provenance of bench/results

Paths are relative to `bench/results/`. Dates are when the file was produced (file timestamp; the original runs also carry it in their file names).

| file | build tag | date measured | what it measures |
|---|---|---|---|
| `original_2026-08-18/experiment_syn7b_20260818_072056.csv` | paper-original | 2026-08-18 | Answer-quality run, `full` config, synthesis model qwen2.5:7b (`--tag syn7b`): per question route, generated SQL, semantic/string similarity to ground truth, correctness, verification outcome, latency. 10 questions. |
| `original_2026-08-18/experiment_syn7b_20260818_072056.jsonl` | paper-original | 2026-08-18 | Same run as the `.csv` above, one JSON record per question including ground truth, RAG chunks, synthesis and final answer. |
| `original_2026-08-18/experiment_syn3b_20260818_072901.csv` | paper-original | 2026-08-18 | Same answer-quality run with synthesis model qwen2.5:3b (`--tag syn3b`), same 10 questions. |
| `original_2026-08-18/experiment_syn3b_20260818_072901.jsonl` | paper-original | 2026-08-18 | Same run as the syn3b `.csv` above, one JSON record per question. |
| `fastpath_precision_before.json` | paper-latest | 2026-10-10 | B3: fast-path precision on 56 labelled cases (`fastpath_cases.json`) before the fix: 40 answered, 15 correct, 25 wrong, precision 0.375, silent error rate 0.446. |
| `fastpath_precision_after.json` | paper-latest | 2026-10-10 | B3: same 56 cases after the fix: 37 answered, 37 correct, 0 wrong, precision 1.0, silent error rate 0. |
| `fastpath_precision_heldout_before.json` | paper-latest | 2026-10-10 | B3: fast-path precision on 90 held-out cases (`fastpath_heldout.json`) before the fix: 17 answered, 8 correct, 9 wrong, precision 0.471. |
| `fastpath_precision_heldout_after.json` | paper-latest | 2026-10-10 | B3: same 90 held-out cases after the fix: 21 answered, 21 correct, 0 wrong, precision 1.0. |
| `coverage.json` | paper-latest | 2026-10-10 | B2: replay of all 992 logged requests (218 distinct questions; audit_log from the 2026-09-07 backup) through the model-free tiers: share served by FAST / PRECOMPUTABLE / MODEL, overall, for genuine traffic, and for the load-test pool; intent mix; routing-decision time. |
| `coverage_per_question.csv` | paper-latest | 2026-10-10 | B2: per distinct question, times asked, tier, intent and whether it is in the load-test pool (218 rows). |
| `coverage_before_fix.json` | paper-latest | 2026-10-10 | B2: the same replay as `coverage.json` on the build before the fast-path fix (from the file name; FAST share of requests 0.648 here vs 0.590 after). |
| `coverage_per_question_before_fix.csv` | paper-latest | 2026-10-10 | B2: per-question table for the before-fix replay (218 rows). |
| `load_fast_1.json` | paper-latest | 2026-10-10 | B5: 1 concurrent user (distinct account) asking a fast-path question: latency p50/p95/max, throughput, failures. |
| `load_fast_10.json` | paper-latest | 2026-10-10 | B5: same, 10 concurrent users. |
| `load_fast_20.json` | paper-latest | 2026-10-10 | B5: same, 20 concurrent users. |
| `load_fast_50.json` | paper-latest | 2026-10-10 | B5: same, 50 concurrent users. |
| `load_fast_60.json` | paper-latest | 2026-10-10 | B5: same, 60 concurrent users. |
| `load_fast_100_run1.json` | paper-latest | 2026-10-10 | B5: same, 100 concurrent users, run 1 of 3. |
| `load_fast_100_run2.json` | paper-latest | 2026-10-10 | B5: same, 100 concurrent users, run 2 of 3. |
| `load_fast_100_run3.json` | paper-latest | 2026-10-10 | B5: same, 100 concurrent users, run 3 of 3. |
| `before_connlimit_fix/load_fast_1.json` | paper-latest | 2026-10-10 | B5: 1 concurrent user, measured before the read-only role connection-limit fix. |
| `before_connlimit_fix/load_fast_10.json` | paper-latest | 2026-10-10 | B5: 10 concurrent users, before the connection-limit fix. |
| `before_connlimit_fix/load_fast_20.json` | paper-latest | 2026-10-10 | B5: 20 concurrent users, before the connection-limit fix: 6 of 20 requests failed ("AI service is temporarily unavailable"). |
| `before_connlimit_fix/load_fast_50.json` | paper-latest | 2026-10-10 | B5: 50 concurrent users, before the connection-limit fix: 11 of 50 requests failed. |
| `freshness.json` | paper-latest | 2026-10-10 | B6: whether a row inserted, then deleted, directly in the college database is reflected in the next fast-path answer (both reflected). |
| `security_postgres.json` | paper-latest | 2026-10-10 | B4 (PostgreSQL): 64 hostile statements in 10 categories against the SQL guard (64 rejected); guard false-positive rate on 104 distinct logged model-written statements (0 rejected, 11 execution errors); 26 write/escape statements sent directly to the read-only role with the guard skipped (database state unchanged). |
| `field_logs.json` | paper-latest | 2026-10-10 | B7: aggregates over the system's own logs for 2026-07-26 to 2026-09-07 (992 requests): latency per route, per-stage time, model token throughput, verification outcomes, injection flags. |
| `e2e_nomodel_smoke.json` | paper-latest | 2026-10-10 | B8: end-to-end smoke run of 8 cases through `POST /api/ask/` (label "nomodel"): 6 fast-path cases correct, 1 decline case scored WRONG, 1 security case scored CHECK; database unchanged. |

Answer acceptance: cosine similarity >= 0.75 between nomic-embed-text embeddings of answer and ground truth (backend/experiments/similarity.py). Router embedding tier: tau_route = 0.62 (backend/router_agent/fast_router.py).
