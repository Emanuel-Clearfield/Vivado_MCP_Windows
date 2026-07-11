"""
vivado_session.py

Windows-native persistent Vivado TCL session manager.

Design notes
------------
Unlike the reference implementation (coreyhahn/vivado_mcp), this module does NOT
use `pexpect`. pexpect relies on POSIX pseudo-terminals (pty) which are not
natively available on Windows. Instead we drive Vivado through a plain
`subprocess.Popen` pipe and solve the blocking-read problem with a dedicated
background daemon thread that reads stdout one character at a time and feeds a
thread-safe `queue.Queue`. The main thread drains that queue and aggregates
output until it observes the Vivado prompt, at which point the command is known
to have completed.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time

# CREATE_NO_WINDOW: prevents a console window from popping up on Windows when
# spawning vivado.bat. Defined explicitly so the module also imports cleanly on
# non-Windows dev machines (where subprocess.CREATE_NO_WINDOW does not exist).
CREATE_NO_WINDOW = 0x08000000

# Vivado's interactive TCL prompt. We watch for this string to know a command
# has finished executing and control has returned to us.
PROMPT = "Vivado%"

# Default flags for launching Vivado in headless interactive TCL mode.
VIVADO_ARGS = ["-mode", "tcl", "-nojournal", "-nolog"]

# Environment variable checked for an explicit Vivado launcher path.
VIVADO_PATH_ENV = "VIVADO_BAT_PATH"


class VivadoSessionError(RuntimeError):
    """Raised for any session lifecycle or command execution failure."""


class VivadoSession:
    """
    Manages a single persistent `vivado.bat -mode tcl` process on Windows.

    A Vivado cold start costs ~30s, so we keep one process alive and stream
    TCL commands into its stdin, reading results back off a background queue.
    """

    def __init__(
        self,
        vivado_path: str | None = None,
        startup_timeout: float = 120.0,
        command_timeout: float = 300.0,
    ) -> None:
        self.vivado_path = self._resolve_vivado_path(vivado_path)
        self.startup_timeout = startup_timeout
        self.command_timeout = command_timeout

        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self._commands_run = 0

    @staticmethod
    def _resolve_vivado_path(vivado_path: str | None) -> str:
        """Resolve Vivado executable path from explicit arg, env var, or PATH."""
        if vivado_path:
            return vivado_path

        if env_path := os.environ.get(VIVADO_PATH_ENV):
            return env_path

        if on_path := shutil.which("vivado.bat") or shutil.which("vivado"):
            return on_path

        # Keep a common fallback for Windows installs.
        return r"C:\VIVADO\2025.2\Vivado\bin\vivado.bat"

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Launch the Vivado process and wait for the first prompt."""
        with self._lock:
            if self.is_alive():
                return

            if not os.path.isfile(self.vivado_path):
                raise VivadoSessionError(
                    f"Vivado executable not found at: {self.vivado_path}"
                )

            # creationflags is only meaningful on Windows; guard for portability.
            creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0

            try:
                self._proc = subprocess.Popen(
                    [self.vivado_path, *VIVADO_ARGS],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # fold stderr into stdout stream
                    bufsize=0,                 # unbuffered: needed for read(1)
                    universal_newlines=True,   # text-mode I/O (str, not bytes)
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise VivadoSessionError(f"Failed to launch Vivado: {exc}") from exc

            self._started_at = time.time()

            # Spin up the background reader BEFORE waiting for the prompt.
            self._reader = threading.Thread(
                target=self._read_loop, name="vivado-stdout-reader", daemon=True
            )
            self._reader.start()

            # Consume Vivado's banner + first prompt.
            self._wait_for_prompt(timeout=self.startup_timeout)

    def stop(self) -> None:
        """Gracefully quit Vivado, force-killing if it does not exit."""
        with self._lock:
            if not self.is_alive():
                self._proc = None
                return
            try:
                assert self._proc and self._proc.stdin
                self._proc.stdin.write("exit\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=15)
            except Exception:
                self._proc.kill()
            finally:
                self._proc = None
                self._started_at = None

    # ------------------------------------------------------------------ #
    # Background reader thread
    # ------------------------------------------------------------------ #
    def _read_loop(self) -> None:
        """
        Daemon loop: read stdout ONE character at a time and push each char
        onto the thread-safe queue. Reading char-by-char (read(1)) means we
        never block waiting for a newline, so we can detect the prompt the
        instant it appears even though Vivado does not emit a trailing newline
        after "Vivado% ".
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                ch = proc.stdout.read(1)
                if ch == "":  # EOF: process exited
                    break
                self._queue.put(ch)
        except (ValueError, OSError):
            # Stream closed underneath us during shutdown.
            pass

    # ------------------------------------------------------------------ #
    # Prompt aggregation
    # ------------------------------------------------------------------ #
    def _wait_for_prompt(self, timeout: float) -> str:
        """
        Drain characters from the queue, aggregating them into a buffer until
        the Vivado prompt is observed at the tail of the output. Returns the
        full aggregated text (prompt included).
        """
        buf: list[str] = []
        deadline = time.time() + timeout

        while True:
            if not self.is_alive() and self._queue.empty():
                raise VivadoSessionError(
                    "Vivado process exited unexpectedly.\n" + "".join(buf)
                )

            remaining = deadline - time.time()
            if remaining <= 0:
                raise VivadoSessionError(
                    f"Timed out after {timeout}s waiting for Vivado prompt.\n"
                    + "".join(buf)
                )

            try:
                ch = self._queue.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue

            buf.append(ch)

            # Only bother scanning the tail once we've seen the prompt's char.
            if ch == "%":
                tail = "".join(buf[-len(PROMPT):])
                if tail.endswith(PROMPT):
                    return "".join(buf)

    # ------------------------------------------------------------------ #
    # Command execution
    # ------------------------------------------------------------------ #
    def run(self, command: str, timeout: float | None = None) -> str:
        """
        Send a TCL command to the live session and return its output with the
        echoed command line and trailing prompt stripped away.
        """
        if not self.is_alive():
            raise VivadoSessionError("No active Vivado session. Call start() first.")

        command = command.rstrip("\n")
        timeout = timeout if timeout is not None else self.command_timeout

        with self._lock:
            assert self._proc and self._proc.stdin
            try:
                self._proc.stdin.write(command + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise VivadoSessionError(f"Failed to write command: {exc}") from exc

            raw = self._wait_for_prompt(timeout=timeout)
            self._commands_run += 1

        return self._sanitize(raw, command)

    @staticmethod
    def _sanitize(raw: str, command: str) -> str:
        """
        Strip the echoed command and the trailing prompt from Vivado's output.

        Vivado echoes the command back on the first line and terminates with a
        fresh "Vivado% " prompt. We remove both so the MCP client sees only the
        actual command result.
        """
        text = raw

        # Remove the trailing prompt.
        idx = text.rfind(PROMPT)
        if idx != -1:
            text = text[:idx]

        lines = text.splitlines()

        # Drop the leading echoed command line if present.
        if lines and lines[0].strip() == command.strip():
            lines = lines[1:]

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        uptime = time.time() - self._started_at if self._started_at else 0.0
        return {
            "alive": self.is_alive(),
            "vivado_path": self.vivado_path,
            "pid": self._proc.pid if self._proc else None,
            "uptime_seconds": round(uptime, 1),
            "commands_run": self._commands_run,
        }