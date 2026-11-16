"""
session_logger.py

Per-project and per-session command logger for Vivado MCP.

Every command sent to Vivado — via any backend — is recorded with timestamps,
duration, execution status, output, and errors. Logs are organised by FPGA
project name so users and agents have a complete history of commands run
against each design.

Log location & organization
---------------------------
Base directory: ``~/.vivado_mcp/logs/`` (or env var ``VIVADO_MCP_LOG_DIR``)

Directory tree::

    ~/.vivado_mcp/logs/
      └── projects/
          ├── _default/                     # Used before a project is identified
          │   └── 20261109_143000_1234_pipe.jsonl
          └── my_fpga/                      # Per-project folder
              ├── my_fpga_history.jsonl     # Cumulative log across all sessions
              ├── my_fpga_journal.tcl       # Replayable TCL journal of successful commands
              └── 20261109_144500_5678_native_tcp.jsonl # Per-session detailed JSONL

Automatic project detection
---------------------------
The logger detects the active project from commands such as:
  - ``open_project <path>/<name>.xpr``
  - ``create_project <name> <dir>``
A project name can also be explicitly passed when starting the session.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

# Environment variable for log directory override.
_LOG_DIR_ENV = "VIVADO_MCP_LOG_DIR"

# Regexes to detect project from TCL commands
_OPEN_PROJECT_RE = re.compile(
    r"open_project\s+[{\"']?([^}\"'\s]+\.xpr)[}\"']?",
    re.IGNORECASE,
)
_CREATE_PROJECT_RE = re.compile(
    r"create_project\s+(?:-[a-zA-Z0-9_]+\s+[^\s]+\s+)*[{\"']?([a-zA-Z0-9_-]+)[}\"']?",
    re.IGNORECASE,
)


def get_default_log_dir() -> Path:
    base = os.environ.get(_LOG_DIR_ENV)
    if base:
        return Path(base)
    home_dir = Path.home() / ".vivado_mcp" / "logs"
    try:
        home_dir.mkdir(parents=True, exist_ok=True)
        return home_dir
    except (OSError, PermissionError):
        cwd_dir = Path.cwd() / ".vivado_mcp" / "logs"
        try:
            cwd_dir.mkdir(parents=True, exist_ok=True)
            return cwd_dir
        except (OSError, PermissionError):
            import tempfile
            temp_dir = Path(tempfile.gettempdir()) / ".vivado_mcp" / "logs"
            temp_dir.mkdir(parents=True, exist_ok=True)
            return temp_dir


class SessionLogger:
    """
    Thread-safe command logger supporting per-project history and per-session JSONL.
    """

    def __init__(
        self,
        backend: str = "pipe",
        log_dir: str | Path | None = None,
        vivado_path: str = "",
        pid: int | None = None,
        project: str | None = None,
    ) -> None:
        self.backend = backend
        self._vivado_path = vivado_path
        self._pid = pid
        self._lock = threading.Lock()
        self._commands_run = 0
        self._project: str = project.strip() if project else "_default"

        now = datetime.now()
        pid_str = str(pid or os.getpid())
        self.session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{pid_str}_{backend}"

        self._base_dir = Path(log_dir) if log_dir else get_default_log_dir()
        self._setup_project_dir(self._project)

        # Write session start header
        self._write_session_event({
            "type": "session_start",
            "timestamp": self._ts(),
            "session_id": self.session_id,
            "backend": backend,
            "vivado_path": vivado_path,
            "pid": pid,
            "project": self._project,
        })

    def _setup_project_dir(self, project_name: str) -> None:
        self._proj_dir = self._base_dir / "projects" / project_name
        self._proj_dir.mkdir(parents=True, exist_ok=True)
        self._session_log_path = self._proj_dir / f"{self.session_id}.jsonl"
        self._history_log_path = self._proj_dir / f"{project_name}_history.jsonl"
        self._journal_tcl_path = self._proj_dir / f"{project_name}_journal.tcl"

    @property
    def project(self) -> str:
        return self._project

    @property
    def session_log_path(self) -> Path:
        return self._session_log_path

    @property
    def history_log_path(self) -> Path:
        return self._history_log_path

    @property
    def journal_tcl_path(self) -> Path:
        return self._journal_tcl_path

    def set_project(self, project_name: str) -> None:
        """Switch active project name and update file paths."""
        project_name = project_name.strip()
        if not project_name or project_name == self._project:
            return
        with self._lock:
            old_project = self._project
            self._project = project_name
            self._setup_project_dir(project_name)
            self._write_session_event({
                "type": "project_changed",
                "timestamp": self._ts(),
                "session_id": self.session_id,
                "old_project": old_project,
                "new_project": project_name,
            })

    def log_command(
        self,
        command: str,
        status: str,
        output: str | None,
        error: str | None,
        duration_ms: float,
    ) -> None:
        """Append a command entry to both the session log and cumulative project history."""
        with self._lock:
            self._commands_run += 1
            detected = self._detect_project_from_cmd(command)
            if detected and detected != self._project:
                old_project = self._project
                self._project = detected
                self._setup_project_dir(detected)
                self._write_session_event({
                    "type": "project_detected",
                    "timestamp": self._ts(),
                    "session_id": self.session_id,
                    "old_project": old_project,
                    "new_project": detected,
                    "trigger_command": command,
                })

            entry = {
                "type": "command",
                "timestamp": self._ts(),
                "session_id": self.session_id,
                "project": self._project,
                "command": command,
                "status": status,
                "output": output,
                "error": error,
                "duration_ms": round(duration_ms, 2),
            }
            line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"

            # Write to session log
            with self._session_log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

            # Write to cumulative project history
            with self._history_log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

            # If successful, also append to clean replayable TCL journal
            if status == "ok":
                with self._journal_tcl_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"# {self._ts()} (duration: {round(duration_ms, 1)}ms)\n")
                    fh.write(f"{command}\n")

    def log_session_end(self) -> None:
        """Write session termination record."""
        with self._lock:
            self._write_session_event({
                "type": "session_end",
                "timestamp": self._ts(),
                "session_id": self.session_id,
                "project": self._project,
                "commands_run": self._commands_run,
            })

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self._project,
            "commands_run": self._commands_run,
            "session_log": str(self._session_log_path),
            "project_history_log": str(self._history_log_path),
            "journal_tcl": str(self._journal_tcl_path),
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _ts() -> str:
        return datetime.now().isoformat(timespec="microseconds")

    def _write_session_event(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        with self._session_log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    @staticmethod
    def _detect_project_from_cmd(command: str) -> str | None:
        m = _OPEN_PROJECT_RE.search(command)
        if m:
            return Path(m.group(1)).stem
        m = _CREATE_PROJECT_RE.search(command)
        if m:
            name = m.group(1).strip()
            # Avoid matching flag names like "-part"
            if not name.startswith("-"):
                return name
        return None

    # ------------------------------------------------------------------ #
    # Query API (used by MCP tools to inspect past project logs)
    # ------------------------------------------------------------------ #

    @classmethod
    def read_project_history(
        cls,
        project_name: str,
        limit: int = 50,
        log_dir: str | Path | None = None,
    ) -> list[dict]:
        """Read the most recent command history entries for a given project."""
        base = Path(log_dir) if log_dir else get_default_log_dir()
        hist_path = base / "projects" / project_name / f"{project_name}_history.jsonl"
        if not hist_path.is_file():
            return []

        entries: list[dict] = []
        with hist_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "command":
                        entries.append(data)
                except json.JSONDecodeError:
                    continue

        return entries[-limit:]

    @classmethod
    def list_projects(cls, log_dir: str | Path | None = None) -> list[str]:
        """List all project names that have command logs."""
        base = Path(log_dir) if log_dir else get_default_log_dir()
        proj_root = base / "projects"
        if not proj_root.is_dir():
            return []
        return [
            p.name for p in proj_root.iterdir()
            if p.is_dir() and p.name != "_default"
        ]
