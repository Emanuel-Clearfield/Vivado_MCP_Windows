"""Vivado simulation orchestration helpers."""

from __future__ import annotations

import time
from pathlib import Path

from vivado_mcp.project_artifacts import parse_simulation_result
from vivado_mcp.vivado_native_tcp_session import VivadoNativeTCPSession


def _tcl_quote(value: str) -> str:
    return "{" + value.replace("}", r"\}").replace("{", r"\{") + "}"


def _run(session: VivadoNativeTCPSession, command: str, timeout: float) -> str:
    return session.run(command, timeout=timeout)


def run_simulation(
    session: VivadoNativeTCPSession,
    project_path: str | None = None,
    mode: str = "behavioral",
    sim_type: str = "functional",
    top: str | None = None,
    timeout: float = 300.0,
    force: bool = True,
) -> dict[str, object]:
    """Configure, launch, run, and summarize a Vivado simulation."""
    valid_modes = {"behavioral", "post-synthesis", "post-implementation"}
    if mode not in valid_modes:
        raise ValueError(f"mode must be one of: {', '.join(sorted(valid_modes))}")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    started = time.monotonic()
    if project_path:
        project = Path(project_path).expanduser().resolve()
        if not project.is_dir():
            raise ValueError(f"Project directory does not exist: {project}")
        xprs = sorted(project.glob("*.xpr"))
        if not xprs:
            raise ValueError(f"No Vivado project file found in: {project}")
        _run(session, f"open_project {_tcl_quote(str(xprs[0]))}", timeout)
    else:
        project_dir = _run(
            session,
            "get_property DIRECTORY [current_project]",
            timeout,
        ).strip()
        project = Path(project_dir)

    if force:
        _run(session, "catch {close_sim -quiet -force}", timeout)
        _run(session, "catch {close_sim -quiet}", timeout)

    fileset = _run(session, "get_filesets sim_1", timeout).strip()
    if not fileset:
        raise RuntimeError("Vivado fileset 'sim_1' was not found")
    if top:
        _run(session, f"set_property top {_tcl_quote(top)} [get_filesets sim_1]", timeout)
    current_top = _run(
        session,
        "get_property top [get_filesets sim_1]",
        timeout,
    ).strip()
    if not current_top:
        raise RuntimeError("Simulation top is not set for fileset 'sim_1'")

    launch = f"launch_simulation -mode {mode}"
    if mode != "behavioral":
        launch += f" -type {sim_type}"
    _run(session, launch, timeout)
    _run(session, "run all", timeout)

    summary = parse_simulation_result(project)
    summary.update(
        {
            "mode": mode,
            "type": sim_type,
            "top": current_top,
            "project": str(project),
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    )
    return summary
