"""
Work Assistant AI - chat server.

A local web app that lets a language model query your databases and run your
scripts, with the output streamed back into the chat.

    python app.py          then open http://127.0.0.1:8800

Everything configurable lives in .env (copy .env.example).
"""

import asyncio
import json
import os
import queue
import sys
import time
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Load .env from NEXT TO THIS FILE, explicitly. Bare load_dotenv() walks up from
# the caller's frame, which fails when there is no frame (python -c, some
# embedding hosts) and silently finds nothing when the app is started from
# another directory.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from core import bridge, db, llm, render, scripts, tools  # noqa: E402

APP_NAME = os.environ.get("APP_NAME", "Work Assistant AI").strip()

# Loopback by default. The websocket carries whatever you type -- including any
# credential a script asks for -- as plain ws://, not wss://. On 0.0.0.0 that
# crosses your network in the clear. Only change this if you have put TLS in
# front of it.
HOST = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("PORT", "8800"))

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
UPLOAD_DIR = os.path.join(HERE, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_OUTPUT_FOR_MODEL = int(os.environ.get("MAX_OUTPUT_FOR_MODEL", "14000"))
TOOL_BUDGET = int(os.environ.get("TOOL_BUDGET", "8"))
ANSWER_TIMEOUT = bridge.ANSWER_TIMEOUT

bridge.install()


# ---------------------------------------------------------------------------
# System prompt - built from what is actually configured
# ---------------------------------------------------------------------------
def build_system_prompt():
    lines = [
        f"You are {APP_NAME}, an assistant that runs real queries and scripts "
        f"on the user's own machine and databases.",
        "",
        "Rules:",
        "- When the user asks for something a tool can do, CALL THE TOOL. Do "
        "not describe what you would do -- do it.",
        "- If a tool needs an argument the user has not given, ask for it "
        "conversationally first.",
        "- A tool's console output, INCLUDING any table, is shown to the user "
        "directly above your message. Never repeat a table, and never "
        "enumerate its rows as a list. Write 1-3 plain sentences: totals, "
        "anything anomalous, and what they might do next.",
        "- Light markdown is fine (**bold**, bullets for genuinely separate "
        "points). Keep replies short; this is an operator console.",
        "- Some tools ask their own questions mid-run. Those are relayed to "
        "the user automatically -- you do not need to do anything.",
        "- Never ask for, echo, guess or repeat a password or passcode. "
        "Credential answers are redacted before they reach you, so '********' "
        "is expected and not an error.",
        "- If a tool reports it was stopped or cancelled, do NOT call it again "
        "on your own. Summarise what you got and ask how to proceed.",
        "- General knowledge and coding questions are fine too; answer them "
        "directly with proper code blocks.",
    ]

    conns = db.connection_names()
    if conns:
        lines += [
            "",
            f"Databases: {', '.join(conns)} (default '{db.DEFAULT_CONNECTION}').",
            ("Writes are ENABLED -- be careful and confirm destructive "
             "statements with the user first."
             if db.ALLOW_WRITES else
             "Databases are READ-ONLY; writes are rejected automatically."),
            "- Never guess a column name. Call list_tables / describe_table "
            "first if you are unsure.",
            "- Never silently drop a condition the user asked for. If it "
            "cannot be expressed, say so instead of answering a different "
            "question.",
            "- If the result is 0 rows, say so plainly and state which filters "
            "were applied. Never present a broadened query as the answer.",
            "- Schema probes are internal steps: never present their output as "
            "the result. Build your reply around ONE final query, run last.",
        ]
    else:
        lines += ["", "No database is configured yet, so SQL tools are "
                      "unavailable. Tell the user to fill in the DB_ block in "
                      ".env if they ask for data."]

    if scripts.SCRIPTS:
        lines += ["", f"Scripts available ({len(scripts.SCRIPTS)}):"]
        for v in list(scripts.SCRIPTS.values())[:40]:
            args = f" (args: {', '.join(v['args'])})" if v["args"] else ""
            lines.append(f"  - {v['tool_name']}: {v['description']}{args}")
    else:
        lines += ["", "No scripts are present in the scripts folder yet."]

    extra = os.environ.get("SYSTEM_PROMPT_EXTRA", "").strip()
    if extra:
        lines += ["", extra]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def _is_cancel(msg):
    """Accept every spelling of Stop. A Stop the server does not recognise is
    indistinguishable from one that was never pressed."""
    if not isinstance(msg, dict):
        return False
    return str(msg.get("type", "")).strip().lower() in (
        "cancel", "stop", "abort", "interrupt")


LIVE_QUEUES = set()
WS_STATE = {"connections": 0, "messages_in": 0, "last_message": None,
            "last_message_at": None, "answers_delivered": 0}


def stop_everything():
    """Abort everything running. Safe from an HTTP request too."""
    try:
        tools.cancel_all()
    except Exception:
        pass
    stopped = bridge.abandon_all()
    for q in list(LIVE_QUEUES):
        try:
            q.put_nowait({"type": "cancel"})
        except Exception:
            pass
    return stopped


# ---------------------------------------------------------------------------
# Tool execution over the websocket
# ---------------------------------------------------------------------------
async def run_tool(name, args, ws, user_q):
    """Execute a tool, streaming output and questions to the browser.

    Returns (output_for_model, result, error_or_None).
    """
    spec = tools.TOOLS.get(name)
    if spec is None:
        return "", None, f"Unknown tool: {name}"

    await ws.send_json({"type": "tool_start", "name": name})
    if name == "run_sql_query" and (args or {}).get("query"):
        await ws.send_json({"type": "tool_sql", "sql": str(args["query"]),
                            "database": str((args or {}).get("connection")
                                            or db.DEFAULT_CONNECTION)})

    ex = bridge.ToolExecution(
        spec["fn"], tools.normalize_args(spec["fn"], dict(args or {})),
        name=name)
    ex.start()

    raw_parts, line_buf, table_block, in_table = [], "", [], False

    async def flush_table():
        nonlocal in_table, table_block
        if not in_table:
            return
        parsed = render.parse_grid_table(table_block)
        if parsed:
            await ws.send_json({"type": "tool_table",
                                "headers": parsed["headers"],
                                "rows": parsed["rows"]})
        table_block, in_table = [], False

    async def process_line(line):
        nonlocal in_table
        if line.startswith(db.SQL_TABLE_SENTINEL):
            await flush_table()
            try:
                data = json.loads(line[len(db.SQL_TABLE_SENTINEL):])
                headers, rows = data.get("headers", []), data.get("rows", [])
                await ws.send_json({"type": "tool_table",
                                    "headers": headers, "rows": rows})
                raw_parts.append(render.table_note(headers, rows))
            except Exception:
                await ws.send_json({"type": "tool_output", "text": line + "\n"})
                raw_parts.append(line)
            return
        await ws.send_json({"type": "tool_output", "text": line + "\n"})
        raw_parts.append(line)
        if render.BORDER_RE.match(line) or render.ROW_RE.match(line):
            in_table = True
            table_block.append(line)
        else:
            await flush_table()

    async def feed(chunk):
        nonlocal line_buf
        line_buf += chunk
        while "\n" in line_buf:
            line, line_buf = line_buf.split("\n", 1)
            await process_line(line)

    async def flush_all():
        nonlocal line_buf
        if line_buf:
            await process_line(line_buf)
            line_buf = ""
        await flush_table()

    async def check_cancel():
        try:
            msg = user_q.get_nowait()
        except asyncio.QueueEmpty:
            return False
        if _is_cancel(msg):
            return True
        await user_q.put(msg)
        return False

    async def do_cancel():
        try:
            tools.cancel_all()
        except Exception:
            pass
        ex.abandon()
        await flush_all()
        note = "[Stopped by user]"
        raw_parts.append(note)
        await ws.send_json({"type": "tool_output", "text": note + "\n"})
        await ws.send_json({"type": "tool_end", "name": name, "ok": False})
        return (render.prepare_for_model("\n".join(raw_parts)), None,
                "Cancelled by the user. Do not retry. Summarise what you have "
                "and ask how to proceed.")

    while True:
        if await check_cancel():
            return await do_cancel()
        try:
            kind, payload = ex.out_q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue

        if kind == "stdout":
            await feed(payload)

        elif kind == "question":
            await flush_all()
            # A prompt marked [secret] asks for a credential. Strip the marker
            # for display and record the ANSWER as **** so it never reaches
            # the model.
            secret = payload.startswith(bridge.SECRET_PREFIX)
            shown = payload[len(bridge.SECRET_PREFIX):] if secret else payload
            raw_parts.append(shown)
            await ws.send_json({"type": "tool_question", "text": shown,
                                "secret": secret})
            try:
                msg = await asyncio.wait_for(user_q.get(),
                                             timeout=ANSWER_TIMEOUT)
            except asyncio.TimeoutError:
                ex.abandon()
                note = (f"[No answer within {ANSWER_TIMEOUT:.0f}s - giving up. "
                        f"Nothing was run.]")
                raw_parts.append(note)
                await ws.send_json({"type": "tool_output", "text": note + "\n"})
                await ws.send_json({"type": "tool_end", "name": name,
                                    "ok": False})
                return (render.prepare_for_model("\n".join(raw_parts)), None,
                        "The user never answered the tool's prompt. Tell them "
                        "it timed out and ask whether to retry.")
            if _is_cancel(msg):
                return await do_cancel()
            answer = (msg.get("text") or "").strip()
            raw_parts.append("********" if secret else answer)
            WS_STATE["answers_delivered"] += 1
            ex.answer_q.put(answer)
            await ws.send_json({"type": "answer_ack", "secret": secret,
                                "chars": len(answer)})

        elif kind == "done":
            await flush_all()
            text = str(payload or "")
            ok = not text.startswith(("ERROR:", "STOPPED"))
            await ws.send_json({"type": "tool_end", "name": name, "ok": ok})
            return (render.prepare_for_model("\n".join(raw_parts)), payload,
                    None if ok else text)

        elif kind == "error":
            await flush_all()
            raw_parts.append(payload)
            await ws.send_json({"type": "tool_output", "text": payload})
            await ws.send_json({"type": "tool_end", "name": name, "ok": False})
            return (render.prepare_for_model("\n".join(raw_parts)), None,
                    payload)


def _truncate_for_model(text):
    if len(text) <= MAX_OUTPUT_FOR_MODEL:
        return text
    head, tail = text[:2000], text[-(MAX_OUTPUT_FOR_MODEL - 2000):]
    return (head + "\n...[output truncated for the model; the user saw all of "
                   "it]...\n" + tail)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/config")
async def config():
    primary = llm.BACKENDS[0] if llm.BACKENDS else None
    return JSONResponse({
        "app_name": APP_NAME,
        "provider": llm.PROVIDER,
        "model": primary.model if primary else None,
        "backend": primary.label if primary else None,
        "fallback": llm.BACKENDS[1].model if len(llm.BACKENDS) > 1 else None,
        "databases": db.connection_names(),
        "scripts": len(scripts.SCRIPTS),
    })


@app.get("/api/debug")
async def api_debug():
    """Live backend state: what is running and what it is waiting for.

    Contains no credentials -- prompts are shown, answers never are.
    If a tool looks stuck, compare ws.answers_delivered against
    executions[-1].pending_question to tell a browser problem from a backend one.
    """
    return JSONResponse({
        "now": time.time(),
        "app": {"name": APP_NAME, "pid": os.getpid(), "cwd": os.getcwd(),
                "host": HOST, "port": PORT,
                "answer_timeout_s": ANSWER_TIMEOUT},
        "llm": llm.describe(),
        "db": db.describe(),
        "scripts": scripts.describe(),
        "ws": dict(WS_STATE),
        "executions": [e.snapshot() for e in bridge.EXECUTIONS],
        "live_executions": len(bridge.live_executions()),
    })


@app.post("/api/stop")
@app.get("/api/stop")
async def api_stop():
    """Abort everything, without needing the websocket.

    The Stop button goes over the websocket; if that path breaks the button
    looks dead. This does not depend on it:
        curl -X POST http://127.0.0.1:8800/api/stop
    """
    stopped = stop_everything()
    return JSONResponse({"stopped": stopped, "count": len(stopped)})


@app.post("/api/rescan")
@app.get("/api/rescan")
async def api_rescan():
    """Pick up scripts added since start-up, without a restart."""
    tools.refresh()
    return JSONResponse(scripts.describe())


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    safe = f"{uuid.uuid4().hex[:8]}_{os.path.basename(file.filename)}"
    dest = os.path.join(UPLOAD_DIR, safe)
    with open(dest, "wb") as f:
        f.write(await file.read())
    return {"path": dest, "name": file.filename}


# ---------------------------------------------------------------------------
# Websocket chat
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    if not llm.BACKENDS:
        await ws.send_json({"type": "error", "text":
            "No LLM backend configured. Copy .env.example to .env and fill in "
            "either the CLOUD_* block (and set LLM_PROVIDER=cloud) or the "
            "LOCAL_* block (LLM_PROVIDER=local)."})
        await ws.close()
        return

    messages = [{"role": "system", "content": build_system_prompt()}]
    user_q = asyncio.Queue()
    LIVE_QUEUES.add(user_q)
    WS_STATE["connections"] += 1

    async def receiver():
        while True:
            data = await ws.receive_json()
            WS_STATE["messages_in"] += 1
            # type and size only -- never the text, which may be a credential
            WS_STATE["last_message"] = {
                "type": str(data.get("type", "?")),
                "chars": len(data.get("text") or "")}
            WS_STATE["last_message_at"] = time.time()
            await user_q.put(data)

    recv_task = asyncio.create_task(receiver())

    async def notify(text):
        await ws.send_json({"type": "tool_output", "text": text + "\n"})

    try:
        while True:
            msg = await user_q.get()

            if _is_cancel(msg):
                killed = stop_everything()
                if killed:
                    await notify(f"[Stopped {len(killed)} leftover job(s)]")
                continue

            if msg.get("type") == "answer":
                # An answer that arrived with no tool waiting for it. DROP it.
                # It must never become a chat message: these carry credentials.
                continue

            text = (msg.get("text") or "").strip()
            if not text:
                continue

            messages.append({"role": "user", "content": text})
            tools_used = 0

            while True:
                await ws.send_json({"type": "thinking"})
                try:
                    resp = await llm.call(messages, tools.OPENAI_TOOLS,
                                          on_failover=notify)
                except llm.AllBackendsFailed as e:
                    note = (f"The AI model didn't respond.\n\n{e}\n\nNothing "
                            f"was run. Please wait a few minutes and try "
                            f"again.")
                    await ws.send_json({"type": "error", "text": note})
                    # Also send a normal assistant turn: the spinner clears on
                    # this in every code path, so a backend failure can never
                    # leave the UI stuck on "thinking".
                    await ws.send_json({"type": "assistant", "text": note})
                    if messages and messages[-1].get("role") == "user":
                        messages.pop()
                    break
                except Exception as e:
                    note = f"LLM backend error: {e}\n\nNothing was run."
                    await ws.send_json({"type": "error", "text": note})
                    await ws.send_json({"type": "assistant", "text": note})
                    break

                choice = resp.choices[0].message
                tool_calls = choice.tool_calls or []

                if not tool_calls:
                    reply = render.strip_markdown_tables(
                        render.strip_think(choice.content or "")) or "(no response)"
                    messages.append({"role": "assistant", "content": reply})
                    await ws.send_json({"type": "assistant", "text": reply})
                    break

                messages.append({
                    "role": "assistant",
                    "content": render.strip_think(choice.content) or None,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name,
                                      "arguments": tc.function.arguments}}
                        for tc in tool_calls],
                })

                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    tools_used += 1
                    if tools_used > TOOL_BUDGET:
                        output, result, error = "", None, (
                            f"TOOL BUDGET EXCEEDED ({TOOL_BUDGET} calls this "
                            "message). Do not call more tools. Summarise what "
                            "you found and what failed, and ask how to proceed.")
                    else:
                        output, result, error = await run_tool(
                            tc.function.name, args, ws, user_q)
                    payload = {"output": _truncate_for_model(output),
                               "result": str(result) if result is not None else ""}
                    if error:
                        payload["error"] = error[-3000:]
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps(payload)})

    except WebSocketDisconnect:
        pass
    finally:
        WS_STATE["connections"] = max(0, WS_STATE["connections"] - 1)
        LIVE_QUEUES.discard(user_q)
        recv_task.cancel()
        # A dropped socket must not leave a tool parked on an answer forever.
        for e in bridge.EXECUTIONS:
            if e.alive and e.pending_question:
                e.abandon()


def _banner():
    primary = llm.BACKENDS[0] if llm.BACKENDS else None
    print(f"\n  {APP_NAME}")
    print(f"  -> http://{HOST}:{PORT}")
    if primary:
        extra = (f"  (fallback: {llm.BACKENDS[1].model})"
                 if len(llm.BACKENDS) > 1 else "  (no fallback configured)")
        print(f"  provider: {llm.PROVIDER}  |  model: {primary.model}{extra}")
    else:
        print("  WARNING: no LLM backend configured -- see .env.example")
    conns = db.connection_names()
    print(f"  databases: {', '.join(conns) if conns else 'none configured'}"
          f"{'' if db.ALLOW_WRITES else '  (read-only)'}")
    print(f"  scripts:   {len(scripts.SCRIPTS)} found in {scripts.SCRIPTS_DIR}")
    print()


if __name__ == "__main__":
    import uvicorn
    _banner()
    if os.environ.get("OPEN_BROWSER", "true").strip().lower() in ("1", "true", "yes"):
        try:
            import webbrowser
            webbrowser.open(f"http://{'127.0.0.1' if HOST in ('0.0.0.0','') else HOST}:{PORT}")
        except Exception:
            pass
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
