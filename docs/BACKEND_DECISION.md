# Backend decision: how the agent talks to Vivado

Status: **decided and implemented** (native TCP backend; wrappers retired to `legacy/`) · Date: 2026-11-15 · Verified on Vivado 2025.2

## The three things being compared

| Label | What it is | Code |
|---|---|---|
| **A. Native TCP** | Vivado itself hosts a socket by sourcing `vivado_socket_server.tcl`. The MCP server opens one short connection per command and gets one JSON line back. | `vivado_native_tcp_session.py`, `vivado_socket_server.tcl` |
| **B. Pipe wrapper** | The MCP server owns Vivado's stdin/stdout and decides a command is "done" when it sees the `Vivado%` prompt. | `vivado_session.py` |
| **B+. TCP wrapper** | B re-exposed on a TCP port, inside the MCP server process. | `vivado_tcp_server.py` |

## What the agent needs back from each action

1. Did it succeed or fail? 2. What value did it return? 3. What did Vivado print while doing it (INFO / WARNING / ERROR)? 4. Is the session still alive and in sync?

| | A. Native TCP | B. Pipe wrapper | B+. TCP wrapper |
|---|---|---|---|
| Success vs failure | Yes: `catch` return code gives `status: ok/error` plus the message | No: text only; the agent must grep for `ERROR:` | No (same as B) |
| Return value vs console text | Separate `result` and `stdout` fields, but `stdout` is always empty on 2025.2 (no `redirect`) | One merged blob | One merged blob |
| Knowing a command finished | Server writes a response line | Scan for the string `Vivado%`; output containing it, or a prompt that never returns, desyncs or times out | Same as B |
| Console messages / progress | Currently **lost** (drain thread discards it; `redirect` missing), fixable via `-log` capture | Captured, but only returned at the end | Same as B |
| Survives MCP restart | Vivado can, but there is **no attach logic** (a new MCP launches a second Vivado and hits "port in use") | No | **No**: the TCP server is a thread inside the MCP process, so the "session survival" claim in its docstring is wrong |
| Cancel a running command | No | No | No |
| Code size | ~540 py + 213 tcl | 369 py | +282 py |

## Verified by running the real `vivado_socket_server.tcl` under `tclsh` (stand-in for Vivado; `redirect` absent, so the fallback path was exercised)

- Works: success, Tcl errors (`status: error` with message), unknown commands, Windows paths with backslashes, quotes, unicode, multi-line scripts, list results.
- **Breaks:** any output containing a control character (ESC, backspace, ...) produces invalid JSON. The Python side then reports "Invalid JSON from server" and the real output is lost. `vss_json_escape` only handles `\ " \n \t \r`.
- **Orphaned commands:** a client timeout does not stop the command. The next command queued behind it (measured: a 3 s command timed out at 1 s; the following 1+1 took 2 s more).
- **Security:** an HTTP POST whose *body line* is `{"command": "..."}` was executed. The server processes any line that parses, so a web page could in principle fire `exec`-capable Tcl at `127.0.0.1:9877` (and B+ has the same issue on 9876). Needs a per-session secret token.

## Recommendation

**Use A (native TCP) as the single primary backend**, with three changes:

1. **Capture the console via `-log`.** `redirect` does not exist in 2025.2 (see "Resolved" below). Launch with `-log <session>.log`, return the new log lines (`console`) with each command, and expose a tail tool for long runs. Drop the dead `redirect` code path.
2. **Attach on start.** Probe `host:port` first; if a Vivado server answers, adopt it instead of launching another.
3. **Harden the protocol.** Escape all control characters (`\u00XX`) in `vss_json_escape`, unescape `\r`, `\uXXXX`, `\/`; require a random token (passed via env var) in every request; reject lines without it.

**Retire B+.** It adds a hop (agent → MCP → TCP → Python → pipe → Vivado), inherits B's weaknesses, and does not deliver the survival it advertises.

**B is optional.** Keep it only as a fallback if `-source` startup ever fails on a machine. Otherwise delete it. Either way, collapse the tool surface the agent sees to one set (`start_session`, `run_tcl`, `session_status`, `stop_session`) so the LLM is not choosing between `run_tcl_command` and `run_native_tcl_command`.

Operating rule for A: Vivado's Tcl event loop is single-threaded, so while a command runs nothing else is answered. Launch long jobs asynchronously (`launch_runs` without `-wait`) and poll `get_property STATUS [get_runs synth_1]` with short commands; bound simulations (`run 1 ms`) rather than `run all` where possible.

## Resolved: capturing console output on Vivado 2025.2 (tested live, 2026-11-15)

- `redirect` and `tee` **do not exist** in 2025.2, so the server's `redirect -variable` path never runs. `stdout` is always empty and anything printed is lost; only the Tcl result comes back.
- **Real error text is console-only.** `send_msg_id {Test 1-4} ERROR ...` printed `ERROR: [Test 1-4] ...` in the window, but the Tcl error the agent received was just `ERROR: [Common 17-39] 'send_msg_id' failed due to earlier errors.` Without console capture the agent learns *that* a command failed, not *why*. The command also aborts at the first ERROR (later `puts` did not run).
- A Tcl channel transform (`chan push stdout/stderr`) captures `puts` output but **not** Vivado's own `INFO/WARNING/CRITICAL WARNING/ERROR` messages; they bypass Tcl's channels. Not sufficient.
- **`-log <file>` works.** A second Vivado (batch mode) wrote the command echo, `INFO:`/`WARNING:` messages and `puts` text to the log **live**, while it was still inside `after 25000`. This is the mechanism to use.
- `create_msg_db` exists but writes a binary database; not practical for agent feedback.

**Design:** launch with `-log <session>.log` instead of `-nolog`. Before each command record the log's byte offset; after it returns, read from that offset (allow a short settle until the size stops growing) and return the slice as `console`. Works with and without a visible window. Still to verify after the change: the same behaviour in `-mode tcl` with the `-source`/`vwait` startup (the test above used `-mode batch`).

## Implemented (2026-11-15)

- **Console capture via `-log`**: Vivado launches with `-log <session>.log`; the Tcl server prints `@@VSS-BEGIN/END <id>@@` around each command and `console_log.py` slices the transcript between them. The END marker also proves the log is flushed. Each result now carries `console`, `console_complete`, `console_truncated`. Dead `redirect` path removed.
- **Attach on start**: `~/.vivado_mcp/logs/native_session.json` (host, port, token, log path); `start_session` re-attaches to a running Vivado, removes stale state, and gives a clear error if something else holds the port.
- **Protocol hardening**: per-session random token on every request (also defeats the HTTP-body attack); lines that are not JSON objects drop the connection silently; real JSON parser/encoder (all escapes incl. `\uXXXX` + surrogate pairs; control characters escaped); top-level `return` (code 2) is ok.
- **Bug fixed**: the Tcl server closed the connection when a large command arrived in several chunks (`gets` returning -1 on a *partial* line was treated as EOF). A 1.5 MB command now works.
- **Timeouts**: return `status: timeout` with the partial console so far, instead of raising and losing it; `console_tail` works while a command runs (tool calls run in threads).
- **Cancel**: `stop_session` during a running command kills the whole process tree (`taskkill /T` on Windows; process group elsewhere). `Popen.kill()` alone would have left `vivado.exe` running behind `vivado.bat`.
- **One tool surface**: `start_session`, `run_tcl_command`, `console_tail`, `session_status`, `stop_session` (+ simulation, artifact and logging tools). `*_native_*` and `*_tcp_server` tools removed.
- **Retired** to `legacy/`: `vivado_session.py` (pipe) and `vivado_tcp_server.py` (TCP wrapper).

Verification: 29 tests pass, including end-to-end tests running the real Tcl server under `tclsh` (auth, HTTP-style request, attach, stale state, port conflict, timeout, stop-while-running, 1.5 MB command). On **real Vivado 2025.2** (second instance with the new script, `-mode tcl -log`): auth enforced; `INFO`/`WARNING`/`puts` lines appear in the transcript in order between the markers; an `ERROR` message that previously reached the agent only as "failed due to earlier errors" is now captured verbatim; ESC characters and `return` handled. Not yet exercised: the Python client against real Vivado (needs an MCP server restart).

## Known issues found while reviewing

- ~~`run_tcl_command` called a non-existent `VivadoSession.run_detailed()`~~: resolved; the pipe backend is retired.
- ~~README listed `run_project_simulation`~~: README rewritten to match the code.
- `pyproject.toml` URLs are still `your-org` placeholders.
- ~~A top-level Tcl `return` was reported as an error~~: fixed.
- `tests/test_simulation_tools.py::test_artifact_reads_reject_escape` uses a Windows-style `..\outside.log`; it passes on Windows but fails on Linux (backslash is not a separator there).
- Two Vivado crash dumps (`hs_err_pid12400/27776`, `EXCEPTION_BREAKPOINT`, ~25 min apart) were in the repo root; moved to `_debug/crash_dumps/`. A Vivado crash kills the in-Vivado socket server, so the client should detect "connection refused" and offer a restart.

## Resulting layout

```
src/vivado_mcp/
  server.py                    # tool registration (one backend)
  vivado_native_tcp_session.py # session: launch / attach / run / stop
  vivado_socket_server.tcl     # server that runs inside Vivado
  console_log.py               # transcript slicing between markers
  simulation.py  project_artifacts.py  session_logger.py
legacy/                        # retired pipe + TCP-wrapper modules (reference only)
docs/BACKEND_DECISION.md
tests/                         # incl. tclsh-backed end-to-end tests
```

## Git state at review time

`main` is 1 commit ahead of `origin/main` (`c86d3b2 Changed the tool calling structure`). Nothing from the reorganisation or the implementation has been committed. The `refs/agents/...` checkpoint refs come from the desktop app and are harmless.

Suggested commits (on a new branch, e.g. `native-backend`):
1. `.gitignore` + `docs/BACKEND_DECISION.md`
2. `session_logger.py` + `tests/test_session.py` (logging, per-project history)
3. `vivado_socket_server.tcl`, `console_log.py`, `vivado_native_tcp_session.py` + `tests/test_console_log.py`, `tests/test_native_protocol.py` (native backend)
4. `simulation.py`, `project_artifacts.py` + `tests/test_simulation_tools.py`
5. `server.py` (tool surface), `legacy/` (moved modules), `README.md`, `pyproject.toml`

The `legacy/` moves show up in git as `vivado_session.py` deleted plus untracked `legacy/`; `git add -A` records them correctly.
