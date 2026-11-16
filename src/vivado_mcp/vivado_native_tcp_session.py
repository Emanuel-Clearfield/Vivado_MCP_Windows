"""
vivado_native_tcp_session.py

Python client for the Vivado-hosted Tcl socket server (``vivado_socket_server.tcl``).

Architecture
------------
    MCP server (this module)
        |  TCP, one JSON line per request, loopback only, shared-secret token
        v
    vivado.bat -mode tcl -source vivado_socket_server.tcl -log <console.log>
        |  Tcl event loop serves the socket and runs commands natively
        v
    <console.log>  <-- Vivado's transcript: Tcl output AND INFO/WARNING/ERROR
                       messages, sliced per command via marker lines

What the agent gets back per command
------------------------------------
``status``    ok | error | timeout
``result``    the Tcl return value
``console``   everything Vivado printed while the command ran (messages included)
``error``     Tcl error text (``None`` when ok); the *detail* is usually in ``console``

Session lifecycle
-----------------
* ``start()`` first tries to **attach** to a Vivado left running by an earlier MCP
  process (state file ``native_session.json`` holds host/port/token/log path).
  Only if nothing answers does it launch a new Vivado.
* ``stop()`` asks Vivado to exit. If a command is in flight and we own the process,
  the process is killed instead (the only way to cancel a running Vivado command).

Window visibility
-----------------
Headless by default (``CREATE_NO_WINDOW``). ``show_window=True`` opens a visible
console; the console log is written either way.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from vivado_mcp.console_log import ConsoleLog
from vivado_mcp.session_logger import SessionLogger, get_default_log_dir

CREATE_NO_WINDOW = 0x08000000  # Windows: suppress console popup

# Path to the bundled TCL server startup script.
_TCL_SCRIPT = Path(__file__).with_name("vivado_socket_server.tcl")

# Environment variables (also read by the TCL script).
_HOST_ENV = "VIVADO_TCL_SERVER_HOST"
_PORT_ENV = "VIVADO_TCL_SERVER_PORT"
_TOKEN_ENV = "VIVADO_TCL_SERVER_TOKEN"
_PATH_ENV = "VIVADO_BAT_PATH"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
_ENCODING = "utf-8"
_MAX_RESPONSE = 64 * 1024 * 1024

_READY_STATUS = "ready"
_ERROR_STATUS = "error"


class VivadoNativeTCPError(RuntimeError):
    """Session lifecycle or transport failure."""


class VivadoTimeoutError(VivadoNativeTCPError):
    """The client gave up waiting; Vivado may still be running the command."""


class VivadoBusyError(VivadoNativeTCPError):
    """Vivado is still running a previous command (one at a time)."""


class VivadoCommandError(VivadoNativeTCPError):
    """Vivado ran the command and reported an error (details in ``console``)."""

    def __init__(self, message: str, *, result: str = "", console: str = "", status: str = "error"):
        super().__init__(message)
        self.message = message
        self.result = result
        self.console = console
        self.status = status

    def __str__(self) -> str:
        if self.console:
            return f"{self.message}\n--- Vivado console ---\n{self.console[-2000:]}"
        return self.message


class VivadoNativeTCPSession:
    """
    Manages (or attaches to) one Vivado process serving the Tcl socket protocol.

    Parameters
    ----------
    vivado_path:      explicit ``vivado.bat``; falls back to env/PATH/common installs.
    host / port:      loopback address of the Tcl server.
    startup_timeout:  seconds to wait for the server to come up.
    command_timeout:  default seconds to wait for one command.
    tcl_script:       alternative server script (defaults to the bundled one).
    show_window:      show Vivado's console window.
    log_dir:          base dir for command logs, console transcripts and the
                      attach state file (default ``~/.vivado_mcp/logs``).
    console_settle:   max seconds to wait for a command's log slice to flush.
    """

    def __init__(
        self,
        vivado_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        startup_timeout: float = 120.0,
        command_timeout: float = 300.0,
        tcl_script: str | os.PathLike | None = None,
        show_window: bool = False,
        log_dir: str | None = None,
        project: str | None = None,
        console_settle: float = 3.0,
    ) -> None:
        self.vivado_path = self._resolve_vivado_path(vivado_path)
        self.host = host or os.environ.get(_HOST_ENV, DEFAULT_HOST)
        self.port = int(port or os.environ.get(_PORT_ENV, DEFAULT_PORT))
        self.startup_timeout = startup_timeout
        self.command_timeout = command_timeout
        self.tcl_script = Path(tcl_script) if tcl_script else _TCL_SCRIPT
        self.show_window = show_window
        self.project = project
        self.console_settle = console_settle
        self._log_dir = log_dir
        self._base_dir = Path(log_dir) if log_dir else get_default_log_dir()
        self._state_path = self._base_dir / "native_session.json"

        self._life = threading.Lock()  # start / stop
        self._cmd = threading.Lock()  # one command at a time
        self._inflight = False
        self._jobs: dict[str, dict] = {}
        self._jobs_lock = threading.Lock()
        self.job_timeout = 4 * 3600.0  # hard ceiling for one background job
        self._seq = itertools.count(1)
        self._nonce = secrets.token_hex(3)

        self._proc: subprocess.Popen | None = None
        self._attached = False
        self._token: str | None = None
        self._vivado_pid: int | None = None
        self._console: ConsoleLog | None = None
        self._console_path: Path | None = None
        self._started_at: float | None = None
        self._commands_run = 0
        self._logger: SessionLogger | None = None

        self._pump: threading.Thread | None = None
        self._ready_evt = threading.Event()
        self._ready_msg: dict | None = None
        self._stdout_tail: collections.deque[str] = collections.deque(maxlen=200)

    # ------------------------------------------------------------------ #
    # Static helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_vivado_path(vivado_path: str | None) -> str:
        if vivado_path:
            return vivado_path
        if env_path := os.environ.get(_PATH_ENV):
            return env_path
        if on_path := shutil.which("vivado.bat") or shutil.which("vivado"):
            return on_path
        candidates = [
            r"C:\AMDDesignTools\2025.2\Vivado\bin\vivado.bat",
            r"C:\VIVADO\2025.2\Vivado\bin\vivado.bat",
            r"C:\Xilinx\Vivado\2025.2\bin\vivado.bat",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return candidates[0]

    def _new_console_path(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        directory = self._base_dir / "console"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"vivado_{stamp}_{self.port}.log"

    def _launch_command(self) -> list[str]:
        """Build the command line, attaching visible batch launches to a console."""
        if self._console_path is None:
            self._console_path = self._new_console_path()
        vivado_args = [
            self.vivado_path,
            "-mode", "tcl",
            "-source", str(self.tcl_script),
            "-nojournal",
            "-log", str(self._console_path),
        ]
        if self.show_window and os.name == "nt" and self.vivado_path.lower().endswith(
            (".bat", ".cmd")
        ):
            command = subprocess.list2cmdline(vivado_args)
            # Explicitly target the console device. The MCP server normally has
            # stdio handles owned by its host app, and inheriting those handles
            # makes Vivado output invisible even with CREATE_NEW_CONSOLE.
            return [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                f"call {command} >CON 2>&1",
            ]
        return vivado_args

    # ------------------------------------------------------------------ #
    # State file (lets a restarted MCP process re-attach)
    # ------------------------------------------------------------------ #

    def _write_state(self) -> None:
        state = {
            "host": self.host,
            "port": self.port,
            "token": self._token,
            "pid": self._vivado_pid,
            "console_log": str(self._console.path) if self._console else None,
            "started_at": self._started_at,
        }
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state), encoding="utf-8")
            try:
                os.chmod(self._state_path, 0o600)
            except OSError:
                pass
        except OSError:
            pass  # attach is a convenience; never fail a start because of it

    def _read_state(self) -> dict | None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict) or not state.get("token"):
            return None
        return state

    def _remove_state(self) -> None:
        try:
            self._state_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    def _ping(self, token: str | None, timeout: float = 2.0) -> tuple[str, dict]:
        """
        Probe the server. Returns ``(state, info)`` where state is
        ``ok`` (answered), ``busy`` (accepted but still running a command),
        ``denied`` (token rejected) or ``down``.
        """
        payload = {"op": "ping"}
        if token:
            payload["token"] = token
        line = (json.dumps(payload) + "\n").encode(_ENCODING)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError:
            return "down", {}
        try:
            with sock:
                sock.settimeout(timeout)
                sock.sendall(line)
                raw = sock.makefile("rb").readline(65536)
        except (TimeoutError, socket.timeout):
            return "busy", {}
        except OSError:
            return "down", {}
        if not raw:
            return "down", {}
        try:
            resp = json.loads(raw.decode(_ENCODING, errors="replace"))
        except ValueError:
            return "down", {}
        if resp.get("pong"):
            return "ok", resp
        if resp.get("error") == "unauthorized":
            return "denied", {}
        return "down", {}

    def _send_raw(self, payload: dict, timeout: float = 30.0) -> dict:
        """Open a short-lived connection, send one request, read one reply line."""
        if self._token:
            payload = {**payload, "token": self._token}
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode(_ENCODING)
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(line)
                rfile = sock.makefile("rb")
                raw = rfile.readline(_MAX_RESPONSE)
                rfile.close()
        except (TimeoutError, socket.timeout) as exc:
            raise VivadoTimeoutError(f"No reply from Vivado within {timeout}s") from exc
        except OSError as exc:
            raise VivadoNativeTCPError(
                f"Socket error communicating with Vivado: {exc}. "
                "Vivado may have crashed or been closed; start the session again."
            ) from exc

        if not raw:
            raise VivadoNativeTCPError(
                "Empty response from Vivado TCL server (request rejected or server gone)."
            )
        try:
            return json.loads(raw.decode(_ENCODING, errors="replace"))
        except json.JSONDecodeError as exc:
            raise VivadoNativeTCPError(f"Invalid JSON from server: {exc}\nRaw: {raw[:300]!r}") from exc

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def is_alive(self) -> bool:
        """True if our Vivado process runs, or an attached server still answers."""
        if self._proc is not None:
            return self._proc.poll() is None
        if self._attached:
            if self._inflight:
                return True
            state, _ = self._ping(self._token, timeout=1.5)
            return state in ("ok", "busy")
        return False

    def _port_in_use(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=0.5):
                return True
        except OSError:
            return False

    def _init_logger(self, pid: int | None) -> None:
        self._logger = SessionLogger(
            backend="native_tcp",
            log_dir=self._log_dir,
            vivado_path=self.vivado_path,
            pid=pid,
            project=self.project,
        )

    def _try_attach(self) -> bool:
        """Adopt a Vivado server left running by an earlier MCP process."""
        state = self._read_state()
        if not state:
            return False
        if state.get("host") != self.host or int(state.get("port", 0)) != self.port:
            return False
        status, info = self._ping(state["token"])
        if status not in ("ok", "busy"):
            self._remove_state()  # stale: server is gone (or token no longer valid)
            return False
        self._token = state["token"]
        self._attached = True
        self._vivado_pid = info.get("pid") or state.get("pid")
        self._started_at = state.get("started_at") or time.time()
        log_path = state.get("console_log")
        if log_path and Path(log_path).is_file():
            self._console = ConsoleLog(log_path)
            self._console.skip_to_end()
        self._init_logger(self._vivado_pid)
        return True

    def start(self) -> None:
        """Attach to a running server if there is one, otherwise launch Vivado."""
        with self._life:
            if self.is_alive():
                return
            self._reset_runtime()
            if self._try_attach():
                return
            self._launch()

    def _launch(self) -> None:
        if not os.path.isfile(self.vivado_path):
            raise VivadoNativeTCPError(f"Vivado executable not found: {self.vivado_path}")
        if not self.tcl_script.is_file():
            raise VivadoNativeTCPError(f"TCL server script not found: {self.tcl_script}")
        if self._port_in_use():
            raise VivadoNativeTCPError(
                f"{self.host}:{self.port} is already in use by a server that does not accept "
                "our token (an older Vivado server, or another program). Close that Vivado "
                "or choose another port."
            )

        self._token = secrets.token_hex(16)
        self._console_path = self._new_console_path()
        self._console = ConsoleLog(self._console_path)

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_CONSOLE if self.show_window else CREATE_NO_WINDOW
            )

        env = os.environ.copy()
        env[_HOST_ENV] = self.host
        env[_PORT_ENV] = str(self.port)
        env[_TOKEN_ENV] = self._token

        # With a visible window all three std handles must be None so Windows
        # attaches the new console's screen buffer directly to Vivado.
        stdin = None if self.show_window else subprocess.DEVNULL
        stdout = None if self.show_window else subprocess.PIPE
        stderr = None if self.show_window else subprocess.STDOUT

        try:
            self._proc = subprocess.Popen(
                self._launch_command(),
                cwd=os.path.dirname(self.vivado_path),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                encoding="utf-8" if not self.show_window else None,
                errors="replace" if not self.show_window else None,
                close_fds=self.show_window,
                creationflags=creationflags,
                env=env,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            self._reset_runtime()
            raise VivadoNativeTCPError(f"Failed to launch Vivado: {exc}") from exc

        self._started_at = time.time()
        try:
            if self.show_window:
                self._wait_for_socket()
            else:
                self._wait_for_ready()
        except Exception:
            if self._proc is not None and self._proc.poll() is None:
                self._kill_tree()
            self._reset_runtime()
            raise

        if self._vivado_pid is None:
            _, info = self._ping(self._token)
            self._vivado_pid = info.get("pid")
        self._init_logger(self._proc.pid)
        self._write_state()

    def _kill_tree(self) -> None:
        """
        Kill the launched process AND its children. ``vivado.bat`` runs the real
        ``vivado.exe`` as a child, so ``Popen.kill()`` alone would leave Vivado
        (and its socket) alive.
        """
        proc = self._proc
        if proc is None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=20,
                    creationflags=CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass

    def stop(self) -> None:
        """
        Ask Vivado to exit. If a command is running and we own the process the
        process is killed (the only way to cancel a running Vivado command).
        """
        with self._life:
            if not self.is_alive():
                self._finish()
                return
            if self._proc is not None and self._inflight:
                self._kill_tree()
            else:
                try:
                    self._send_raw({"op": "exit"}, timeout=10)
                except VivadoNativeTCPError:
                    pass
                if self._proc is not None:
                    try:
                        self._proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        self._kill_tree()
                else:
                    deadline = time.time() + 15
                    while time.time() < deadline and self._ping(self._token, 1.0)[0] != "down":
                        time.sleep(0.3)
            self._finish()

    def _reset_runtime(self) -> None:
        self._proc = None
        self._attached = False
        self._token = None
        self._vivado_pid = None
        self._console = None
        self._console_path = None
        self._started_at = None
        self._ready_evt = threading.Event()
        self._ready_msg = None
        self._stdout_tail.clear()

    def _finish(self) -> None:
        if self._logger:
            self._logger.log_session_end()
            self._logger = None
        self._remove_state()
        self._reset_runtime()

    # ------------------------------------------------------------------ #
    # Startup detection
    # ------------------------------------------------------------------ #

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        """Read Vivado's stdout for its whole life (keeps the pipe from filling)."""
        evt = self._ready_evt
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                stripped = line.strip()
                self._stdout_tail.append(stripped)
                if not evt.is_set() and stripped.startswith("{") and '"status"' in stripped:
                    try:
                        msg = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    self._ready_msg = msg
                    evt.set()
        except (OSError, ValueError):
            pass
        finally:
            evt.set()  # EOF / exit also wakes the waiter

    def _wait_for_ready(self) -> None:
        """Headless: wait for the JSON ready line printed by the Tcl script."""
        assert self._proc and self._proc.stdout
        self._pump = threading.Thread(
            target=self._pump_stdout, args=(self._proc,), name="vivado-stdout-pump", daemon=True
        )
        self._pump.start()

        deadline = time.time() + self.startup_timeout
        while not self._ready_evt.wait(0.25):
            if time.time() > deadline:
                raise VivadoNativeTCPError(
                    f"Startup timeout ({self.startup_timeout}s) waiting for the Vivado TCL "
                    "server ready signal.\n" + "\n".join(self._stdout_tail)
                )

        msg = self._ready_msg
        if msg is None:
            raise VivadoNativeTCPError(
                "Vivado exited before emitting a ready signal.\n" + "\n".join(self._stdout_tail)
            )
        if msg.get("status") == _READY_STATUS:
            self._vivado_pid = msg.get("pid")
            return
        raise VivadoNativeTCPError(
            f"Vivado TCL server failed to start: {msg.get('message', 'unknown error')}"
        )

    def _wait_for_socket(self) -> None:
        """Visible window: no stdout pipe, so poll the socket with an authenticated ping."""
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if not self.is_alive():
                raise VivadoNativeTCPError("Vivado process terminated unexpectedly during startup.")
            state, info = self._ping(self._token, timeout=1.0)
            if state == "ok":
                self._vivado_pid = info.get("pid")
                return
            if state == "denied":
                raise VivadoNativeTCPError(
                    f"Another server on {self.host}:{self.port} rejected our token."
                )
            time.sleep(0.5)
        raise VivadoNativeTCPError(
            f"Timed out waiting for Vivado TCP socket on {self.host}:{self.port} "
            f"after {self.startup_timeout}s."
        )

    # ------------------------------------------------------------------ #
    # Command execution
    # ------------------------------------------------------------------ #

    def run(self, command: str, timeout: float | None = None) -> str:
        """Run a command; return the Tcl result or raise :class:`VivadoCommandError`."""
        env = self.run_detailed(command, timeout=timeout)
        if env["status"] == "ok":
            return str(env["result"])
        raise VivadoCommandError(
            str(env.get("error") or "Vivado command failed"),
            result=str(env.get("result") or ""),
            console=str(env.get("console") or ""),
            status=str(env["status"]),
        )

    def run_detailed(self, command: str, timeout: float | None = None) -> dict:
        """
        Run a Tcl command, blocking, and return the full envelope::

            {status, command, result, console, console_complete,
             console_truncated, error, code, duration_ms}

        Tcl errors and timeouts are reported in the envelope (not raised) so the
        console text that explains them is never lost. Transport failures
        (Vivado gone, bad token) raise :class:`VivadoNativeTCPError`; a command
        already running raises :class:`VivadoBusyError`.
        """
        self._require_alive()
        timeout = timeout if timeout is not None else self.command_timeout
        cmd_id = f"{self._nonce}-{next(self._seq)}"
        if not self._cmd.acquire(blocking=False):
            raise VivadoBusyError(self._busy_message())
        return self._execute(command.strip(), cmd_id, timeout)

    def _require_alive(self) -> None:
        if not self.is_alive():
            raise VivadoNativeTCPError("No active Vivado session. Call start() first.")

    def _busy_message(self) -> str:
        with self._jobs_lock:
            running = [j for j in self._jobs.values() if not j["done"].is_set()]
        if running:
            j = running[-1]
            return (
                f"Vivado is busy running job {j['id']} "
                f"({time.monotonic() - j['started']:.0f}s so far): {j['command'][:120]!r}. "
                "Vivado runs one command at a time. Poll job_status or console_tail, "
                "or stop_session to kill it."
            )
        return "Vivado is busy running another command."

    def _execute(
        self, command: str, cmd_id: str, timeout: float, *, raise_transport: bool = True
    ) -> dict:
        """Run one command. The caller holds ``self._cmd``; it is released here."""
        t0 = time.monotonic()
        transport_error: Exception | None = None
        try:
            self._inflight = True
            try:
                try:
                    response = self._send_raw(
                        {"op": "exec", "id": cmd_id, "command": command}, timeout=timeout
                    )
                except VivadoTimeoutError:
                    partial = self._console.partial(cmd_id) if self._console else ""
                    envelope = {
                        "status": "timeout",
                        "command": command,
                        "result": "",
                        "console": partial,
                        "console_complete": False,
                        "console_truncated": False,
                        "error": (
                            f"Timed out after {timeout}s. Vivado is probably STILL running this "
                            "command and will not answer other commands until it finishes; "
                            "use console_tail to follow progress."
                        ),
                        "code": None,
                    }
                except VivadoNativeTCPError as exc:
                    transport_error = exc
                    envelope = {
                        "status": "error",
                        "command": command,
                        "result": "",
                        "console": "",
                        "console_complete": False,
                        "console_truncated": False,
                        "error": str(exc),
                        "code": None,
                    }
                else:
                    ext = (
                        self._console.extract(cmd_id, settle=self.console_settle)
                        if self._console
                        else {"text": "", "complete": False, "truncated": False}
                    )
                    ok = response.get("status") == "ok"
                    envelope = {
                        "status": "ok" if ok else "error",
                        "command": command,
                        "result": response.get("result", "") or "",
                        "console": ext["text"],
                        "console_complete": bool(ext["complete"]),
                        "console_truncated": bool(ext["truncated"]),
                        "error": None if ok else str(response.get("error") or "Unknown Tcl error"),
                        "code": response.get("code"),
                    }
            finally:
                self._inflight = False
            self._commands_run += 1
        finally:
            self._cmd.release()

        envelope["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
        self._log_envelope(envelope)
        if transport_error is not None and raise_transport:
            raise VivadoNativeTCPError(str(transport_error)) from transport_error
        return envelope

    # ------------------------------------------------------------------ #
    # Background jobs
    # ------------------------------------------------------------------ #

    def submit(self, command: str, job_timeout: float | None = None) -> str:
        """
        Start a command in the background and return its job id at once.

        The command runs on its own thread holding the connection open for as
        long as Vivado needs (up to ``job_timeout``, default 4 h). Collect the
        outcome with :meth:`wait_job`. Raises :class:`VivadoBusyError` if Vivado
        is already running something.
        """
        self._require_alive()
        timeout = job_timeout if job_timeout is not None else self.job_timeout
        cmd_id = f"{self._nonce}-{next(self._seq)}"
        if not self._cmd.acquire(blocking=False):
            raise VivadoBusyError(self._busy_message())
        job = {
            "id": cmd_id,
            "command": command.strip(),
            "started": time.monotonic(),
            "done": threading.Event(),
            "envelope": None,
        }
        with self._jobs_lock:
            self._jobs[cmd_id] = job
            for old in [k for k, j in self._jobs.items() if j["done"].is_set()][:-9]:
                del self._jobs[old]  # keep the 9 most recent finished jobs
        threading.Thread(target=self._job_main, args=(job, timeout), daemon=True).start()
        return cmd_id

    def _job_main(self, job: dict, timeout: float) -> None:
        try:
            env = self._execute(job["command"], job["id"], timeout, raise_transport=False)
        except Exception as exc:  # never leave a job unfinished
            env = {
                "status": "error", "command": job["command"], "result": "", "console": "",
                "console_complete": False, "console_truncated": False,
                "error": f"{type(exc).__name__}: {exc}", "code": None, "duration_ms": 0.0,
            }
        job["envelope"] = env
        job["done"].set()

    def wait_job(self, job_id: str | None = None, wait: float = 0.0) -> dict:
        """
        Return a job's envelope if it finished, else a ``running`` snapshot.

        ``job_id`` defaults to the most recent job. ``wait`` blocks up to that
        many seconds for it to finish.
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id) if job_id else (list(self._jobs.values()) or [None])[-1]
        if job is None:
            raise VivadoNativeTCPError(
                f"Unknown job {job_id!r} (only the most recent jobs are remembered)."
                if job_id else "No jobs have been started."
            )
        if wait > 0:
            job["done"].wait(wait)
        if job["done"].is_set():
            return {**job["envelope"], "job_id": job["id"]}
        partial = self._console.partial(job["id"]) if self._console else ""
        return {
            "status": "running",
            "job_id": job["id"],
            "command": job["command"],
            "elapsed_s": round(time.monotonic() - job["started"], 1),
            "console": partial[-4000:],
            "console_complete": False,
            "console_truncated": len(partial) > 4000,
            "result": "",
            "error": None,
        }

    def running_job(self) -> str | None:
        with self._jobs_lock:
            running = [j["id"] for j in self._jobs.values() if not j["done"].is_set()]
        return running[-1] if running else None

    def _log_envelope(self, env: dict) -> None:
        if not self._logger:
            return
        console = str(env.get("console") or "")[:20000]
        result = str(env.get("result") or "")
        ok = env["status"] == "ok"
        output = result if not console else f"{result}\n--- console ---\n{console}"
        error = None if ok else f"{env.get('error')}\n--- console ---\n{console}".strip()
        self._logger.log_command(
            command=str(env["command"]),
            status="ok" if ok else "error",
            output=output if ok else None,
            error=error,
            duration_ms=float(env["duration_ms"]),
        )

    def console_tail(self, lines: int = 100) -> str:
        """Last ``lines`` lines of Vivado's transcript (works while a command runs)."""
        return self._console.tail(lines) if self._console else ""

    def set_project(self, project_name: str) -> None:
        """Explicitly switch active FPGA project name for logging."""
        self.project = project_name
        if self._logger:
            self._logger.set_project(project_name)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def status(self) -> dict:
        uptime = time.time() - self._started_at if self._started_at else 0.0
        base = {
            "alive": self.is_alive(),
            "attached": self._attached,
            "busy": self._inflight,
            "running_job": self.running_job(),
            "vivado_path": self.vivado_path,
            "pid": self._proc.pid if self._proc else self._vivado_pid,
            "host": self.host,
            "port": self.port,
            "uptime_seconds": round(uptime, 1),
            "commands_run": self._commands_run,
            "show_window": self.show_window,
            "project": self._logger.project if self._logger else self.project,
            "console_log": str(self._console.path) if self._console else None,
        }
        if self._logger:
            base["log"] = self._logger.summary()
        return base

    # ------------------------------------------------------------------ #
    # Context manager support
    # ------------------------------------------------------------------ #

    def __enter__(self) -> VivadoNativeTCPSession:
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()
