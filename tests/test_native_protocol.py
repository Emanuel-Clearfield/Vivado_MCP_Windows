"""
End-to-end tests of the real ``vivado_socket_server.tcl`` + Python client.

A fake ``vivado`` launcher runs the bundled Tcl script under ``tclsh`` and tees
its output into the ``-log`` file, exactly like Vivado's transcript. Skipped on
Windows and wherever ``tclsh`` is missing.
"""

from __future__ import annotations

import json
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from vivado_mcp.vivado_native_tcp_session import (
    VivadoCommandError,
    VivadoNativeTCPError,
    VivadoNativeTCPSession,
)

try:
    import shutil

    _HAVE_TCLSH = shutil.which("tclsh") is not None
except Exception:  # pragma: no cover
    _HAVE_TCLSH = False

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or not _HAVE_TCLSH,
    reason="needs tclsh and a POSIX shell-less launcher",
)

_FAKE_VIVADO = """#!{python}
import subprocess, sys
args = sys.argv[1:]
src = args[args.index("-source") + 1]
log = args[args.index("-log") + 1]
proc = subprocess.Popen(["tclsh", src], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1)
with open(log, "a", encoding="utf-8") as fh:
    for line in proc.stdout:
        fh.write(line)
        fh.flush()
        sys.stdout.write(line)
        sys.stdout.flush()
sys.exit(proc.wait())
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_vivado(tmp_path: Path) -> str:
    path = tmp_path / "bin" / "vivado"
    path.parent.mkdir()
    path.write_text(_FAKE_VIVADO.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def make_session(tmp_path: Path, fake_vivado: str):
    sessions: list[VivadoNativeTCPSession] = []
    port = _free_port()

    def factory(**kw) -> VivadoNativeTCPSession:
        kw.setdefault("startup_timeout", 30)
        kw.setdefault("console_settle", 3.0)
        s = VivadoNativeTCPSession(
            vivado_path=fake_vivado, port=port, log_dir=str(tmp_path / "logs"), **kw
        )
        sessions.append(s)
        return s

    factory.port = port  # type: ignore[attr-defined]
    yield factory
    for s in sessions:
        try:
            s.stop()
        except Exception:
            pass


def test_result_console_and_tcl_errors(make_session):
    s = make_session()
    s.start()

    ok = s.run_detailed("puts hello; expr {6*7}")
    assert ok["status"] == "ok"
    assert ok["result"] == "42"
    assert ok["console"] == "hello"
    assert ok["console_complete"] is True

    bad = s.run_detailed("puts before; error boom")
    assert bad["status"] == "error"
    assert bad["error"] == "boom"
    assert "before" in bad["console"]

    with pytest.raises(VivadoCommandError) as exc:
        s.run("nosuchcommand")
    assert "invalid command name" in str(exc.value)


def test_top_level_return_is_not_an_error(make_session):
    s = make_session()
    s.start()

    env = s.run_detailed("return foo")

    assert env["status"] == "ok" and env["result"] == "foo"


def test_awkward_text_roundtrips(make_session):
    s = make_session()
    s.start()

    assert s.run("format %c 27") == "\x1b"  # control chars used to break the JSON reply
    assert s.run("set p {C:\\Users\\intel\\proj\\x.xpr}") == "C:\\Users\\intel\\proj\\x.xpr"
    assert s.run('set q {say "hi" \\ back}') == 'say "hi" \\ back'
    assert s.run("set a 1\r\nset b 2\r\nexpr {$a+$b}") == "3"
    assert s.run("set u {é😀}") == "é😀"  # non-BMP survives the JSON round trip
    assert s.run("string length {é}") == "1"


def test_large_command_is_not_dropped(make_session):
    s = make_session()
    s.start()

    assert s.run("string length {" + "a" * 1_500_000 + "}", timeout=60) == "1500000"


def test_requests_without_the_token_are_rejected(make_session):
    s = make_session()
    s.start()

    def raw(payload: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", s.port), timeout=5) as sock:
            sock.sendall(payload)
            return sock.makefile("rb").readline()

    no_token = json.loads(raw(b'{"op": "exec", "command": "set ::pwned 1"}\n'))
    assert no_token["status"] == "error" and no_token["error"] == "unauthorized"

    wrong = json.loads(raw(b'{"op": "exec", "command": "set ::pwned 1", "token": "nope"}\n'))
    assert wrong["error"] == "unauthorized"

    # A forged token hidden inside the command text must not count.
    forged = json.dumps({"command": 'set ::pwned 1 ;# "token": "' + (s._token or "") + '"'})
    assert json.loads(raw(forged.encode() + b"\n"))["error"] == "unauthorized"

    assert s.run("info exists ::pwned") == "0"


def test_http_style_requests_are_dropped_silently(make_session):
    s = make_session()
    s.start()
    body = b'{"command": "set ::pwned 1"}\n'
    http = (
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: text/plain\r\n"
        b"Content-Length: %d\r\n\r\n" % len(body)
    ) + body

    with socket.create_connection(("127.0.0.1", s.port), timeout=5) as sock:
        sock.sendall(http)
        assert sock.makefile("rb").readline() == b""  # closed, no reply

    time.sleep(0.2)
    assert s.run("info exists ::pwned") == "0"


def test_attach_to_running_server_from_a_new_session(make_session):
    first = make_session()
    first.start()
    first.run("set ::marker survived")

    second = make_session()  # same port + log_dir, like a restarted MCP process
    second.start()

    assert second.status()["attached"] is True
    assert second.run("set ::marker") == "survived"
    assert second.run_detailed("puts via-second")["console"] == "via-second"

    second.stop()  # asks Vivado to exit
    deadline = time.time() + 10
    while first.is_alive() and time.time() < deadline:
        time.sleep(0.2)
    assert first.is_alive() is False


def test_stale_state_file_is_ignored_and_new_server_started(make_session, tmp_path):
    state = tmp_path / "logs" / "native_session.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps({"host": "127.0.0.1", "port": make_session.port, "token": "dead"}),
        encoding="utf-8",
    )

    s = make_session()
    s.start()

    assert s.status()["attached"] is False
    assert s.run("expr {1+1}") == "2"


def test_foreign_listener_on_the_port_gives_a_clear_error(make_session):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", make_session.port))
        blocker.listen(1)
        with pytest.raises(VivadoNativeTCPError) as exc:
            make_session().start()
    assert "already in use" in str(exc.value)


def test_timeout_reports_partial_console_and_next_command_is_clean(make_session):
    s = make_session()
    s.start()

    slow = s.run_detailed("puts started; after 2500; puts finished", timeout=1)

    assert slow["status"] == "timeout"
    assert "started" in slow["console"]
    assert "STILL running" in slow["error"]

    nxt = s.run_detailed("puts next; expr {1}", timeout=30)  # queues behind the slow one
    assert nxt["status"] == "ok"
    assert nxt["console"] == "next"  # no leakage from the earlier command


def test_stop_during_a_running_command_kills_vivado(make_session):
    s = make_session()
    s.start()
    outcome: dict = {}

    def run():
        try:
            outcome["env"] = s.run_detailed("after 30000", timeout=60)
        except VivadoNativeTCPError as exc:
            outcome["exc"] = exc

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.8)
    assert s.status()["busy"] is True

    s.stop()
    t.join(timeout=15)

    assert not t.is_alive()
    assert s.is_alive() is False
    assert "exc" in outcome or outcome["env"]["status"] != "ok"


def test_status_does_not_expose_the_token(make_session):
    s = make_session()
    s.start()

    assert s._token and s._token not in json.dumps(s.status(), default=str)


def test_background_job_runs_to_completion_and_is_collectable(make_session):
    s = make_session()
    s.start()

    job = s.submit("puts started; after 1500; puts finished; expr {7*6}")
    snap = s.wait_job(job)  # not finished yet
    assert snap["status"] == "running"
    assert snap["job_id"] == job
    assert s.running_job() == job
    assert s.status()["running_job"] == job

    done = s.wait_job(job, wait=20)
    assert done["status"] == "ok"
    assert done["result"] == "42"
    assert done["console"] == "started\nfinished"
    assert done["console_complete"] is True
    assert s.running_job() is None
    assert s.wait_job()["job_id"] == job  # default = most recent


def test_running_job_exposes_partial_console(make_session):
    s = make_session()
    s.start()
    job = s.submit("puts first; after 2000; puts second")
    time.sleep(0.8)
    snap = s.wait_job(job)
    assert snap["status"] == "running"
    assert "first" in snap["console"] and "second" not in snap["console"]
    assert s.wait_job(job, wait=20)["status"] == "ok"


def test_second_command_while_a_job_runs_is_rejected_not_queued(make_session):
    from vivado_mcp.vivado_native_tcp_session import VivadoBusyError

    s = make_session()
    s.start()
    job = s.submit("after 1500; expr {1}")
    with pytest.raises(VivadoBusyError) as exc:
        s.submit("expr {2}")
    assert job in str(exc.value)
    with pytest.raises(VivadoBusyError):
        s.run_detailed("expr {3}")

    assert s.wait_job(job, wait=20)["status"] == "ok"
    nxt = s.run_detailed("puts after; expr {4}")  # free again
    assert nxt["status"] == "ok" and nxt["console"] == "after"


def test_job_tcl_error_and_unknown_job(make_session):
    s = make_session()
    s.start()
    job = s.submit("puts before; error boom")
    env = s.wait_job(job, wait=20)
    assert env["status"] == "error" and env["error"] == "boom"
    assert "before" in env["console"]
    with pytest.raises(VivadoNativeTCPError):
        s.wait_job("nope-1")


def test_stop_kills_a_running_job_and_finishes_it(make_session):
    s = make_session()
    s.start()
    job = s.submit("after 30000")
    time.sleep(0.8)
    s.stop()
    env = s.wait_job(job, wait=15)
    assert env["status"] in ("error", "timeout")
    assert s.is_alive() is False
