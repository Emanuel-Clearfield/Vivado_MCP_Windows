# Vivado MCP Server (Windows-Native)

Vivado MCP Server is a Model Context Protocol (MCP) server that lets LLM clients
control a persistent AMD/Xilinx Vivado TCL session on Windows.

It is designed to avoid the `pexpect` dependency and run reliably on native
Windows by using `subprocess.Popen` plus a background stdout reader thread.

## Why use this

- Persistent Vivado process to avoid repeated startup cost.
- Windows-native process handling (no POSIX pseudo-terminal requirement).
- Clean tool API for MCP clients.
- No command prompt popup windows (`CREATE_NO_WINDOW`).

## Requirements

- Windows 10/11
- Python 3.10+
- Vivado installed locally
- `uv` (recommended) or `pip`

## Quick install

### Option A: local development install (uv)

```bash
git clone <your-repo-url>
cd vivado-mcp-win
uv sync
```

### Option B: install from package index (when published)

```bash
pip install vivado-mcp-win
```

## Vivado path configuration

The server resolves `vivado.bat` in this order:

1. `start_session` tool argument `vivado_path`
2. Environment variable `VIVADO_BAT_PATH`
3. PATH lookup (`vivado.bat` or `vivado`)
4. Fallback `C:\VIVADO\2025.2\Vivado\bin\vivado.bat`

Recommended: set `VIVADO_BAT_PATH` explicitly.

PowerShell example:

```powershell
$env:VIVADO_BAT_PATH = "C:\Xilinx\Vivado\2025.2\bin\vivado.bat"
```

## Run the MCP server

From source checkout:

```bash
uv run vivado-mcp
```

From installed package:

```bash
vivado-mcp
```

Note: this is a stdio MCP server, so it waits for MCP messages from a client.
Running it directly in a terminal will appear idle.

## MCP client configuration

Use the `vivado-mcp` command as a stdio server in your MCP client config.

Generic example:

```json
{
	"mcpServers": {
		"vivado": {
			"command": "vivado-mcp",
			"args": [],
			"env": {
				"VIVADO_BAT_PATH": "C:\\VIVADO\\2025.2\\Vivado\\bin\\vivado.bat"
			}
		}
	}
}
```

If your client runs inside this repository, you can also use:

```json
{
	"mcpServers": {
		"vivado": {
			"command": "uv",
			"args": ["run", "vivado-mcp"],
			"env": {
				"VIVADO_BAT_PATH": "C:\\VIVADO\\2025.2\\Vivado\\bin\\vivado.bat"
			}
		}
	}
}
```

## Exposed MCP tools

- `start_session`: starts or reuses a persistent Vivado session.
- `run_tcl_command`: executes TCL in the live session.
- `session_status`: returns session status and metrics.
- `stop_session`: stops the running session.

## Development

Install dev dependencies:

```bash
uv sync --group dev
```

Run lint:

```bash
uv run ruff check .
```

Run tests:

```bash
uv run pytest
```

## Publish checklist

1. Update project URLs in `pyproject.toml`.
2. Bump version.
3. Build package:

```bash
uv build
```

4. Publish (example with trusted publisher or token):

```bash
uv publish
```

## Troubleshooting

- Error `Vivado executable not found`: set `VIVADO_BAT_PATH` or pass
	`vivado_path` to `start_session`.
- Session timeout on startup: increase startup timeout in code or ensure Vivado
	installation is healthy and licensed.
- Import errors in editor for `mcp.*`: run `uv sync` and ensure VS Code uses the
	project virtual environment.
