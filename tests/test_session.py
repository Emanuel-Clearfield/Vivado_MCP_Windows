"""
tests/test_session.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from vivado_mcp.session_logger import SessionLogger
from vivado_mcp.vivado_native_tcp_session import VivadoNativeTCPSession


def test_session_logger_per_project_and_detection():
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = SessionLogger(
            backend="native_tcp",
            log_dir=tmpdir,
            project="initial_proj",
        )
        assert logger.project == "initial_proj"

        # Log command
        logger.log_command("get_property PART [current_project]", "ok", "xc7a35t", None, 15.0)

        # Trigger project auto-detection via open_project
        logger.log_command(
            "open_project C:/path/to/my_fpga_design.xpr", "ok", "opened", None, 120.0
        )
        assert logger.project == "my_fpga_design"

        # Trigger project auto-detection via create_project
        logger.log_command(
            "create_project -part xc7a35t new_uart_proj ./uart", "ok", "created", None, 80.0
        )
        assert logger.project == "new_uart_proj"

        logger.log_session_end()

        # Check project listing
        projects = SessionLogger.list_projects(log_dir=tmpdir)
        assert "initial_proj" in projects
        assert "my_fpga_design" in projects
        assert "new_uart_proj" in projects

        # Check history retrieval
        hist = SessionLogger.read_project_history("new_uart_proj", log_dir=tmpdir)
        assert len(hist) == 1
        assert "create_project" in hist[0]["command"]

        # Check replayable tcl journal
        journal_path = Path(tmpdir) / "projects" / "new_uart_proj" / "new_uart_proj_journal.tcl"
        assert journal_path.is_file()
        journal_content = journal_path.read_text(encoding="utf-8")
        assert "create_project" in journal_content


def test_session_show_window_flags(tmp_path):
    silent = VivadoNativeTCPSession(
        vivado_path="dummy.bat", show_window=False, log_dir=str(tmp_path)
    )
    assert silent.show_window is False

    windowed = VivadoNativeTCPSession(
        vivado_path="dummy.bat", show_window=True, project="uart_core", log_dir=str(tmp_path)
    )
    assert windowed.show_window is True
    assert windowed.project == "uart_core"


def test_native_visible_batch_launch_uses_console_wrapper(monkeypatch, tmp_path):
    session = VivadoNativeTCPSession(
        vivado_path=r"C:\Xilinx\Vivado\2025.2\bin\vivado.bat",
        show_window=True,
        log_dir=str(tmp_path),
    )

    monkeypatch.setattr("vivado_mcp.vivado_native_tcp_session.os.name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    command = session._launch_command()

    assert command[:3] == [r"C:\Windows\System32\cmd.exe", "/d", "/c"]
    assert command[3].startswith("call ")
    assert command[3].endswith(">CON 2>&1")
    assert "vivado.bat" in command[3]
    assert "-source" in command[3]
    assert "-log" in command[3]


def test_native_headless_launch_does_not_use_console_wrapper(monkeypatch, tmp_path):
    session = VivadoNativeTCPSession(
        vivado_path=r"C:\Xilinx\Vivado\2025.2\bin\vivado.bat",
        show_window=False,
        log_dir=str(tmp_path),
    )

    monkeypatch.setattr("vivado_mcp.vivado_native_tcp_session.os.name", "nt")

    command = session._launch_command()

    assert command[0].endswith("vivado.bat")
    assert command[0] != "cmd.exe"


def test_launch_captures_console_with_log_file_not_nolog(tmp_path):
    session = VivadoNativeTCPSession(vivado_path="vivado", log_dir=str(tmp_path))

    command = session._launch_command()

    assert "-nolog" not in command
    assert command[command.index("-log") + 1].endswith(".log")
    assert Path(command[command.index("-log") + 1]).parent == tmp_path / "console"
