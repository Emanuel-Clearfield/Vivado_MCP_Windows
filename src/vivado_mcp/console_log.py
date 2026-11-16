"""
console_log.py

Reads Vivado's console transcript (the ``-log`` file) and slices out the text a
single command produced.

Why a log file?
---------------
Vivado 2025.2 has no ``redirect``/``tee``, and its INFO / WARNING / ERROR
messages are written below Tcl's channels, so they cannot be captured from
inside Tcl. The transcript written via ``-log`` does contain them (live, in
order), so we read that instead.

The Tcl server prints two marker lines around every command::

    @@VSS-BEGIN <id>@@
    ...command output and Vivado messages...
    @@VSS-END <id>@@

The END marker is written *after* the command finished and flushed, so once it
shows up in the log everything the command printed is already there.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

BEGIN_FMT = "@@VSS-BEGIN {id}@@"
END_FMT = "@@VSS-END {id}@@"
_MARKER_RE = re.compile(r"^@@VSS-(?:BEGIN|END) [^\n]*@@\r?\n?", re.MULTILINE)

#: Cap on console text returned inline with one command (the log keeps it all).
DEFAULT_MAX_CHARS = 60_000


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip("\n")


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Keep the END of oversized output: errors and summaries come last."""
    if max_chars and len(text) > max_chars:
        dropped = len(text) - max_chars
        return f"[... {dropped} earlier characters omitted; see console_tail ...]\n" + text[
            -max_chars:
        ], True
    return text, False


class ConsoleLog:
    """Incremental reader over one Vivado ``-log`` file."""

    def __init__(self, path: str | Path, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self.path = Path(path)
        self.max_chars = max_chars
        self._pos = 0  # bytes before this offset were already consumed
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def exists(self) -> bool:
        return self.path.is_file()

    def skip_to_end(self) -> None:
        """Ignore everything already in the log (used when attaching)."""
        with self._lock:
            self._pos = self.size()

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _read_from(self, offset: int) -> bytes:
        try:
            with self.path.open("rb") as fh:
                fh.seek(offset)
                return fh.read()
        except OSError:
            return b""

    # ------------------------------------------------------------------ #
    def extract(
        self,
        cmd_id: str,
        settle: float = 3.0,
        poll: float = 0.05,
    ) -> dict[str, object]:
        """
        Return the console text between the BEGIN/END markers of ``cmd_id``.

        Waits up to ``settle`` seconds for the END marker. Result keys:
        ``text`` (str), ``complete`` (END marker seen), ``truncated`` (text was
        capped to ``max_chars``), ``available`` (a log file exists at all).
        """
        begin = BEGIN_FMT.format(id=cmd_id).encode()
        end = END_FMT.format(id=cmd_id).encode()

        with self._lock:
            start_pos = self._pos
            deadline = time.monotonic() + max(settle, 0.0)
            data = b""
            while True:
                data = self._read_from(start_pos)
                b = data.find(begin)
                e = data.find(end, b + len(begin)) if b >= 0 else -1
                if e >= 0:
                    break
                if not self.exists():
                    return {"text": "", "complete": False, "truncated": False, "available": False}
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll)

            if b < 0:
                # Marker never reached the log (log not being written?).
                self._pos = start_pos + len(data)
                return {"text": "", "complete": False, "truncated": False, "available": True}

            body_start = data.find(b"\n", b)
            body_start = len(data) if body_start < 0 else body_start + 1
            if e >= 0:
                body = data[body_start:e]
                line_end = data.find(b"\n", e)
                self._pos = start_pos + (len(data) if line_end < 0 else line_end + 1)
                complete = True
            else:
                body = data[body_start:]
                self._pos = start_pos + len(data)
                complete = False

        text = _clean(body.decode("utf-8", errors="replace"))
        text, truncated = _cap(text, self.max_chars)
        return {"text": text, "complete": complete, "truncated": truncated, "available": True}

    def partial(self, cmd_id: str) -> str:
        """Output a still-running command has produced so far (no waiting)."""
        begin = BEGIN_FMT.format(id=cmd_id).encode()
        data = self._read_from(self._pos)
        b = data.find(begin)
        if b < 0:
            return ""
        nl = data.find(b"\n", b)
        body = data[(len(data) if nl < 0 else nl + 1) :]
        text = _clean(body.decode("utf-8", errors="replace"))
        return _cap(text, self.max_chars)[0]

    def tail(self, lines: int = 100, max_bytes: int = 256 * 1024) -> str:
        """Last ``lines`` lines of the transcript, marker lines removed."""
        if lines < 1:
            raise ValueError("lines must be at least 1")
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                data = fh.read()
        except OSError:
            return ""
        text = _MARKER_RE.sub("", data.decode("utf-8", errors="replace"))
        return "\n".join(_clean(text).splitlines()[-lines:])
