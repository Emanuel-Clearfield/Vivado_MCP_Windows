"""
server.py

MCP server exposing a persistent Windows-native Vivado TCL session to LLM
clients over stdio. The headline tool is `run_tcl_command`, which forwards
arbitrary TCL to the live Vivado process and returns the sanitized output.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from vivado_mcp.vivado_session import VivadoSession, VivadoSessionError

# --- Singleton session -------------------------------------------------- #
# Only one Vivado process runs at a time. Lazily created on first use.
_session: VivadoSession | None = None


def get_session() -> VivadoSession:
    global _session
    if _session is None:
        _session = VivadoSession()
    return _session


server = Server("vivado-mcp-win")


# --- Tool declarations -------------------------------------------------- #
@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="start_session",
            description=(
                "Start a persistent Vivado TCL session (vivado.bat -mode tcl). "
                "Optionally override the vivado.bat path. Reuses an existing "
                "session if one is already running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vivado_path": {
                        "type": "string",
                        "description": (
                            "Full path to vivado.bat, e.g. "
                            r"C:\VIVADO\2025.2\Vivado\bin\vivado.bat"
                        ),
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="run_tcl_command",
            description=(
                "Send an arbitrary TCL command to the persistent Vivado session "
                "and return its output. Auto-starts a session if none exists."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The TCL command to execute in Vivado.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Optional per-command timeout in seconds.",
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="session_status",
            description="Return status/statistics for the current Vivado session.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="stop_session",
            description="Stop the running Vivado session and free resources.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


# --- Tool dispatch ------------------------------------------------------ #
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # Blocking subprocess I/O is offloaded so we never stall the event loop.
        result = await asyncio.to_thread(_dispatch, name, arguments)
    except VivadoSessionError as exc:
        result = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - surface anything else as JSON
        result = {"error": f"Unexpected error: {exc}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


def _dispatch(name: str, arguments: dict) -> dict:
    """Synchronous handler run in a worker thread."""
    if name == "start_session":
        session = get_session()
        if path := arguments.get("vivado_path"):
            session.vivado_path = path
        session.start()
        return {"status": "started", **session.status()}

    if name == "run_tcl_command":
        command = arguments.get("command")
        if not command:
            return {"error": "Missing required argument: 'command'."}
        session = get_session()
        if not session.is_alive():
            session.start()
        output = session.run(command, timeout=arguments.get("timeout"))
        return {"command": command, "output": output}

    if name == "session_status":
        return get_session().status()

    if name == "stop_session":
        get_session().stop()
        return {"status": "stopped"}

    return {"error": f"Unknown tool: {name}"}


# --- Entrypoints -------------------------------------------------------- #
async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def run() -> None:
    """Console-script entrypoint (see [project.scripts] in pyproject.toml)."""
    asyncio.run(main())


if __name__ == "__main__":
    run()