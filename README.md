# Vivado MCP Server (Windows-Native)

Vivado MCP Server is a Model Context Protocol (MCP) server that lets LLM clients
control a persistent AMD/Xilinx Vivado Tcl session on Windows and get back
**what actually happened**: the Tcl result, whether it failed, and everything
Vivado printed (INFO / WARNING / ERROR messages included).

## How it works

Vivado itself hosts a small Tcl socket server (`vivado_socket_server.tcl`). The MCP
server sends one JSON request per command over loopback and Vivado answers directly
from its own Tcl interpreter, so there is no prompt scraping.

- **Reliable success/failure**: Tcl return codes become `status: ok | error`.
- **Console capture**: Vivado is launched with `-log`; each command's slice of that
  transcript is returned as `console` (Vivado 2025.2 has no `redirect`, and its
  messages bypass Tcl's channels, so the log is the only place they can be read).
- **Survives MCP restarts**: Vivado keeps running; `start_session` re-attaches to it
  instead of launching a second one (cold start is ~30 s).
- **Secured**: loopback only, per-session random token required on every request;
  anything that is not a JSON object (e.g. an HTTP request from a web page) is dropped.
- **Audit trail**: per-project command history and a replayable `.tcl` journal.

Why this design beat a stdin/stdout pipe wrapper (and a TCP layer on top of it):
see [docs/BACKEND_DECISION.md](docs/BACKEND_DECISION.md). The old modules live in `legacy/`.

## Requirements

- Windows 10/11
- Python 3.10+
- Vivado installed locally (developed against 2025.2)
- `uv` (recommended) or `pip`

## Quick install

```bash
git clone <your-repo-url>
cd Vivado_MCP_Windows
uv sync
```

## Vivado path configuration

The server resolves `vivado.bat` in this order:

1. `start_session` tool argument `vivado_path`
2. Environment variable `VIVADO_BAT_PATH`
3. PATH lookup (`vivado.bat` or `vivado`)
4. Common install locations (`C:\AMDDesignTools\2025.2\...`, `C:\VIVADO\2025.2\...`, `C:\Xilinx\Vivado\2025.2\...`)

Recommended: set `VIVADO_BAT_PATH` explicitly.

```powershell
$env:VIVADO_BAT_PATH = "C:\AMDDesignTools\2025.2\Vivado\bin\vivado.bat"
```

## Run the MCP server

```bash
uv run vivado-mcp
```

This is a stdio MCP server: it waits for MCP messages from a client and looks idle
when run directly in a terminal. **Restart it after updating the code** so the
client loads the new tools.

## MCP client configuration

* You must insert the directory of where you installd the vivado-mcp server
```json
{
	"mcpServers": {
		"vivado": {
			"command": "uv",
			"args": ["run",
			"--directory",
            "DIRECTORY TO THE VIVADO MCP FOLDER",
			 "vivado-mcp"],
			"env": {
				"VIVADO_BAT_PATH": "C:/AMDDesignTools/2025.2/Vivado/bin/vivado.bat"
			}
		}
	}
}
```

### Optional: document search (RAG)

The agent can search reference PDFs (board user guides, datasheets) through `search_docs`.
Everything lives in this project's `rag/` folder: `config.yaml` (tracked), `sources/` for the
PDFs and `db/` for the LanceDB database (both git-ignored).

1. Install the extra once: `uv sync --extra rag`.
2. Start the embedding model named in `rag/config.yaml` (default: `bge-base-en-v1.5-fp16-ov`
   on OVMS at `http://localhost:8000`, OpenAI-compatible `/v1/embeddings`).
3. Put PDFs in `rag/sources/` and build the database:

```bash
uv run --extra rag vivado-docs ingest rag/sources
```

4. Add `"--extra", "rag"` before `"vivado-mcp"` in the MCP client `args` and restart it.

The same embedding model must be used for ingesting and searching; after changing it, delete
`rag/db` and ingest again. `vivado-docs list`, `remove <file>`, `reindex` and `config` manage
the database. To query it from a terminal (same search as the agent's `search_docs`):

```bash
uv run --extra rag vivado-docs search "ZedBoard 100 MHz clock pin" -k 5
```

`-s <file>` limits it to one document, `--json` prints the raw result, and
`vivado-docs page <file> <page>` prints a whole page. `VIVADO_MCP_RAG_CONFIG` points at a different config file if needed.
Embeddings come only from a local server (OVMS; LM Studio and Ollama also work); no cloud
service or API key is used.

## Tools

### Session
- `start_session(vivado_path?, show_window?, project?, host?, port?)`: attach to a running Vivado or launch one. `show_window=true` opens a visible Vivado console.
- `run_tcl_command(command, wait=60)`: run Tcl as a **background job** and wait up to `wait` seconds. If it finishes, you get the full result; if not, you get `status: running` and a `job_id` while Vivado keeps going (starts/attaches a session if needed).
- `job_status(job_id?, wait=0)`: collect a job's outcome (default: the most recent job). `wait=N` blocks up to N seconds. While running it returns `elapsed_s` and the console so far.
- `console_tail(lines=100)`: last N lines of Vivado's transcript. Only reads the log file, so it works **while a long command is still running**.
- `session_status()`: alive / attached / busy / running_job / pid / port / console log path.
- `stop_session()`: stop Vivado. If a command is still running, the Vivado process tree is killed (the only way to cancel).

### Guides for the agent
Four markdown guides ship in `src/vivado_mcp/guides/` and are exposed as MCP resources: `vivado://guide/overview`, `vivado://guide/workflow`, `vivado://guide/tcl-cheatsheet`, `vivado://guide/troubleshooting`. The `vivado_guide(topic?)` tool returns the same text for clients that do not surface resources. Edit the `.md` files to change what the agent is told.

### `run_tcl_command` result

| field | meaning |
|---|---|
| `status` | `ok`, `error` (Tcl error), `running` (still going: poll `job_status`) or `busy` (another job holds Vivado; returned immediately, with its `job_id`) |
| `job_id` | id for `job_status` |
| `result` | the Tcl return value |
| `console` | everything Vivado printed during the command, including `INFO:`/`WARNING:`/`ERROR:` messages. **The reason for a failure is usually here**: Vivado's Tcl error is often just "failed due to earlier errors". |
| `error` | Tcl error text, `null` when ok |
| `console_complete` / `console_truncated` | whether the full slice arrived / was capped (the log keeps everything; use `console_tail`) |
| `duration_ms`, `code` | timing and Tcl return code |

Operating rules (Vivado's Tcl loop is single-threaded):
- While a command runs, Vivado answers nothing else, so a new `run_tcl_command` returns `status: busy` at once instead of queueing. Poll with `job_status` / `console_tail` (both are safe while a job runs).
- Long steps (`launch_runs ... -wait_on_run`, `run all`, bitstream) can be sent as-is: the connection stays open for the whole job (ceiling 4 h) and the agent collects the result with `job_status(wait=...)`.
- There is no way to cancel a running Vivado command except `stop_session`, which kills the process (a new session is then needed).
- A Vivado `ERROR` aborts the rest of that command.

### Board programming (Hardware Manager)
- `list_hw_devices(hw_server_url?)`: connect to `hw_server` (launched by Vivado on first use, ~30 s) and list JTAG targets and devices: name, part, `is_fpga` (false for the Zynq `arm_dap_*` debug port) and `done` (DONE pin). Read-only.
- `program_device(bitstream?, device?, probes?, run="impl_1", target?, hw_server_url?)`: program the FPGA over JTAG. Without `bitstream` it uses the newest `.bit` of the open project's `impl_1` run and reports `run_status` / `run_needs_refresh`. The ARM debug port is skipped automatically; `device` / `target` are only needed for multi-FPGA chains or several cables. Returns `done: true` when the design is running. Programming is volatile; flash and SD card are not touched.

Both run as background jobs like `run_tcl_command` (`status: running` + `job_id` if the connect takes longer than `wait`).

### Simulation and artifact inspection
- `close_simulation`: clear active simulation snapshots before a rerun.
- `run_simulation`: configure `sim_1`, launch behavioral or post-synthesis simulation (Vivado 2025.2 syntax), wait for completion, parse common PASS/FAIL markers.
- `list_project_artifacts`, `read_project_artifact`: browse and read bounded files under a project's `.runs` / `.sim` directories (paths cannot escape the project).
- `summarize_simulation_logs`: parse PASS/FAIL markers from existing logs.

### Document search (optional, see "Optional: document search" above)
Read-only search of the `rag/` knowledge base. The tools return matching text with source file and page; they do not generate an answer, the agent does that.
- `search_docs(query, top_k?, source_file?)`: hybrid vector + full-text search merged by weighted reciprocal rank fusion. Falls back to vector-only, with a `note`, when the full-text index is missing.
- `list_docs()`: documents in the database (chunks, page range) and the config in use.
- `read_doc_page(source_file, page_number)`: all stored text of one page, to read around a hit.

New documents ingested with `vivado-docs` are visible on the next call without restarting the server.

### Per-project command logging
- `set_active_project`, `get_project_command_history`, `list_logged_projects`.

## Files and logs

Everything lives under `~/.vivado_mcp/logs/` (override with `VIVADO_MCP_LOG_DIR`):

- `console/vivado_<time>_<port>.log`: Vivado's full transcript for the session.
- `projects/<project>/<project>_history.jsonl`: every command, status, duration, output and console.
- `projects/<project>/<project>_journal.tcl`: replayable script of the successful commands.
- `native_session.json`: host/port/token of the running session, used to re-attach (treat like a password; delete it if you want to force a fresh launch).

Projects are detected automatically from `open_project` / `create_project`, or set with `project=`.

## Environment variables

| variable | purpose |
|---|---|
| `VIVADO_BAT_PATH` | path to `vivado.bat` |
| `VIVADO_SHOW_WINDOW` | `1` = visible Vivado console by default |
| `VIVADO_MCP_LOG_DIR` | where logs and the attach state file are kept |
| `VIVADO_TCL_SERVER_HOST` / `_PORT` | loopback address of the Tcl server (default `127.0.0.1:9877`) |
| `VIVADO_MCP_RAG_CONFIG` | document-search config file or folder (default `rag/config.yaml`) |

The token (`VIVADO_TCL_SERVER_TOKEN`) is generated per session and passed to Vivado automatically.

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```

`tests/test_native_protocol.py` runs the real Tcl server under `tclsh` through a fake
`vivado` launcher; it is skipped on Windows or where `tclsh` is missing.

## Troubleshooting

- `Vivado executable not found`: set `VIVADO_BAT_PATH` or pass `vivado_path` to `start_session`.
- `127.0.0.1:9877 is already in use ...`: an older Vivado server (without token support) or another program holds the port. Close that Vivado or pass another `port`.
- Attach keeps failing: delete `~/.vivado_mcp/logs/native_session.json`; a stale entry is normally removed automatically.
- `console` is empty: check `session_status().console_log` exists and is growing; if Vivado was started some other way (without `-log`), only `result` and `error` are available.
- Import errors for `mcp.*` in the editor: run `uv sync` and select the project virtual environment.
