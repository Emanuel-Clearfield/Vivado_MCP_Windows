# Retired backends

These modules were retired in favour of the Vivado-native Tcl socket backend
(`src/vivado_mcp/vivado_native_tcp_session.py` + `vivado_socket_server.tcl`).
See `docs/BACKEND_DECISION.md` for why.

- `vivado_session.py`    - subprocess-pipe session (prompt scraping on `Vivado%`).
  Gives text only (no success/failure signal) and its `run_detailed()` was never implemented.
- `vivado_tcp_server.py` - TCP wrapper around the pipe session; it ran inside the MCP
  process, so it did not survive an MCP restart.

Not importable from the package and not covered by tests. Kept for reference only;
safe to delete once you no longer need them.
