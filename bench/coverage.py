"""B2 - deterministic-tier coverage on the real logged traffic.

Replays every question in audit_log through the two model-free tiers in the
order the orchestrator tries them (pre-computed lookup, then the fast path) and
reports the share each serves. Questions from the capacity-test pool are
reported separately so synthetic load does not flatter the figure.

Output: results/coverage.json, results/coverage_per_question.csv
"""
import csv
import json
import time
from collections import Counter

import _django  # noqa: F401
from django.db import connection

from fast_path import service as fp
from orchestrator import precompute
from sql_agent import db as college_db

DEPTS = ["Engineering", "Computer Science", "Science", "Management", "Education",
         "Arts and Humanities", "Social Science", "Medicine"]
RANKS = ["Professor", "Associate Professor", "Assistant Professor", "Lecturer"]
POOL = set()
for d in DEPTS:
    POOL.add(f"How many faculty are in the {d} department?")
for r in RANKS:
    POOL.add(f"How many faculty hold the {r} rank?")
for lv in ["Expert", "Advanced", "Intermediate", "Basic"]:
    POOL.add(f"How many faculty have a competency level of {lv}?")
for u in ["Public", "Private", "Deemed"]:
    POOL.add(f"How many faculty records come from {u} universities?")
for d in DEPTS:
    for r in RANKS:
        POOL.add(f"How many {r} faculty are in the {d} department?")


def main():
    with connection.cursor() as cur:
        cur.execute("SELECT question FROM audit_log ORDER BY id")
        asked = [r[0] for r in cur.fetchall()]

    counts = Counter(asked)
    verdict = {}
    lat = []
    for q in counts:
        t0 = time.perf_counter()
        pre = precompute.match(q, college_db.connection)
        fast = fp.try_answer(q, college_db.connection) if pre is None else None
        lat.append((time.perf_counter() - t0) * 1000)
        if pre is not None:
            verdict[q] = ("PRECOMPUTABLE", pre[0].name)
        elif fast is not None:
            verdict[q] = ("FAST", fast.intent)
        else:
            verdict[q] = ("MODEL", "")

    def summarise(selector):
        qs = [q for q in counts if selector(q)]
        traffic = sum(counts[q] for q in qs)
        out = {"distinct": len(qs), "requests": traffic}
        for tier in ("FAST", "PRECOMPUTABLE", "MODEL"):
            d = [q for q in qs if verdict[q][0] == tier]
            out[tier] = {"distinct": len(d), "requests": sum(counts[q] for q in d),
                         "share_requests": round(sum(counts[q] for q in d) / traffic, 4) if traffic else None,
                         "share_distinct": round(len(d) / len(qs), 4) if qs else None}
        out["model_free_share_requests"] = round(
            (out["FAST"]["requests"] + out["PRECOMPUTABLE"]["requests"]) / traffic, 4) if traffic else None
        return out

    report = {
        "source": "audit_log replay (restored backup 2026-09-07)",
        "all_traffic": summarise(lambda q: True),
        "genuine_traffic": summarise(lambda q: q not in POOL),
        "load_test_pool": summarise(lambda q: q in POOL),
        "intent_mix_genuine": Counter(verdict[q][1] or "MODEL" for q in counts if q not in POOL
                                      for _ in range(counts[q])),
        "routing_decision_ms": {"n": len(lat), "p50": sorted(lat)[len(lat) // 2],
                                "p95": sorted(lat)[int(len(lat) * 0.95)], "max": max(lat)},
    }
    with open(f"{_django.RESULTS}/coverage.json", "w") as f:
        json.dump(report, f, indent=2, default=dict)
    with open(f"{_django.RESULTS}/coverage_per_question.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["question", "times_asked", "tier", "intent_or_shape", "load_test_pool"])
        for q, n in counts.most_common():
            w.writerow([q, n, verdict[q][0], verdict[q][1], q in POOL])
    print(json.dumps({k: v for k, v in report.items() if k != "intent_mix_genuine"}, indent=1, default=dict))


if __name__ == "__main__":
    main()
