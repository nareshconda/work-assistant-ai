# Work Assistant AI

A local chat app that lets a language model **query your databases** and **run
your scripts**, and streams the output back into the conversation.

Point it at PostgreSQL or Azure Synapse, drop your shell and Python scripts in a
folder, and ask for things in plain language. Everything runs on your machine.

```
you    which region brings in the most revenue?
       -> runs SELECT ... GROUP BY region, shows the table, adds one line of insight

you    run the disk usage script on /var/log
       -> runs scripts/disk_usage.sh, streams its output live
```

- **Bring your own model.** Any OpenAI-compatible endpoint: OpenAI, Azure,
  NVIDIA, Groq, Together, OpenRouter — or your own Ollama / LM Studio / vLLM
  box. One variable switches between them.
- **Scripts are the config.** Drop a `.sh` or `.py` in `scripts/` and it becomes
  a tool. No registration, no restart.
- **Read-only by default.** Anything that modifies data is rejected before it
  reaches the server.
- **No telemetry, no cloud dependency** beyond the model endpoint you choose.

---

## Quick start

```bash
git clone https://github.com/nareshconda/work-assistant-ai.git
cd work-assistant-ai

python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env     # then edit it - see below
python app.py            # opens http://127.0.0.1:8800
```

Requires Python 3.10 or newer.

### Minimum edit to `.env`

Pick a model:

```bash
LLM_PROVIDER=cloud
CLOUD_BASE_URL=https://api.openai.com/v1
CLOUD_API_KEY=sk-...
CLOUD_MODEL=gpt-4o-mini
```

…or a local one:

```bash
LLM_PROVIDER=local
LOCAL_BASE_URL=http://localhost:11434/v1
LOCAL_MODEL=qwen2.5:14b
```

And a database, if you want SQL:

```bash
DB_KIND=postgres
DB_HOST=db.example.com
DB_NAME=analytics
DB_USER=readonly_user
DB_PASSWORD=...
```

That's it. Leave the database blank and you still get scripts; leave `scripts/`
empty and you still get SQL.

---

## The single variable swap

`LLM_PROVIDER` is the only line you change to move between a hosted model and
your own hardware:

| value | primary | fallback |
|---|---|---|
| `cloud` | `CLOUD_*` | `LOCAL_*` |
| `local` | `LOCAL_*` | `CLOUD_*` |

Both blocks live in `.env` side by side. **Fill in both and you get automatic
failover** — if the primary times out or errors, the other answers, and the chat
tells you it switched. Fill in one and you simply have no fallback.

This matters more than it sounds. A local box that is asleep, or a hosted API
having a bad afternoon, otherwise leaves the UI spinning forever. Three timeout
layers prevent that:

| variable | default | what it does |
|---|---|---|
| `LLM_REQUEST_TIMEOUT` | 60s | HTTP read timeout |
| `LLM_HARD_TIMEOUT` | 75s | absolute cap, enforced by the event loop |
| `LLM_MAX_RETRIES` | 0 | SDK retries — leave at 0 |
| `LLM_COOLDOWN` | 120s | after *all* backends fail, fail fast for this long |

`LLM_HARD_TIMEOUT` is the one that saves you: a wedged HTTP call in a worker
thread cannot be cancelled, so without an outer cap the `await` never returns
and no error is ever raised. `LLM_MAX_RETRIES` defaults to 0 because the OpenAI
SDK retries twice by default, quietly turning a 60s timeout into three minutes
of silence.

> **Local models must support tool calling**, or the assistant will chat happily
> and never actually run anything. Known good: `qwen2.5`, `qwen3`, `llama3.1` /
> `llama3.3`, `mistral-nemo`, `firefunction-v2`.

---

## Databases

Set `DB_KIND` to `postgres` (PostgreSQL, Redshift, Timescale…) or `synapse`
(Azure Synapse, Azure SQL, SQL Server).

Synapse additionally needs a system ODBC driver:

```bash
# macOS
brew install unixodbc msodbcsql18
# Debian/Ubuntu
apt-get install unixodbc msodbcsql18
# Windows: install "ODBC Driver 18 for SQL Server" from Microsoft
```

**For more than one database**, copy `connections.example.json` to
`connections.json` (gitignored) and list them there. The assistant picks by name
and falls back to `DB_DEFAULT`.

### Read-only

`DB_ALLOW_WRITES=false` is the default. Queries are checked before execution:
only a single `SELECT` or `WITH … SELECT` is permitted, multiple statements are
refused, and PostgreSQL connections also set
`default_transaction_read_only`.

**Use a read-only database user anyway.** The check is a seatbelt, not a
sandbox — the right place to enforce this is your database's own permissions.

The assistant also gets `list_tables` and `describe_table`, so it looks up a
schema rather than guessing column names.

---

## Scripts

Anything in `scripts/` becomes a tool. Header comments tell the model when to
use it:

```bash
#!/usr/bin/env bash
# @name: disk_usage
# @description: Show disk usage for a path, largest directories first.
# @arg path: Directory to measure. Defaults to the home directory.
# @confirm: false
# @timeout: 60

du -h -d 1 "${1:-$HOME}" | sort -hr | head -15
```

Output streams into the chat as it is produced. Print a `tabulate` grid table
and it renders as a real table with a CSV download. Exit non-zero and the
failure is reported with the output.

See [`scripts/README.md`](scripts/README.md) for the full header format and
`scripts/examples/` for working samples.

### Asking the user something mid-run

A script runs as a subprocess, so a bare `input()` would hit EOF. Announce the
question on stdout and read one line back:

```bash
echo "[ask] Which environment?";       read ENVIRONMENT
echo "[secret] API key:";              read API_KEY
```

```python
print("[secret] Password:", flush=True)
password = sys.stdin.readline().strip()
```

`[secret]` shows a masked credential card. That answer is **never written to
disk, never echoed into the transcript, and replaced with `********` before
anything is sent to the model.**

### Safety

- Only files inside `scripts/` run. Path traversal and symlinks pointing
  outside are refused, at discovery *and* at run time.
- Arguments are passed as argv, never interpolated into a shell string — a value
  containing `;` or `&&` is an argument, not a second command.
- Every run has a timeout (`@timeout`, or `SCRIPT_TIMEOUT`).
- `@confirm: true` makes the assistant ask you first. **Put it on anything
  destructive.**

> Scripts run with your user's permissions, triggered by a language model.
> Only put things in `scripts/` you are comfortable with that. Treat it like
> giving someone shell access on your behalf.

By default `.gitignore` ignores everything in `scripts/` except the README and
examples, so your own runbooks can't be published by accident.

---

## Endpoints

| route | what it gives you |
|---|---|
| `/` | the chat UI |
| `/config` | which model and databases are live |
| `/api/debug` | what is running, what it is waiting for, backend health |
| `/api/stop` | abort everything — works without the websocket |
| `/api/rescan` | pick up newly added scripts without a restart |

`/api/debug` is the one to open when something looks stuck. It shows whether a
tool is waiting on an answer that never arrived (`pending_question` set,
`ws.answers_delivered` not incrementing — a browser problem) or sitting in a
backend call. It contains no credentials: prompts are shown, answers never are.

---

## Security

This is a tool that runs SQL and shell commands on your machine at a language
model's request. Be deliberate about it.

- **It binds to `127.0.0.1` by default. Keep it there.** The websocket is plain
  `ws://`, not `wss://` — on `0.0.0.0`, everything you type, including a
  password a script asks for, crosses your network in the clear. If you must
  expose it, put TLS and authentication in front of it.
- **There is no authentication.** Anyone who can reach the port can run your
  scripts and query your databases.
- **Use a read-only database user.** `DB_ALLOW_WRITES=false` is a seatbelt, not
  a sandbox.
- **`.env` and `connections.json` are gitignored.** Keep them that way. If you
  have ever committed a key, rotate it — removing it from a later commit does
  not remove it from history.
- Credentials typed into the chat are held in memory only. Note that Python
  strings are immutable and cannot be wiped, so they persist in the heap until
  garbage collected; prefer short-lived tokens where you can.

---

## Troubleshooting

**The model chats but never runs anything.** It doesn't support tool calling, or
not through your endpoint. Try `qwen2.5` or a hosted model.

**"No LLM backend configured."** Neither block in `.env` is complete. Each needs
a base URL, a key and a model. Check `/config`.

**SQL tools aren't offered.** `DB_HOST` is empty. Check `/config` → `databases`.

**A script doesn't show up.** It needs a `.sh` or `.py` extension and must be
inside `scripts/`. Hit `/api/rescan`, or check `/api/debug` → `scripts`.

**Something is stuck.** `curl -X POST http://127.0.0.1:8800/api/stop`, then look
at `/api/debug`.

**Synapse connection fails.** Usually the ODBC driver. Verify with
`python -c "import pyodbc; print(pyodbc.drivers())"` — if the list is empty or
lacks "ODBC Driver 18 for SQL Server", install it.

---

## How it fits together

```
app.py              web server, websocket chat loop, tool orchestration
core/llm.py         backend selection, failover, timeouts
core/db.py          postgres + synapse, read-only enforcement
core/scripts.py     discovery, metadata parsing, sandboxed execution
core/tools.py       builds the function declarations the model sees
core/bridge.py      runs tools on threads, captures output, relays prompts
core/render.py      console output -> chat tables and model summaries
static/index.html   the UI (single file, no build step)
```

`core/bridge.py` is the non-obvious one. `stdout` and `input()` are patched
**once** and routed **per thread**, rather than per execution. The naive
approach breaks the moment one tool is stopped while its thread keeps running: a
later execution overwrites the globals, the two share one hook, and an answer
can be delivered to the wrong thread.

## Contributing

Issues and pull requests welcome. There is no build step — edit, run `python
app.py`, reload.

Useful additions: more database backends (MySQL, DuckDB, Snowflake), an auth
layer, conversation history, scheduled runs.

## License

MIT — see [LICENSE](LICENSE).
