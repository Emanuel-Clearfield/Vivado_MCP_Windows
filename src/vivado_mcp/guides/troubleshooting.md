# Troubleshooting

| symptom | what to do |
|---|---|
| `status: running` | Not an error. `job_status(wait=60)` until done. Watch `console_tail`. |
| `status: busy` | A job holds Vivado. Poll `job_status`; `stop_session` only if you want to kill it. |
| `status: error`, vague error such as "failed due to earlier errors" | The real cause is in `console` (`ERROR: [Xxx n-n] ...`). Fix that, rerun. |
| `invalid command name "x"` | Typo, or the command needs a project/design open. Try `help x`, `info commands *x*`. |
| `No active Vivado session` | Call `start_session`. |
| "Vivado may have crashed" / socket error | Call `stop_session` then `start_session`. Crash dumps are `hs_err_pid*.log` in Vivado's working directory. |
| Port in use / not our server | Another program holds the port. Start with a different `port`. |
| Denied / unauthorized | A different server with another token is on that port. Stop it or use another port. |
| `console_complete: false` | The log slice had not flushed. Use `console_tail`. |
| `console_truncated: true` | Output was capped. The log has everything: `console_tail(lines=500)`. |
| Command seems stuck | `console_tail` for progress, `session_status` for `running_job`. A hang cannot be cancelled, only killed with `stop_session`. |
| Paths with backslashes break | Use forward slashes or `{C:\path}` in braces. |

## Reading failures
1. Look at `console` for `ERROR:` and `CRITICAL WARNING:` lines, top to bottom. The first
   error is usually the cause; later ones follow from it.
2. For run failures read `<proj>.runs/<run>/runme.log` with `read_project_artifact`.
3. Check state: `current_project`, `get_property STATUS [get_runs impl_1]`.
