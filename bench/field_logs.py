"""B7 - what the logs of the system's own use say (26 Jul - 7 Sep 2026).

Read-only aggregates over the restored production-style database: request mix
and latency per route (audit_log), per-stage time and token throughput
(query_profile), and verification outcomes (verification_logs). No user
identifiers are read.

Output: results/field_logs.json
"""
import json

import _django
from django.db import connection


def q(sql):
    with connection.cursor() as cur:
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


P = ("count(*) n, round(percentile_cont(0.5) within group (order by {c})::numeric) p50, "
     "round(percentile_cont(0.95) within group (order by {c})::numeric) p95, round(avg({c})::numeric) mean")

out = {
    "window": q("select min(created_at)::date::text first, max(created_at)::date::text last, count(*) requests from audit_log")[0],
    "audit_by_route": q(f"select coalesce(nullif(route,''),'(none)') route, {P.format(c='latency_ms')} "
                        "from audit_log group by 1 order by n desc"),
    "injection_flagged": q("select count(*) n from audit_log where injection_flags <> '' and injection_flags <> '[]'")[0],
    "profile_by_route": q(f"select route, cached, {P.format(c='total_ms')} from query_profile group by 1,2 order by n desc"),
    "profile_stage_means_uncached": q(
        "select route, count(*) n, round(avg(router_ms)::numeric) router, round(avg(sql_ms)::numeric) sql, "
        "round(avg(rag_ms)::numeric) rag, round(avg(synthesis_ms)::numeric) synthesis, "
        "round(avg(verification_ms)::numeric) verification, round(avg(ttft_ms)::numeric) ttft, "
        "round(avg(total_ms)::numeric) total from query_profile where cached in ('','miss','no','none') "
        "group by 1 order by n desc"),
    "throughput": q("select count(*) n, round((sum(prompt_tokens)/nullif(sum(prompt_ms),0)*1000)::numeric,1) prefill_tok_s, "
                    "round((sum(gen_tokens)/nullif(sum(gen_ms),0)*1000)::numeric,2) gen_tok_s, "
                    "round(avg(prompt_tokens)::numeric) mean_prompt_tokens, round(avg(gen_tokens)::numeric) mean_gen_tokens "
                    "from query_profile where prompt_ms > 0 and gen_ms > 0"),
    "verification_tier": q("select verification_tier, count(*) n from query_profile group by 1 order by 2 desc"),
    "cached_values": q("select cached, count(*) n from query_profile group by 1 order by 2 desc"),
    "verification_claims": q("select action_taken, supported, count(*) n, round(avg(confidence)::numeric,3) mean_conf "
                             "from verification_logs group by 1,2 order by 3 desc"),
    "verification_runs": q("select count(distinct run_id) runs, count(*) claims, "
                           "sum(case when supported then 0 else 1 end) unsupported_claims, "
                           "count(distinct run_id) filter (where not supported) runs_with_unsupported, "
                           "count(distinct run_id) filter (where original_answer <> final_answer) runs_corrected "
                           "from verification_logs")[0],
    "verification_by_route": q("select route, count(distinct run_id) runs, count(*) claims, "
                               "sum(case when supported then 0 else 1 end) unsupported from verification_logs group by 1"),
}
json.dump(out, open(f"{_django.RESULTS}/field_logs.json", "w"), indent=1, default=str)
print(json.dumps(out, indent=1, default=str))
