"""Safe discovery and reading of Vivado project artifacts."""

from __future__ import annotations

import fnmatch
from pathlib import Path

_DEFAULT_DIRECTORIES = (".runs", ".sim", ".Xil")
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def project_root(path: str | Path) -> Path:
    """Return a resolved project directory, rejecting non-directories."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Project directory does not exist: {root}")
    return root


def _is_artifact_path(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    return any(
        part.endswith(_DEFAULT_DIRECTORIES)
        or part in _DEFAULT_DIRECTORIES
        or ".runs" in part
        or ".sim" in part
        for part in relative.parts
    )


def list_artifacts(
    path: str | Path,
    pattern: str = "*",
    limit: int = 200,
) -> list[dict[str, object]]:
    """List files under a project, including sibling-style Vivado directories."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    root = project_root(path)
    entries: list[dict[str, object]] = []
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file() or not fnmatch.fnmatch(candidate.name, pattern):
            continue
        if not _is_artifact_path(root, candidate) and candidate.suffix.lower() not in {
            ".log",
            ".jou",
            ".rpt",
            ".vcd",
            ".wdb",
        }:
            continue
        entries.append(
            {
                "path": str(candidate.relative_to(root)),
                "size_bytes": candidate.stat().st_size,
            }
        )
        if len(entries) >= limit:
            break
    return entries


def read_artifact(
    root_path: str | Path,
    artifact_path: str,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    tail_lines: int | None = None,
) -> dict[str, object]:
    """Read a bounded artifact path beneath the project root."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")
    root = project_root(root_path)
    candidate = (root / artifact_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Artifact path must remain inside the project directory")
    if not candidate.is_file():
        raise FileNotFoundError(f"Artifact does not exist: {artifact_path}")

    data = candidate.read_bytes()
    truncated = len(data) > max_bytes
    if truncated:
        data = data[-max_bytes:]
    text = data.decode("utf-8", errors="replace")
    if tail_lines is not None:
        if tail_lines < 1:
            raise ValueError("tail_lines must be at least 1")
        text = "\n".join(text.splitlines()[-tail_lines:])
    return {
        "path": str(candidate.relative_to(root)),
        "content": text,
        "size_bytes": candidate.stat().st_size,
        "truncated": truncated,
    }


def parse_simulation_result(root_path: str | Path) -> dict[str, object]:
    """Summarize common PASS/FAIL markers from Vivado simulation logs."""
    files = list_artifacts(root_path, pattern="*.log", limit=500)
    pass_count = 0
    fail_count = 0
    errors: list[str] = []
    warnings: list[str] = []
    latest_log: str | None = None
    latest_mtime = 0.0

    root = project_root(root_path)
    for entry in files:
        candidate = root / str(entry["path"])
        mtime = candidate.stat().st_mtime
        if mtime >= latest_mtime:
            latest_mtime = mtime
            latest_log = str(entry["path"])
        text = candidate.read_text(encoding="utf-8", errors="replace")
        upper = text.upper()
        pass_count += upper.count("PASS")
        fail_count += upper.count("FAIL")
        errors.extend(line.strip() for line in text.splitlines() if "ERROR" in line.upper())
        warnings.extend(line.strip() for line in text.splitlines() if "WARNING" in line.upper())

    status = "failed" if fail_count or errors else "passed" if pass_count else "unknown"
    return {
        "status": status,
        "pass_markers": pass_count,
        "fail_markers": fail_count,
        "errors": errors[-20:],
        "warnings": warnings[-20:],
        "log_path": latest_log,
    }
