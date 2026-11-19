# Vivado MCP: overview

You drive ONE persistent Vivado session through Tcl. State (open project, variables,
loaded designs) survives between calls until `stop_session`.

## Tools
| tool | use |
|---|---|
| `start_session(show_window?)` | Attach to a running Vivado or launch one. `run_tcl_command` also does this on demand. |
| `run_tcl_command(command, wait=60)` | Run Tcl as a background job; waits up to `wait` s. |
| `job_status(job_id?, wait=0)` | Collect a job that was still `running`. |
| `console_tail(lines=100)` | Last lines of Vivado's transcript. Safe while a job runs. |
| `session_status()` | alive / busy / running_job / paths. |
| `stop_session()` | Stop Vivado. Kills it if a job is running (the only way to cancel). |
| `run_simulation`, `close_simulation` | Behavioral / post-synthesis simulation with PASS/FAIL parsing. |
| `list_project_artifacts`, `read_project_artifact` | Read files under a project's `.runs` / `.sim`. |
| `set_active_project`, `get_project_command_history`, `list_logged_projects` | Per-project command history. |
| `list_hw_devices()`, `program_device(bitstream?, device?, probes?)` | JTAG chain of the connected board; program the FPGA (default: newest `.bit` of `impl_1`). |
| `search_docs(query, top_k?, source_file?)` | Search reference PDFs (board user guides, datasheets) for pins, clocks, banks, jumpers. Returns chunks with file + page; cite them. |
| `list_docs()`, `read_doc_page(source_file, page)` | What documents exist; full text of one page around a hit. |

## Result fields (`run_tcl_command`, `job_status`)
- `status`: `ok`, `error`, `running` (still going), `busy` (another job holds Vivado).
- `result`: the Tcl return value.
- `console`: everything Vivado PRINTED during the command: `INFO:`, `WARNING:`,
  `CRITICAL WARNING:`, `ERROR:` and `puts` text. **Read this first when something fails.**
  Vivado's Tcl error is often only "'X' failed due to earlier errors".
- `error`: Tcl error text, null when ok.
- `job_id`: pass to `job_status`.
- `console_complete` / `console_truncated`: whether the whole slice arrived / was capped.
  The full transcript is always available with `console_tail`.

## Rules
1. Vivado runs one command at a time. During a job, new commands return `busy` at once.
2. A `running` result is NOT a failure. Vivado keeps working. Call `job_status(wait=60)`
   repeatedly, and glance at `console_tail` for progress.
3. A Vivado `ERROR` aborts the rest of that command (later lines do not run).
4. Return code 2 (`return`) counts as ok.
5. `stop_session` while a job runs kills Vivado and loses unsaved in-memory state.
   Save/close projects first when you can.
6. Console text contains only what Vivado printed. A command that prints nothing has an
   empty `console`; look at `result`.

## Board and part facts: search the documents, do not guess
Pin locations, I/O standards, clock frequencies and part numbers come from the board's
documentation. Before writing constraints (`set_property PACKAGE_PIN ...`, `IOSTANDARD`,
`create_clock -period ...`), call `search_docs` with a specific query (e.g.
"ZedBoard GCLK pin and frequency"), then `read_doc_page` on the hit to confirm, and cite
source and page. If `search_docs` returns an error (not configured, empty database,
embedding server down) say so instead of inventing values.
