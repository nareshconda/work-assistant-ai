"""
Tool registry.

Builds the function declarations the model sees. Two sources:
  * the database tools (run_sql_query, list_tables, describe_table)
  * one tool per script discovered in scripts/

Scripts become real, individually-named tools rather than one generic
run_script(name), because a model picks far more reliably from
"script_disk_usage: Show disk usage for a path" than from a string argument it
has to guess. Above MAX_SCRIPT_TOOLS we fall back to a generic runner so a big
folder cannot blow up the schema.
"""

import difflib
import inspect

from . import db, scripts

MAX_SCRIPT_TOOLS = 40

_NO_PARAMS = {"type": "object", "properties": {}}


def normalize_args(fn, args):
    """Map model-supplied argument names onto the function's real parameters.

    Models rename things (table_name vs table, max_rows vs limit). Without this a
    harmless rename becomes TypeError: unexpected keyword argument.
    """
    if not args:
        return args or {}
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return args
    params = list(sig.parameters)
    if not params:
        return {}
    if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        return args                      # **kwargs takes anything

    def norm(s):
        return str(s).lower().replace("_", "").replace("-", "").replace(" ", "")

    pnorm = {p: norm(p) for p in params}
    out, leftover = {}, {}
    for k, v in args.items():
        (out if k in params else leftover)[k] = v
    for k in list(leftover):
        hit = next((p for p in params if p not in out and pnorm[p] == norm(k)),
                   None)
        if hit:
            out[hit] = leftover.pop(k)
    for k in list(leftover):
        pool = [pnorm[p] for p in params if p not in out]
        cand = difflib.get_close_matches(norm(k), pool, n=1, cutoff=0.6)
        if cand:
            hit = next(p for p in params if p not in out and pnorm[p] == cand[0])
            out[hit] = leftover.pop(k)
    required = [n for n, prm in sig.parameters.items()
                if prm.default is inspect.Parameter.empty
                and prm.kind in (prm.POSITIONAL_OR_KEYWORD, prm.KEYWORD_ONLY)]
    missing = [p for p in required if p not in out]
    if len(missing) == 1 and len(leftover) == 1:
        out[missing[0]] = next(iter(leftover.values()))
    return out


# ---------------------------------------------------------------------------
# Database tools
# ---------------------------------------------------------------------------
def _db_tools():
    names = db.connection_names()
    if not names:
        return {}
    where = (f"Available connections: {', '.join(names)}. "
             f"Default: '{db.DEFAULT_CONNECTION}'.")
    mode = ("Writes are ENABLED." if db.ALLOW_WRITES
            else "READ-ONLY: only SELECT / WITH are permitted; anything that "
                 "modifies data is rejected before it reaches the server.")
    conn_param = {
        "type": "string",
        "description": f"Which connection to use. {where} Omit for the default.",
    }
    return {
        "run_sql_query": {
            "fn": db.run_query,
            "declaration": {
                "name": "run_sql_query",
                "description": (
                    "Run a SQL query and show the result table to the user. "
                    f"{mode} {where}\n"
                    "Write the SQL yourself from the schema, or pass the "
                    "user's SQL VERBATIM if they supplied it. The query and "
                    "the result table are BOTH displayed automatically -- do "
                    "not repeat them in your reply, just add a one-line "
                    "takeaway. If it errors, read the error, fix the SQL and "
                    "retry (max 2 retries).\n"
                    "Do not add LIMIT unless the user asked for one. If you "
                    "are unsure of the schema, call list_tables or "
                    "describe_table first -- never guess a column name. If a "
                    "condition cannot be expressed, say so rather than "
                    "quietly answering a different question."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "The SQL statement to run."},
                        "connection": conn_param,
                    },
                    "required": ["query"],
                },
            },
        },
        "list_tables": {
            "fn": db.list_tables,
            "declaration": {
                "name": "list_tables",
                "description": ("List the tables on a connection. Use this "
                                "before writing SQL against an unfamiliar "
                                f"database. {where}"),
                "parameters": {"type": "object",
                               "properties": {"connection": conn_param}},
            },
        },
        "describe_table": {
            "fn": db.describe_table,
            "declaration": {
                "name": "describe_table",
                "description": ("Show the columns and types of one table. Use "
                                "this instead of guessing column names."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string",
                                  "description": "Table name (no schema prefix needed)."},
                        "connection": conn_param,
                    },
                    "required": ["table"],
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Script tools
# ---------------------------------------------------------------------------
def _make_script_fn(script_key):
    def _run(**kwargs):
        return scripts.run(script_key, **kwargs)
    _run.__name__ = f"script_{script_key}"
    return _run


def _script_tools():
    out = {}
    found = scripts.SCRIPTS
    if not found:
        return out

    if len(found) > MAX_SCRIPT_TOOLS:
        # Too many to declare individually: one generic runner instead.
        def run_script(script_name, **kwargs):
            return scripts.run(script_name, **kwargs)
        out["run_script"] = {
            "fn": run_script,
            "declaration": {
                "name": "run_script",
                "description": (
                    f"Run one of the {len(found)} scripts in the scripts "
                    "folder. Call list_scripts first to see the names and "
                    "what each one does."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script_name": {"type": "string",
                                        "description": "Name from list_scripts."},
                    },
                    "required": ["script_name"],
                },
            },
        }
        return out

    for key, info in found.items():
        props = {a: {"type": "string", "description": d}
                 for a, d in info["args"].items()}
        desc = info["description"]
        if info["confirm"]:
            desc += (" This script asks the user to confirm before it runs; "
                     "that prompt is relayed automatically.")
        out[info["tool_name"]] = {
            "fn": _make_script_fn(key),
            "declaration": {
                "name": info["tool_name"],
                "description": f"{desc} (runs {info['relpath']})",
                "parameters": {"type": "object", "properties": props},
            },
        }
    return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def build():
    reg = {
        "list_scripts": {
            "fn": scripts.list_scripts,
            "declaration": {
                "name": "list_scripts",
                "description": ("Show every script available in the scripts "
                                "folder, what each one does and what "
                                "arguments it takes."),
                "parameters": _NO_PARAMS,
            },
        },
    }
    reg.update(_db_tools())
    reg.update(_script_tools())
    return reg


TOOLS = build()


def refresh():
    """Rescan scripts and rebuild the registry (used by /api/rescan)."""
    global TOOLS, FUNCTION_DECLARATIONS, OPENAI_TOOLS
    scripts.rescan()
    TOOLS = build()
    FUNCTION_DECLARATIONS = [t["declaration"] for t in TOOLS.values()]
    OPENAI_TOOLS = [{"type": "function", "function": d}
                    for d in FUNCTION_DECLARATIONS]
    return TOOLS


FUNCTION_DECLARATIONS = [t["declaration"] for t in TOOLS.values()]
OPENAI_TOOLS = [{"type": "function", "function": d}
                for d in FUNCTION_DECLARATIONS]


def cancel_all():
    """Best-effort cancel of anything running (the Stop button)."""
    ok = []
    try:
        ok.append(("sql", db.cancel_active()))
    except Exception:
        pass
    try:
        ok.append(("script", scripts.cancel_active()))
    except Exception:
        pass
    return ok
