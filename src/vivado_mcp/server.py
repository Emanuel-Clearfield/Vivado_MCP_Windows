"""
server.py

MCP server exposing a persistent Windows-native Vivado TCL session to LLM
clients over stdio. Uses FastMCP for decorator-based tool registration.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from vivado_mcp.vivado_session import VivadoSession, VivadoSessionError

# --- Singleton session -------------------------------------------------- #
# Only one Vivado process runs at a time. Lazily created on first use.
_session: VivadoSession | None = None


def get_session() -> VivadoSession:
    global _session
    if _session is None:
        _session = VivadoSession()
    return _session


# Initialize FastMCP instead of the low-level Server
mcp = FastMCP("vivado-mcp-win")


# --- Tool implementations ----------------------------------------------- #

@mcp.tool()
async def start_session(vivado_path: str | None = None) -> dict[str, object]:
    """Start a persistent Vivado TCL session (vivado.bat -mode tcl). Optionally override the vivado.bat path. Reuses an existing session if one is already running."""
    session = get_session()
    if vivado_path:
        session.vivado_path = vivado_path
    session.start()
    return {"status": "started", **session.status()}


@mcp.tool()
async def run_tcl_command(command: str, timeout: float | None = None) -> dict[str, object]:
    """Send an arbitrary TCL command to the persistent Vivado session and return its output. Auto-starts a session if none exists."""
    if not command:
        return {"error": "Missing required argument: 'command'."}
    session = get_session()
    if not session.is_alive():
        session.start()
    output = session.run(command, timeout=timeout)
    return {"command": command, "output": output}


@mcp.tool()
async def session_status() -> dict[str, object]:
    """Return status/statistics for the current Vivado session."""
    return get_session().status()


@mcp.tool()
async def stop_session() -> dict[str, object]:
    """Stop the running Vivado session and free resources."""
    get_session().stop()
    return {"status": "stopped"}


# --- Entrypoints -------------------------------------------------------- #
def run() -> None:
    """Console-script entrypoint (see [project.scripts] in pyproject.toml)."""
    # FastMCP abstracts away the stdio streams and event loop setup
    mcp.run()


if __name__ == "__main__":
    run()