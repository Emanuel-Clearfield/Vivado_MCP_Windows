from __future__ import annotations

import time
from pathlib import Path

from vivado_mcp.console_log import ConsoleLog


def _cmd(cmd_id: str, body: str) -> str:
    return f"Vivado% something\n@@VSS-BEGIN {cmd_id}@@\n{body}@@VSS-END {cmd_id}@@\nresult\n"


def test_extracts_text_between_markers(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_text(
        "banner\n" + _cmd("a-1", "INFO: [Test 1-1] hi\nWARNING: [Test 1-2] careful\n"),
        encoding="utf-8",
    )

    out = ConsoleLog(log).extract("a-1", settle=0.2)

    assert out["complete"] is True
    assert out["text"] == "INFO: [Test 1-1] hi\nWARNING: [Test 1-2] careful"


def test_consecutive_commands_do_not_leak_into_each_other(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_text(_cmd("a-1", "one\n") + _cmd("a-2", "two\n"), encoding="utf-8")
    console = ConsoleLog(log)

    assert console.extract("a-1", settle=0.2)["text"] == "one"
    assert console.extract("a-2", settle=0.2)["text"] == "two"


def test_empty_output_and_crlf(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_bytes(
        b"@@VSS-BEGIN x-1@@\r\n@@VSS-END x-1@@\r\n"
        b"@@VSS-BEGIN x-2@@\r\nline1\r\nline2\r\n@@VSS-END x-2@@\r\n"
    )
    console = ConsoleLog(log)

    assert console.extract("x-1", settle=0.2)["text"] == ""
    assert console.extract("x-2", settle=0.2)["text"] == "line1\nline2"


def test_incomplete_command_returns_partial_after_settle(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_text("@@VSS-BEGIN a-1@@\nstill running\n", encoding="utf-8")
    console = ConsoleLog(log)

    t0 = time.monotonic()
    out = console.extract("a-1", settle=0.3)

    assert time.monotonic() - t0 >= 0.25
    assert out["complete"] is False
    assert out["text"] == "still running"
    assert console.partial("a-1") == ""  # position advanced past what extract consumed


def test_partial_reads_running_command_without_waiting(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_text("old\n@@VSS-BEGIN a-1@@\nstep 1\nstep 2\n", encoding="utf-8")

    assert ConsoleLog(log).partial("a-1") == "step 1\nstep 2"


def test_missing_log_returns_quickly_and_unavailable(tmp_path: Path):
    t0 = time.monotonic()
    out = ConsoleLog(tmp_path / "nope.log").extract("a-1", settle=5.0)

    assert time.monotonic() - t0 < 1.0
    assert out["available"] is False
    assert out["text"] == ""


def test_marker_never_logged_gives_empty_but_available(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_text("some other output\n", encoding="utf-8")

    out = ConsoleLog(log).extract("a-1", settle=0.2)

    assert out["available"] is True and out["complete"] is False and out["text"] == ""


def test_oversized_output_keeps_the_end(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_text(_cmd("a-1", "x" * 500 + "\nERROR: the important part\n"), encoding="utf-8")

    out = ConsoleLog(log, max_chars=100).extract("a-1", settle=0.2)

    assert out["truncated"] is True
    assert out["text"].endswith("ERROR: the important part")
    assert "omitted" in out["text"].splitlines()[0]


def test_tail_drops_marker_lines(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_text(_cmd("a-1", "hello\n"), encoding="utf-8")

    tail = ConsoleLog(log).tail(10)

    assert "@@VSS" not in tail
    assert "hello" in tail


def test_skip_to_end_ignores_history(tmp_path: Path):
    log = tmp_path / "v.log"
    log.write_text(_cmd("a-1", "old\n"), encoding="utf-8")
    console = ConsoleLog(log)
    console.skip_to_end()

    assert console.extract("a-1", settle=0.2)["available"] is True
    assert console.extract("a-1", settle=0.0)["text"] == ""
