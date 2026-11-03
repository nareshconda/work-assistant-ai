# scripts/

Drop a `.sh` or `.py` file in here and the assistant can run it. No
registration, no restart — the folder *is* the configuration.

## Making a script the assistant understands

Header comments become the description the model reads when deciding what to
call. All of them are optional, but a good `@description` is the difference
between the assistant picking your script and ignoring it.

```bash
#!/usr/bin/env bash
# @name: disk_usage
# @description: Show disk usage for a path, largest directories first.
# @arg path: Directory to measure. Defaults to the current directory.
# @confirm: false
# @timeout: 60
```

| key | meaning |
|---|---|
| `@name` | Tool name. Defaults to the filename. |
| `@description` | What it does and when to use it. **Write this one.** |
| `@arg <name>` | One per argument, passed positionally in declaration order. |
| `@confirm` | `true` makes the assistant ask you before running it. |
| `@timeout` | Seconds before it is killed. Defaults to `SCRIPT_TIMEOUT`. |

## How your script is run

* Working directory is this folder.
* Arguments arrive as `$1`, `$2`… (bash) or `sys.argv[1:]` (python), in the
  order the `@arg` lines appear.
* Everything printed to stdout/stderr streams into the chat live.
* Exit `0` is success; anything else is reported as a failure with the output.
* Print a `tabulate` grid table and the chat renders it as a real table with a
  CSV download button.

## Safety

* Only files inside this folder run. Path traversal and symlinks pointing
  outside are refused.
* Arguments are passed as argv, never interpolated into a shell string — a
  value containing `;` or `&&` is an argument, not a second command.
* **Put `@confirm: true` on anything destructive.** The assistant will ask
  before running it.
* These scripts run with your user's permissions. Only put things in here you
  are happy for a language model to trigger.

## Version control

By default `.gitignore` ignores everything here except `README.md` and
`examples/`, so your own runbooks can't be published by accident. Remove those
lines from `.gitignore` if you *want* to version your scripts.
