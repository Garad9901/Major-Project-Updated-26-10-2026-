"""B5 - concurrent users against the running server, distinct accounts.

Every simulated user is a separate account with its own session (bench001..),
already signed in before the clock starts, and asks one question at the same
moment as the others. The question mix is drawn from the real replayed traffic
that the deterministic tiers serve, so this measures the path most requests
actually take - not the model, which is measured separately.

usage: python load.py <users> <label> [questions.json] [--regenerate]
Output: results/load_<label>.json
"""
import json
import os
import statistics
import sys
import threading
import time

import _django  # noqa: F401  (results dir)
from client import Client

PASSWORD = os.environ.get("BENCH_PASSWORD")
if not PASSWORD:
    sys.exit("BENCH_PASSWORD is not set. Set it to the benchmark accounts' password before running this script.")


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def main():
    users = int(sys.argv[1])
    label = sys.argv[2]
    qfile = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else None
    regenerate = "--regenerate" in sys.argv
    if qfile:
        questions = json.load(open(qfile))
    else:
        questions = [
            "How many faculty are in Medicine?", "How many departments are there?",
            "How many faculty hold the Lecturer rank?", "What is the average age of faculty?",
            "How many faculty have a High Development Need?",
            "How many faculty in the Medicine department are from Deemed universities?",
            "What is the average AI tool adoption score for Professors?",
            "How many Associate Professor faculty are in the Education department?",
            "How many faculty records come from Private universities?",
            "What is the average teaching effectiveness score?",
        ]
    clients = [None] * users

    def login(i):
        clients[i] = Client(f"bench{i + 1:03d}", PASSWORD)

    ts = [threading.Thread(target=login, args=(i,)) for i in range(users)]
    [t.start() for t in ts]
    [t.join() for t in ts]

    results = [None] * users
    barrier = threading.Barrier(users)

    def go(i):
        barrier.wait()
        results[i] = clients[i].ask(questions[i % len(questions)], regenerate=regenerate)

    t0 = time.perf_counter()
    ts = [threading.Thread(target=go, args=(i,)) for i in range(users)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = (time.perf_counter() - t0) * 1000

    ok = [r for r in results if r["status"] == 200 and not r["error"]]
    lat = [r["total_ms"] for r in ok]
    routes = {}
    for r in results:
        routes[r["route"] or "none"] = routes.get(r["route"] or "none", 0) + 1
    summary = {
        "label": label, "users": users, "distinct_accounts": users, "wall_ms": round(wall),
        "ok": len(ok), "failed": users - len(ok), "routes": routes,
        "p50_ms": round(pct(lat, 0.5), 1) if lat else None,
        "p95_ms": round(pct(lat, 0.95), 1) if lat else None,
        "max_ms": round(max(lat), 1) if lat else None,
        "mean_ms": round(statistics.mean(lat), 1) if lat else None,
        "throughput_rps": round(len(ok) / (wall / 1000), 1),
    }
    json.dump({"summary": summary, "results": results}, open(f"{_django.RESULTS}/load_{label}.json", "w"), indent=1)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
