# Copyright (c) 2026 Yash Garad. All rights reserved.

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from common.allowlist import ALLOWED_TABLES

# ALLOWED_TABLES is no longer defined here. common/allowlist.py is the single
# source of truth, and this command is what turns it into actual Postgres
# grants — which in turn is what the sync worker discovers at startup. See that
# module for why the sync worker derives rather than imports.



# Pool max (24, sql_agent/db.py) + sync worker + operator headroom. Kept equal
# to db/sql/create_rag_agent_ro.sql; test_readonly_role asserts both.
CONNECTION_LIMIT = 32

class Command(BaseCommand):
    help = (
        "Create or refresh the read-only 'rag_agent_ro' database role and its "
        "SELECT grants, idempotently, using the password from "
        "RAG_AGENT_RO_PASSWORD. Runs on backend startup so the whole stack "
        "comes up self-configured. Executes as the app's DB owner."
    )

    def handle(self, *args, **options):
        password = os.getenv("RAG_AGENT_RO_PASSWORD")
        if not password:
            raise CommandError("RAG_AGENT_RO_PASSWORD is not set; cannot create the read-only role.")

        db_name = connection.settings_dict["NAME"]
        pw_literal = password.replace("'", "''")  # escape single quotes for the SQL string literal
        grant_targets = ", ".join(f"public.{t}" for t in ALLOWED_TABLES)

        statements = [
            # Create the role only if it doesn't already exist.
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'rag_agent_ro') THEN
                    CREATE ROLE rag_agent_ro LOGIN PASSWORD '{pw_literal}';
                END IF;
            END
            $$;
            """,
            # Keep the password in sync with .env even if the role pre-existed.
            f"ALTER ROLE rag_agent_ro LOGIN PASSWORD '{pw_literal}';",
            # CONNECTION LIMIT must exceed the backend's pool (RAG_AGENT_RO_POOL_MAX,
            # 24) plus the sync worker. This said 10 after the pool was raised
            # to 24 and db/sql/create_rag_agent_ro.sql to 32 - and because THIS
            # command runs on every container start (entrypoint.sh), it put the
            # limit back to 10 on every boot. Measured 2026-10-10: at 20
            # concurrent fast-path users, 6 failed with "too many connections
            # for role rag_agent_ro" and fell through to the model; at 50, 11.
            f"ALTER ROLE rag_agent_ro NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT {CONNECTION_LIMIT};",
            "ALTER ROLE rag_agent_ro SET default_transaction_read_only = on;",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC;",
            f'GRANT CONNECT ON DATABASE "{db_name}" TO rag_agent_ro;',
            "GRANT USAGE ON SCHEMA public TO rag_agent_ro;",
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM rag_agent_ro;",
            f"GRANT SELECT ON {grant_targets} TO rag_agent_ro;",
        ]

        with connection.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)

        self.stdout.write(self.style.SUCCESS(
            f"rag_agent_ro configured with SELECT on {len(ALLOWED_TABLES)} tables."
        ))
