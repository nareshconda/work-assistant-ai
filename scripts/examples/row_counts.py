#!/usr/bin/env python3
# @name: row_counts
# @description: Count rows in every table of the default database and print them largest first. Use for "how big are my tables" or "which table has the most rows".
# @arg limit: How many tables to show. Defaults to 25.
# @timeout: 300
"""
Shows how a script can reuse the app's own database configuration instead of
carrying its own credentials: it imports core.db, which reads .env.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from core import db  # noqa: E402

try:
    from tabulate import tabulate
except ImportError:
    tabulate = None


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 25

    name = db.DEFAULT_CONNECTION
    cfg = db.CONNECTIONS.get(name)
    if not cfg:
        print("No database configured. Fill in the DB_ block in .env.")
        return 1

    kind = (cfg.get("kind") or "postgres").lower()
    if kind in ("postgres", "postgresql", "pg"):
        q = """
            SELECT schemaname AS schema, relname AS table, n_live_tup AS rows
            FROM pg_stat_user_tables
            ORDER BY n_live_tup DESC
        """
    else:
        q = """
            SELECT s.name AS [schema], t.name AS [table], SUM(p.rows) AS [rows]
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.partitions p ON p.object_id = t.object_id
            WHERE p.index_id IN (0, 1)
            GROUP BY s.name, t.name
            ORDER BY SUM(p.rows) DESC
        """

    conn, _ = db._connect(cfg)
    try:
        cur = conn.cursor()
        cur.execute(db.validate_sql(q))
        rows = cur.fetchall()[:limit]
        headers = [d[0] for d in cur.description]
        cur.close()
    finally:
        conn.close()

    if not rows:
        print("No tables found.")
        return 0

    data = [[str(c) for c in r] for r in rows]
    if tabulate:
        print(tabulate(data, headers=headers, tablefmt="grid"))
    else:
        for r in data:
            print("  ".join(r))
    print(f"\n{len(data)} table(s) shown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
