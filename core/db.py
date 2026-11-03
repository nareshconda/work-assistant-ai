"""
Database connections and query execution.

Supports PostgreSQL (psycopg2) and Azure Synapse / SQL Server (pyodbc) behind
one interface, so the assistant does not care which you use.

Connections come from .env. The simple case is one database:

    DB_KIND=postgres
    DB_HOST=... DB_PORT=5432 DB_NAME=... DB_USER=... DB_PASSWORD=...

For several, add a connections.json (gitignored) -- see connections.example.json.

READ-ONLY BY DEFAULT. Statements that modify anything are rejected before they
reach the server, and PostgreSQL connections additionally set
default_transaction_read_only. Set DB_ALLOW_WRITES=true only if you mean it.
"""

import json
import os
import re

SQL_TABLE_SENTINEL = "@@WAAI_TABLE@@"

ALLOW_WRITES = os.environ.get("DB_ALLOW_WRITES", "").strip().lower() in (
    "1", "true", "yes")
STATEMENT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "300000"))

_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|create|copy|"
    r"call|do|vacuum|merge|reindex|cluster|refresh|listen|notify|set|reset|"
    r"exec|execute|backup|restore|bulk)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _from_env():
    kind = os.environ.get("DB_KIND", "").strip().lower()
    host = os.environ.get("DB_HOST", "").strip()
    if not kind or not host:
        return {}
    name = os.environ.get("DB_LABEL", "").strip() or "default"
    return {name: {
        "kind": kind,
        "host": host,
        "port": os.environ.get("DB_PORT", "").strip(),
        "database": os.environ.get("DB_NAME", "").strip(),
        "user": os.environ.get("DB_USER", "").strip(),
        "password": os.environ.get("DB_PASSWORD", ""),
        "driver": os.environ.get("DB_ODBC_DRIVER",
                                 "ODBC Driver 18 for SQL Server").strip(),
        "description": os.environ.get("DB_DESCRIPTION", "").strip(),
    }}


def _from_file():
    path = os.environ.get("DB_CONNECTIONS_FILE", "connections.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"WARNING: could not read {path}: {e}")
        return {}
    out = {}
    for name, cfg in (data or {}).items():
        if isinstance(cfg, dict) and cfg.get("host"):
            out[str(name)] = cfg
    return out


CONNECTIONS = {}
CONNECTIONS.update(_from_env())
CONNECTIONS.update(_from_file())      # the file wins on a name clash

DEFAULT_CONNECTION = (os.environ.get("DB_DEFAULT", "").strip()
                      or (next(iter(CONNECTIONS)) if CONNECTIONS else ""))


def describe():
    """Connection list for /api/debug and the system prompt. No passwords."""
    return {
        "default": DEFAULT_CONNECTION,
        "allow_writes": ALLOW_WRITES,
        "connections": {
            name: {"kind": c.get("kind"), "host": c.get("host"),
                   "database": c.get("database"),
                   "description": c.get("description", "")}
            for name, c in CONNECTIONS.items()
        },
    }


def connection_names():
    return list(CONNECTIONS)


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
def validate_sql(query):
    """Allow a single read-only statement. Returns the cleaned SQL."""
    q = (query or "").strip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if not q:
        raise ValueError("Empty query.")
    if ";" in q:
        raise ValueError("Only a single SQL statement is allowed.")
    if ALLOW_WRITES:
        return q
    if not re.match(r"^(select|with)\b", q, re.IGNORECASE):
        raise ValueError(
            "Read-only mode: only SELECT (or WITH ... SELECT) is allowed. "
            "Set DB_ALLOW_WRITES=true in .env to permit writes.")
    m = _WRITE_KEYWORDS.search(q)
    if m:
        raise ValueError(
            f"Read-only mode: the statement contains '{m.group(1)}'. "
            f"Set DB_ALLOW_WRITES=true in .env to permit writes.")
    return q


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------
ACTIVE = {"conn": None}


def _connect(cfg):
    kind = (cfg.get("kind") or "postgres").lower()

    if kind in ("postgres", "postgresql", "pg"):
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError(
                "psycopg2 is not installed. pip install psycopg2-binary")
        opts = f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
        if not ALLOW_WRITES:
            opts += " -c default_transaction_read_only=on"
        return psycopg2.connect(
            host=cfg["host"], port=int(cfg.get("port") or 5432),
            dbname=cfg.get("database") or "", user=cfg.get("user") or "",
            password=cfg.get("password") or "", options=opts,
        ), "postgres"

    if kind in ("synapse", "sqlserver", "mssql", "azure"):
        try:
            import pyodbc
        except ImportError:
            raise RuntimeError(
                "pyodbc is not installed. pip install pyodbc, and install a "
                "system ODBC driver (see requirements.txt).")
        driver = cfg.get("driver") or "ODBC Driver 18 for SQL Server"
        port = cfg.get("port") or 1433
        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={cfg['host']},{port}",
            f"DATABASE={cfg.get('database','')}",
            f"UID={cfg.get('user','')}",
            f"PWD={cfg.get('password','')}",
            "Encrypt=yes",
            "TrustServerCertificate=no",
            "Connection Timeout=30",
        ]
        if cfg.get("extra_odbc"):
            parts.append(str(cfg["extra_odbc"]))
        conn = pyodbc.connect(";".join(parts), timeout=30)
        if not ALLOW_WRITES:
            # Synapse has no session-level read-only switch; validate_sql is the
            # guard. Use a read-only database user for real protection.
            pass
        return conn, "synapse"

    raise RuntimeError(
        f"Unknown DB_KIND {kind!r}. Use 'postgres' or 'synapse'.")


def cancel_active():
    """Cancel an in-flight query (called by the chat's Stop button)."""
    conn = ACTIVE.get("conn")
    if conn is None:
        return False
    try:
        conn.cancel()          # psycopg2
        return True
    except Exception:
        pass
    try:
        conn.close()           # pyodbc has no cancel; closing unblocks it
        return True
    except Exception:
        return False


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<binary>"
    return str(v)


def run_query(query, connection=""):
    """Run a read-only query and emit the result as a table to the chat."""
    if not CONNECTIONS:
        print("-> No database is configured. Copy .env.example to .env and "
              "fill in the DB_* block.")
        return "ERROR: no database configured."

    name = (connection or DEFAULT_CONNECTION or "").strip()
    cfg = CONNECTIONS.get(name)
    if cfg is None:
        print(f"-> Unknown connection {name!r}. "
              f"Available: {', '.join(CONNECTIONS) or '(none)'}")
        return f"ERROR: unknown connection {name!r}."

    # A refusal is an expected outcome, not a crash: report it as a plain
    # message so the chat shows the reason rather than a stack trace.
    try:
        safe = validate_sql(query)
    except ValueError as e:
        print(f"-> Refused: {e}")
        return f"ERROR: {e}"

    mode = "read-write" if ALLOW_WRITES else "read-only"
    print(f"-> Running on '{name}' ({cfg.get('kind')}, {mode}):")
    print(safe)
    print()

    try:
        conn, kind = _connect(cfg)
    except Exception as e:
        # Unreachable host, bad password, missing ODBC driver. Say which,
        # plainly, instead of dumping a traceback into the chat.
        print(f"-> Could not connect to '{name}': {e}")
        print("   Check the DB_ settings in .env (host, port, user, password),")
        print("   and that this machine can reach the database.")
        return f"ERROR: could not connect to '{name}': {e}"

    ACTIVE["conn"] = conn
    try:
        cur = conn.cursor()
        if kind == "synapse":
            try:
                cur.timeout = max(1, STATEMENT_TIMEOUT_MS // 1000)
            except Exception:
                pass
        cur.execute(safe)
        rows = cur.fetchall() if cur.description else []
        colnames = [d[0] for d in cur.description] if cur.description else []
        cur.close()
    except Exception as e:
        # A SQL error is information the model needs in order to fix its query,
        # so surface the database's own message rather than a traceback.
        print(f"-> SQL error: {e}")
        return f"ERROR: {e}"
    finally:
        ACTIVE["conn"] = None
        try:
            conn.close()
        except Exception:
            pass

    if not rows:
        print("-> Query returned 0 rows.")
        return "0 rows returned."

    # One structured line carries the whole result set to the UI, so the console
    # log stays small even for thousands of rows.
    payload = {"headers": colnames,
               "rows": [[_cell(v) for v in r] for r in rows]}
    print(SQL_TABLE_SENTINEL + json.dumps(payload, ensure_ascii=False))
    print(f"-> {len(rows)} row(s) returned.")
    return f"{len(rows)} rows returned."


def list_tables(connection=""):
    """List tables available on a connection, so the model can orient itself."""
    name = (connection or DEFAULT_CONNECTION or "").strip()
    cfg = CONNECTIONS.get(name)
    if cfg is None:
        print(f"-> Unknown connection {name!r}.")
        return f"ERROR: unknown connection {name!r}."
    kind = (cfg.get("kind") or "postgres").lower()
    if kind in ("postgres", "postgresql", "pg"):
        q = ("SELECT table_schema, table_name FROM information_schema.tables "
             "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
             "ORDER BY table_schema, table_name")
    else:
        q = ("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
             "WHERE TABLE_TYPE = 'BASE TABLE' "
             "ORDER BY TABLE_SCHEMA, TABLE_NAME")
    return run_query(q, connection=name)


def describe_table(table, connection=""):
    """Column names and types for one table."""
    name = (connection or DEFAULT_CONNECTION or "").strip()
    cfg = CONNECTIONS.get(name)
    if cfg is None:
        print(f"-> Unknown connection {name!r}.")
        return f"ERROR: unknown connection {name!r}."
    bare = str(table).split(".")[-1].strip().strip('"[]')
    if not re.match(r"^[A-Za-z0-9_]+$", bare):
        print("-> Invalid table name.")
        return "ERROR: invalid table name."
    kind = (cfg.get("kind") or "postgres").lower()
    if kind in ("postgres", "postgresql", "pg"):
        q = ("SELECT column_name, data_type, is_nullable "
             f"FROM information_schema.columns WHERE table_name = '{bare}' "
             "ORDER BY ordinal_position")
    else:
        q = ("SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
             f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{bare}' "
             "ORDER BY ORDINAL_POSITION")
    return run_query(q, connection=name)
