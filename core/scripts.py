"""
Script auto-discovery.

Drop a .sh or .py file into scripts/ and the assistant can run it. No
registration, no restart needed beyond a rescan -- the folder IS the config.

A script describes itself with header comments, which become the tool
description the model reads when deciding what to call:

    #!/usr/bin/env bash
    # @name: disk_usage
    # @description: Show disk usage for a path, largest directories first.
    # @arg path: Directory to measure. Defaults to the current directory.
    # @confirm: false
    # @timeout: 60

Everything is optional. With no header at all you still get a runnable tool
named after the file, the model just has less idea when to use it.

SAFETY
  * Only files inside scripts/ run. The resolved path is checked, so a name
    like "../../etc/passwd" cannot escape, and symlinks pointing outside are
    refused.
  * Arguments are passed as argv, never interpolated into a shell string, so a
    value containing ; or && is an argument and not a second command.
  * Every run has a timeout.
  * @confirm: true makes the assistant ask you before running it. Put that on
    anything destructive.
"""

import os
import re
import shlex
import subprocess
import sys
import threading
import time

SCRIPTS_DIR = os.path.abspath(
    os.environ.get("SCRIPTS_DIR")
    or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts"))

DEFAULT_TIMEOUT = int(os.environ.get("SCRIPT_TIMEOUT", "300"))
SCRIPTS_ENABLED = os.environ.get("SCRIPTS_ENABLED", "true").strip().lower() in (
    "1", "true", "yes")

RUNNERS = {".sh": ["bash"], ".bash": ["bash"], ".py": [sys.executable],
           ".zsh": ["zsh"]}

_HEADER_RE = re.compile(r"^\s*(?:#|//)\s*@(\w+)\s*:\s*(.*)$")
_ARG_RE = re.compile(r"^\s*(?:#|//)\s*@arg\s+([A-Za-z_]\w*)\s*:\s*(.*)$")

_ACTIVE = {"proc": None}
_LOCK = threading.Lock()


def _safe_name(path):
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    name = re.sub(r"[^A-Za-z0-9_]", "_", stem).strip("_").lower()
    return name or "script"


def _parse_header(path, max_lines=40):
    """Read @key: value lines from the top of the file."""
    meta = {"args": {}, "arg_order": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    break
                if i == 0 and line.startswith("#!"):
                    continue
                stripped = line.strip()
                if stripped and not stripped.startswith(("#", "//")):
                    break           # past the header block
                m = _ARG_RE.match(line)
                if m:
                    meta["args"][m.group(1)] = m.group(2).strip()
                    meta["arg_order"].append(m.group(1))
                    continue
                m = _HEADER_RE.match(line)
                if m and m.group(1).lower() != "arg":
                    meta[m.group(1).lower()] = m.group(2).strip()
    except Exception:
        pass
    return meta


def discover():
    """Find runnable scripts. Returns {tool_name: info}."""
    found = {}
    if not SCRIPTS_ENABLED or not os.path.isdir(SCRIPTS_DIR):
        return found
    for root, dirs, files in os.walk(SCRIPTS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in RUNNERS or fn.startswith("."):
                continue
            full = os.path.join(root, fn)
            try:
                real = os.path.realpath(full)
                if os.path.commonpath([real, os.path.realpath(SCRIPTS_DIR)]) \
                        != os.path.realpath(SCRIPTS_DIR):
                    continue        # symlink escaping the folder
            except Exception:
                continue
            meta = _parse_header(full)
            name = _safe_name(meta.get("name") or fn)
            while name in found:
                name += "_x"
            rel = os.path.relpath(full, SCRIPTS_DIR)
            found[name] = {
                "tool_name": f"script_{name}",
                "file": full,
                "relpath": rel,
                "ext": ext,
                "description": meta.get("description", "").strip()
                               or f"Run the script {rel}.",
                "args": meta.get("args", {}),
                "arg_order": meta.get("arg_order", []),
                "confirm": str(meta.get("confirm", "")).strip().lower()
                           in ("1", "true", "yes"),
                "timeout": int(meta.get("timeout") or DEFAULT_TIMEOUT),
            }
    return found


SCRIPTS = discover()


def rescan():
    """Pick up newly added scripts without a restart."""
    global SCRIPTS
    SCRIPTS = discover()
    return SCRIPTS


def describe():
    return {
        "enabled": SCRIPTS_ENABLED,
        "dir": SCRIPTS_DIR,
        "count": len(SCRIPTS),
        "scripts": [
            {"name": k, "tool": v["tool_name"], "file": v["relpath"],
             "description": v["description"], "args": list(v["args"]),
             "confirm": v["confirm"], "timeout": v["timeout"]}
            for k, v in SCRIPTS.items()
        ],
    }


def list_scripts():
    """Tool: show what is available in the scripts folder."""
    rescan()
    if not SCRIPTS:
        print(f"-> No scripts found in {SCRIPTS_DIR}")
        print("   Drop a .sh or .py file in there and ask me again.")
        return "No scripts available."
    try:
        from tabulate import tabulate
        rows = [[v["relpath"], v["description"][:60],
                 ", ".join(v["args"]) or "-",
                 "yes" if v["confirm"] else "no"]
                for v in SCRIPTS.values()]
        print(tabulate(rows, headers=["script", "what it does", "args",
                                      "confirm?"], tablefmt="grid"))
    except Exception:
        for v in SCRIPTS.values():
            print(f"  {v['relpath']:<30} {v['description']}")
    return f"{len(SCRIPTS)} script(s) available."


# ---------------------------------------------------------------------------
# Asking the user something from inside a script
# ---------------------------------------------------------------------------
# A script runs as a SUBPROCESS, so a bare input() in it would just hit EOF --
# its stdin is not the chat. Instead it announces what it wants on stdout and
# then reads one line from stdin:
#
#   bash    echo "[ask] Which environment?";  read ENV
#   python  print("[secret] Password:", flush=True); pw = input()
#
# We watch stdout for those markers, relay the question to the chat (masked for
# [secret]), and write the answer back to the script's stdin. The prompt line
# itself is not echoed, so a credential prompt never lands in the transcript.
ASK_PREFIX = "[ask] "
SECRET_ASK_PREFIX = "[secret] "


def _maybe_prompt(line, proc):
    """If `line` is a prompt marker, ask the user and feed stdin. -> handled?"""
    secret = line.startswith(SECRET_ASK_PREFIX)
    if not secret and not line.startswith(ASK_PREFIX):
        return False
    question = line[len(SECRET_ASK_PREFIX if secret else ASK_PREFIX):].strip()
    try:
        # SECRET_ASK_PREFIX is also the marker app.py uses to mask the card, so
        # a secret question is passed through with its prefix intact.
        answer = input((SECRET_ASK_PREFIX if secret else "") +
                       (question or "Input required:"))
    except Exception as e:
        print(f"-> Could not get an answer ({e}); the script will see a blank.")
        answer = ""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write((answer or "") + "\n")
            proc.stdin.flush()
    except Exception:
        pass
    return True


def cancel_active():
    """Kill a running script (called by the chat's Stop button)."""
    with _LOCK:
        proc = _ACTIVE.get("proc")
    if proc is None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        return True
    except Exception:
        return False


def run(script_name, **kwargs):
    """Run one discovered script. Output streams to the chat live."""
    info = SCRIPTS.get(script_name)
    if info is None:
        rescan()
        info = SCRIPTS.get(script_name)
    if info is None:
        print(f"-> No script named {script_name!r}. "
              f"Available: {', '.join(SCRIPTS) or '(none)'}")
        return f"Unknown script {script_name!r}."

    path = info["file"]
    # Re-check confinement at run time, not just at discovery.
    real = os.path.realpath(path)
    if os.path.commonpath([real, os.path.realpath(SCRIPTS_DIR)]) \
            != os.path.realpath(SCRIPTS_DIR):
        print("-> Refused: that script is outside the scripts folder.")
        return "Refused: path escapes the scripts folder."
    if not os.path.exists(real):
        print(f"-> {info['relpath']} no longer exists.")
        return "Script not found."

    if info["confirm"]:
        answer = input(
            f"Run '{info['relpath']}'? This script is marked as needing "
            f"confirmation. (y/n): ").strip().lower()
        if answer not in ("y", "yes"):
            print("-> Cancelled; the script was not run.")
            return "Cancelled by the user."

    # argv, never a shell string: a value containing ; or && stays an argument.
    argv = list(RUNNERS[info["ext"]]) + [real]
    for key in (info["arg_order"] or list(kwargs)):
        if key in kwargs and kwargs[key] not in (None, ""):
            argv.append(str(kwargs[key]))

    shown = " ".join(shlex.quote(a) for a in argv[len(RUNNERS[info["ext"]]):])
    print(f"-> Running {info['relpath']} {shown.replace(real, '').strip()}".rstrip())
    print(f"   (timeout {info['timeout']}s)")
    print()

    started = time.time()
    try:
        proc = subprocess.Popen(
            argv, cwd=SCRIPTS_DIR, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, env=dict(os.environ, PYTHONUNBUFFERED="1"),
        )
    except Exception as e:
        print(f"-> Could not start it: {e}")
        return f"ERROR: could not start {info['relpath']}: {e}"

    with _LOCK:
        _ACTIVE["proc"] = proc

    timed_out = False

    def _killer():
        nonlocal timed_out
        timed_out = True
        try:
            proc.terminate()
        except Exception:
            pass

    timer = threading.Timer(info["timeout"], _killer)
    timer.daemon = True
    timer.start()
    try:
        for line in proc.stdout:            # stream live into the chat
            line = line.rstrip("\n")
            asked = _maybe_prompt(line, proc)
            if asked:
                continue                    # the prompt itself is not output
            print(line)
        proc.wait()
    finally:
        timer.cancel()
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        with _LOCK:
            _ACTIVE["proc"] = None

    took = time.time() - started
    print()
    if timed_out:
        print(f"-> Timed out after {info['timeout']}s and was stopped.")
        return (f"ERROR: {info['relpath']} timed out after "
                f"{info['timeout']}s.")
    if proc.returncode == 0:
        print(f"-> Finished in {took:.1f}s (exit 0).")
        return f"{info['relpath']} completed successfully in {took:.1f}s."
    print(f"-> Exited with code {proc.returncode} after {took:.1f}s.")
    return (f"ERROR: {info['relpath']} exited with code {proc.returncode}. "
            f"The output above shows why.")
