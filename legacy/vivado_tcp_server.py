"""
vivado_tcp_server.py

Optional TCP socket layer on top of VivadoSession.

Why use this instead of (or alongside) the stdio-pipe approach
--------------------------------------------------------------
* **Session sharing**: multiple MCP server instances, scripts, or CLI tools can
  all talk to the SAME persistent Vivado process over a plain TCP connection,
  without each one paying the ~30 s cold-start cost.
* **Session survival**: the Vivado process (and the TCP server) can keep running
  even when the MCP server process is restarted, updated, or crashes.
* **Debuggability**: you can send ad-hoc TCL commands directly with any TCP
  client (Python socket, netcat, etc.) without going through MCP.
* **Future remote access**: if ever needed, an SSH tunnel gives secure remote
  access with zero extra code.

Wire protocol (line-delimited JSON over TCP)
--------------------------------------------
Every message in BOTH directions is a single JSON object followed by ``\\n``.

Client  → Server request::

    {"command": "<tcl expression>"}\\n

Server → Client response::

    {"output": "<result string>"}\\n          # success
    {"error":  "<error message>"}\\n          # failure

The server processes one request at a time per connection (sequential), but
multiple clients may be connected concurrently — each connection gets its own
thread which serialises access through the VivadoSession lock.

Security
--------
By default the server binds to ``127.0.0.1`` only (loopback). This prevents
remote access without an explicit SSH tunnel or bind override. Never bind to
``0.0.0.0`` on an untrusted network: Vivado TCL has full filesystem access.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from vivado_mcp.vivado_session import VivadoSession, VivadoSessionError

logger = logging.getLogger(__name__)

# Protocol constants
_ENCODING = "utf-8"
_MAX_LINE = 4 * 1024 * 1024  # 4 MiB safety cap per request line
_REQUEST_KEY = "command"
_OUTPUT_KEY = "output"
_ERROR_KEY = "error"


class VivadoTCPServer:
    """
    A TCP server that wraps a :class:`~vivado_mcp.vivado_session.VivadoSession`
    and exposes it over a simple line-delimited JSON protocol.

    Usage::

        session = VivadoSession()
        session.start()

        srv = VivadoTCPServer(session, host="127.0.0.1", port=9876)
        srv.start()          # non-blocking; runs in background threads
        ...
        srv.stop()           # graceful shutdown

    The session object is owned by the caller; ``VivadoTCPServer`` never calls
    ``session.start()`` or ``session.stop()`` itself, so the two layers remain
    independently controllable.
    """

    def __init__(
        self,
        session: VivadoSession,
        host: str = "127.0.0.1",
        port: int = 9876,
        backlog: int = 5,
    ) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.backlog = backlog

        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._client_threads: list[threading.Thread] = []
        self._clients_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def is_running(self) -> bool:
        """Return True if the accept loop is active."""
        return (
            self._accept_thread is not None
            and self._accept_thread.is_alive()
        )

    def start(self) -> None:
        """
        Open the server socket and start the background accept loop.

        Raises :class:`OSError` if the port is already in use.
        """
        if self.is_running():
            return

        self._stop_event.clear()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(self.backlog)
        sock.settimeout(1.0)  # so the accept loop can check _stop_event
        self._server_sock = sock

        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="vivado-tcp-accept",
            daemon=True,
        )
        self._accept_thread.start()
        logger.info("VivadoTCPServer listening on %s:%d", self.host, self.port)

    def stop(self, join_timeout: float = 5.0) -> None:
        """
        Signal the accept loop to stop and close the server socket.

        Active client handlers are given up to *join_timeout* seconds to finish
        their current command before the threads are abandoned (they are daemon
        threads, so they won't block process exit).
        """
        self._stop_event.set()

        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None

        if self._accept_thread and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=join_timeout)

        # Wait briefly for client threads.
        with self._clients_lock:
            threads = list(self._client_threads)
        for t in threads:
            t.join(timeout=join_timeout)

        logger.info("VivadoTCPServer stopped.")

    def status(self) -> dict:
        """Return a status dict for MCP tool introspection."""
        with self._clients_lock:
            active_clients = sum(1 for t in self._client_threads if t.is_alive())
        return {
            "running": self.is_running(),
            "host": self.host,
            "port": self.port,
            "active_clients": active_clients,
        }

    # ------------------------------------------------------------------ #
    # Accept loop (background thread)
    # ------------------------------------------------------------------ #

    def _accept_loop(self) -> None:
        """Accept incoming connections and spin up a handler thread for each."""
        assert self._server_sock is not None
        while not self._stop_event.is_set():
            try:
                conn, addr = self._server_sock.accept()
            except TimeoutError:
                # Normal: 1 s timeout lets us re-check _stop_event.
                continue
            except OSError:
                # Socket was closed by stop(); exit cleanly.
                break

            logger.info("New TCP client connected from %s:%d", *addr)
            t = threading.Thread(
                target=self._handle_client,
                args=(conn, addr),
                name=f"vivado-tcp-client-{addr[1]}",
                daemon=True,
            )
            with self._clients_lock:
                # Prune dead threads before appending.
                self._client_threads = [
                    ct for ct in self._client_threads if ct.is_alive()
                ]
                self._client_threads.append(t)
            t.start()

    # ------------------------------------------------------------------ #
    # Per-client handler (background thread)
    # ------------------------------------------------------------------ #

    def _handle_client(
        self,
        conn: socket.socket,
        addr: tuple[str, int],
    ) -> None:
        """
        Read JSON requests from *conn*, execute TCL commands through the shared
        :attr:`session`, and write JSON responses back.

        The connection is kept alive until the client disconnects or an
        unrecoverable socket error occurs.
        """
        # Wrap the socket in a file-like object for line-by-line reading.
        with conn:
            rfile = conn.makefile("rb")
            try:
                while not self._stop_event.is_set():
                    raw = rfile.readline(_MAX_LINE)
                    if not raw:
                        # Client closed the connection.
                        break

                    response = self._dispatch(raw)
                    payload = json.dumps(response, ensure_ascii=False) + "\n"

                    try:
                        conn.sendall(payload.encode(_ENCODING))
                    except OSError as exc:
                        logger.warning("Send error to %s:%d — %s", *addr, exc)
                        break
            except OSError as exc:
                logger.debug("Client %s:%d connection error: %s", *addr, exc)
            finally:
                rfile.close()
                logger.info("TCP client %s:%d disconnected.", *addr)

    def _dispatch(self, raw: bytes) -> dict:
        """
        Parse a raw request line and run the TCL command.

        Returns a dict that will be JSON-serialised and sent to the client.
        """
        # --- Parse JSON ---
        try:
            msg = json.loads(raw.decode(_ENCODING, errors="replace"))
        except json.JSONDecodeError as exc:
            return {_ERROR_KEY: f"Invalid JSON: {exc}"}

        if not isinstance(msg, dict) or _REQUEST_KEY not in msg:
            return {_ERROR_KEY: f"Request must be a JSON object with a '{_REQUEST_KEY}' field."}

        command: str = str(msg[_REQUEST_KEY]).strip()
        if not command:
            return {_ERROR_KEY: "Empty command."}

        timeout: float | None = msg.get("timeout")  # optional per-command timeout

        # --- Execute via session ---
        try:
            if not self.session.is_alive():
                return {_ERROR_KEY: "Vivado session is not running. Call start_session first."}
            output = self.session.run(command, timeout=timeout)
            return {_OUTPUT_KEY: output}
        except VivadoSessionError as exc:
            return {_ERROR_KEY: str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error handling command %r", command)
            return {_ERROR_KEY: f"Internal error: {exc}"}
