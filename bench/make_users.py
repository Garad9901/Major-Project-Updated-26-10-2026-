"""Provision N distinct benchmark accounts (bench001..benchNNN) through the
project's own provision_user, then clear must_change_password so a load test
can sign in directly. Distinct accounts matter: per-user throttles would
otherwise count every simulated user as one (MASTER_PROMPT section 7)."""
import os
import sys

import _django  # noqa: F401
from accounts.management.commands._provision import ProvisionError, provision_user
from accounts.models import UserProfile

N = int(sys.argv[1]) if len(sys.argv) > 1 else 60
PASSWORD = os.environ.get("BENCH_PASSWORD")
if not PASSWORD:
    sys.exit("BENCH_PASSWORD is not set. Set it to the benchmark accounts' password before running this script.")
made = 0
for i in range(1, N + 1):
    name = f"bench{i:03d}"
    try:
        provision_user(username=name, role="staff", password=PASSWORD, created_by="bench")
        made += 1
    except ProvisionError:
        pass
UserProfile.objects.filter(user__username__startswith="bench").update(must_change_password=False)
print(f"created {made}, total bench accounts {UserProfile.objects.filter(user__username__startswith='bench').count()}")
