from __future__ import annotations

from pathlib import Path

import pytest

from vivado_mcp.project_artifacts import (
    list_artifacts,
    parse_simulation_result,
    read_artifact,
)
from vivado_mcp.simulation import run_simulation


class FakeSession:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, timeout: float | None = None) -> str:
        self.commands.append(command)
        if command == "get_property DIRECTORY [current_project]":
            return ""
        if command == "get_filesets sim_1":
            return "sim_1"
        if command == "get_property top [get_filesets sim_1]":
            return "tb_top"
        return ""


def test_artifacts_include_flat_vivado_directories(tmp_path: Path):
    sim_dir = tmp_path / "mux_test.sim" / "sim_1" / "behav" / "xsim"
    sim_dir.mkdir(parents=True)
    log = sim_dir / "xsim.log"
    log.write_text("TEST PASS\n", encoding="utf-8")

    entries = list_artifacts(tmp_path)
    assert any(entry["path"] == str(log.relative_to(tmp_path)) for entry in entries)
    content = read_artifact(tmp_path, str(log.relative_to(tmp_path)))["content"]
    assert content.replace("\r\n", "\n") == "TEST PASS\n"
    assert parse_simulation_result(tmp_path)["status"] == "passed"


def test_artifact_reads_reject_escape(tmp_path: Path):
    with pytest.raises(ValueError):
        read_artifact(tmp_path, "..\\outside.log")


def test_run_simulation_uses_sim_1_and_2025_syntax(tmp_path: Path):
    (tmp_path / "design.xpr").write_text("", encoding="utf-8")
    session = FakeSession()

    result = run_simulation(session, project_path=str(tmp_path), top="tb_top")

    assert result["status"] == "unknown"
    assert "get_filesets sim_1" in session.commands
    assert "launch_simulation -mode behavioral" in session.commands
    assert all("-run_type" not in command for command in session.commands)
