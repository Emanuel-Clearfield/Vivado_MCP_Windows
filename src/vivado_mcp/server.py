"""MCP tools for a persistent Vivado Tcl session (Vivado-native socket backend)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from vivado_mcp.project_artifacts import (
    list_artifacts,
    parse_simulation_result,
    read_artifact,
)
from vivado_mcp.session_logger import SessionLogger
from vivado_mcp.simulation import run_simulation as execute_simulation
from vivado_mcp.vivado_native_tcp_session import (
    VivadoBusyError,
    VivadoNativeTCPError,
    VivadoNativeTCPSession,
)

_DEFAULT_SHOW_WINDOW = os.environ.get("VIVADO_SHOW_WINDOW", "").lower() in {
    "1",
    "true",
    "yes",
}

_session: VivadoNativeTCPSession | None = None

mcp = FastMCP("vivado-mcp-win")


def _err(command: str, error: str) -> dict[str, object]:
    return {
        "status": "error",
        "command": command,
        "result": "",
        "console": "",
        "error": error,
    }


def get_session(
    host: str | None = None,
    port: int | None = None,
    vivado_path: str | None = None,
    show_window: bool | None = None,
    project: str | None = None,
) -> VivadoNativeTCPSession:
    """Return the process-wide session, creating it on first use."""
    global _session
    window = _DEFAULT_SHOW_WINDOW if show_window is None else show_window
    if _session is not None and (
        (host and host != _session.host) or (port and port != _session.port)
    ):
        if _session.is_alive():
            raise VivadoNativeTCPError(
                f"A session is already running on {_session.host}:{_session.port}. "
                "Call stop_session first to use a different host/port."
            )
        _session = None
    if _session is None:
        _session = VivadoNativeTCPSession(
            vivado_path=vivado_path,
            host=host,
            port=port,
            show_window=window,
            project=project,
        )
    else:
        if vivado_path:
            _session.vivado_path = vivado_path
        if show_window is not None:
            _session.show_window = window
        if project:
            _session.set_project(project)
    return _session


def _ensure_started(session: VivadoNativeTCPSession) -> None:
    if not session.is_alive():
        session.start()


@mcp.tool()
async def start_session(
    vivado_path: str | None = None,
    show_window: bool | None = None,
    project: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> dict[str, object]:
    """Start Vivado (or attach to one a previous MCP process left running).

    Vivado hosts its own Tcl socket server, so the session survives MCP restarts:
    calling this again re-attaches instead of launching a second Vivado.
    show_window=true opens a visible Vivado console. Cold start takes ~30 s.
    """
    try:
        session = get_session(
            host=host,
            port=port,
            vivado_path=vivado_path,
            show_window=show_window,
            project=project,
        )
        if session.is_alive():
            return {"status": "already_running", **session.status()}
        await asyncio.to_thread(session.start)
        info = session.status()
        return {"status": "attached" if info["attached"] else "started", **info}
    except VivadoNativeTCPError as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def run_tcl_command(command: str, wait: float = 60.0) -> dict[str, object]:
    """Run a Tcl command in the live Vivado session (starts/attaches one if needed).

    The command runs as a background job. This call waits up to `wait` seconds
    (default 60) for it to finish:

      * finished -> the full result (below).
      * still going -> status "running" plus a `job_id` and the console so far.
        Vivado KEEPS RUNNING it; nothing is lost. Call `job_status(job_id, wait=N)`
        to wait for it, and `console_tail` to watch progress. Use this for
        synthesis, implementation, bitstream and simulation, which take minutes.

    Result fields:
      status   ok | error | running | busy
      result   the Tcl return value
      console  everything Vivado PRINTED while the command ran, including its
               INFO / WARNING / CRITICAL WARNING / ERROR messages and puts output.
               The reason a command failed is usually here, not in `error`.
      error    Tcl error text (null when ok)
      job_id   id to pass to job_status

    Vivado runs one command at a time. While a job runs, new commands return
    status "busy" immediately (with the running job's id) instead of waiting.
    A Vivado ERROR aborts the command. To cancel a running job, call
    stop_session (kills Vivado) and start a new session.

    Unsure how to proceed? Read the guide: `vivado_guide('overview')` or
    `vivado_guide('workflow')` (also the resources vivado://guide/*).
    """
    if not command or not command.strip():
        return _err("", "Missing required argument: 'command'.")
    wait = max(0.0, float(wait))
    try:
        session = get_session()
        await asyncio.to_thread(_ensure_started, session)
        try:
            job_id = await asyncio.to_thread(session.submit, command)
        except VivadoBusyError as exc:
            out = _err(command, str(exc))
            out["status"] = "busy"
            out["job_id"] = session.running_job()
            return out
        return await asyncio.to_thread(session.wait_job, job_id, wait)
    except VivadoNativeTCPError as exc:
        return _err(command, str(exc))


@mcp.tool()
async def job_status(job_id: str = "", wait: float = 0.0) -> dict[str, object]:
    """Get the outcome of a background Tcl job started by run_tcl_command.

    `job_id` defaults to the most recent job. With `wait` > 0 this blocks up to
    that many seconds for the job to finish. Returns the same fields as
    run_tcl_command: status "running" (with elapsed_s and the console so far)
    until it finishes, then ok / error with the complete result and console.
    The last 10 jobs are remembered.
    """
    if _session is None:
        return {"status": "error", "error": "No session has been started."}
    try:
        return await asyncio.to_thread(_session.wait_job, job_id or None, max(0.0, float(wait)))
    except VivadoNativeTCPError as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def console_tail(lines: int = 100) -> dict[str, object]:
    """Return the last N lines of Vivado's console transcript.

    Safe to call while a long command is still running (it only reads the log
    file), so use it to follow progress of runs, simulations and timeouts.
    """
    if lines < 1:
        return {"status": "error", "error": "lines must be at least 1"}
    if _session is None:
        return {"status": "error", "error": "No session has been started."}
    return {"status": "ok", "lines": lines, "console": _session.console_tail(lines)}


@mcp.tool()
async def session_status() -> dict[str, object]:
    """Return session status (alive, attached, busy, running_job, pid, port, console log path)."""
    return await asyncio.to_thread(get_session().status)


@mcp.tool()
async def stop_session() -> dict[str, object]:
    """Stop Vivado. If a command is still running the Vivado process is killed."""
    if _session is None:
        return {"status": "not_running"}
    await asyncio.to_thread(_session.stop)
    return {"status": "stopped"}


@mcp.tool()
async def close_simulation(force: bool = True) -> dict[str, object]:
    """Close active Vivado simulation snapshots before a new launch."""
    commands = ["catch {close_sim -quiet -force}"]
    if force:
        commands.append("catch {close_sim -quiet}")
    try:
        session = get_session()
        await asyncio.to_thread(_ensure_started, session)
        for command in commands:
            await asyncio.to_thread(session.run, command)
        return {"status": "closed"}
    except VivadoNativeTCPError as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def run_simulation(
    project_path: str | None = None,
    mode: str = "behavioral",
    sim_type: str = "functional",
    top: str | None = None,
    timeout: float = 300.0,
    force: bool = True,
) -> dict[str, object]:
    """Run a Vivado simulation and return parsed PASS/FAIL results."""
    try:
        session = get_session()
        await asyncio.to_thread(_ensure_started, session)
        return await asyncio.to_thread(
            lambda: execute_simulation(
                session=session,
                project_path=project_path,
                mode=mode,
                sim_type=sim_type,
                top=top,
                timeout=timeout,
                force=force,
            )
        )
    except (OSError, RuntimeError, ValueError, VivadoNativeTCPError) as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def list_project_artifacts(
    project_path: str,
    pattern: str = "*",
    limit: int = 200,
) -> dict[str, object]:
    """List logs and generated artifacts beneath a Vivado project directory."""
    try:
        return {"status": "ok", "artifacts": list_artifacts(project_path, pattern, limit)}
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def read_project_artifact(
    project_path: str,
    artifact_path: str,
    max_bytes: int = 2 * 1024 * 1024,
    tail_lines: int | None = None,
) -> dict[str, object]:
    """Read a bounded generated Vivado artifact beneath a project directory."""
    try:
        return {
            "status": "ok",
            **read_artifact(project_path, artifact_path, max_bytes, tail_lines),
        }
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def summarize_simulation_logs(project_path: str) -> dict[str, object]:
    """Parse common PASS/FAIL markers from Vivado simulation logs."""
    try:
        return {"status": "ok", **parse_simulation_result(project_path)}
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
async def set_active_project(project: str) -> dict[str, object]:
    """Set the project used for command logging."""
    if not project or not project.strip():
        return {"error": "Project name cannot be empty."}
    name = project.strip()
    if _session is not None:
        _session.set_project(name)
    return {"status": "active_project_set", "project": name}


@mcp.tool()
async def get_project_command_history(
    project: str,
    limit: int = 50,
) -> dict[str, object]:
    """Return recent logged commands for a project."""
    if not project or not project.strip():
        return {"error": "Project name cannot be empty."}
    name = project.strip()
    entries = SessionLogger.read_project_history(name, limit=limit)
    return {"project": name, "total_returned": len(entries), "history": entries}


@mcp.tool()
async def list_logged_projects() -> dict[str, object]:
    """List projects with recorded command history."""
    return {"projects": SessionLogger.list_projects()}


_GUIDE_DIR = Path(__file__).parent / "guides"
_GUIDES = {
    "overview": "Tools, result fields and operating rules. Read this first.",
    "workflow": "Project -> simulate -> synthesis -> implementation -> bitstream -> program, with Tcl.",
    "tcl-cheatsheet": "Common Vivado/Tcl commands, properties, help, messages.",
    "troubleshooting": "Symptoms (running, busy, vague errors, crashes) and what to do.",
}


def _read_guide(topic: str) -> str:
    if topic not in _GUIDES:
        raise KeyError(f"Unknown guide {topic!r}. Available: {', '.join(_GUIDES)}")
    return (_GUIDE_DIR / f"{topic}.md").read_text(encoding="utf-8")


@mcp.resource("vivado://guide/overview", mime_type="text/markdown")
def guide_overview() -> str:
    """Tools, result fields and operating rules. Read this first."""
    return _read_guide("overview")


@mcp.resource("vivado://guide/workflow", mime_type="text/markdown")
def guide_workflow() -> str:
    """Typical FPGA flow in Vivado Tcl: project, simulation, synthesis, implementation, bitstream."""
    return _read_guide("workflow")


@mcp.resource("vivado://guide/tcl-cheatsheet", mime_type="text/markdown")
def guide_tcl_cheatsheet() -> str:
    """Common Vivado/Tcl commands, properties, help and message handling."""
    return _read_guide("tcl-cheatsheet")


@mcp.resource("vivado://guide/troubleshooting", mime_type="text/markdown")
def guide_troubleshooting() -> str:
    """What each status/symptom means and how to react."""
    return _read_guide("troubleshooting")


@mcp.tool()
async def vivado_guide(topic: str = "") -> dict[str, object]:
    """Read a usage guide (same text as the vivado://guide/<topic> resources).

    Topics: overview, workflow, tcl-cheatsheet, troubleshooting. Call with no topic
    to list them. Use this when your client does not expose MCP resources.
    """
    if not topic:
        return {"status": "ok", "topics": _GUIDES}
    try:
        return {"status": "ok", "topic": topic, "text": _read_guide(topic)}
    except KeyError as exc:
        return {"status": "error", "error": str(exc.args[0]), "topics": _GUIDES}


def run() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    run()
