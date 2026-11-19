"""Document knowledge base (board user guides, datasheets) for the agent.

PDFs are ingested with the `vivado-docs` CLI (see rag_ingest.py) into a LanceDB table. The MCP
tools only READ it: they embed the agent's query with the same embedding model, run a hybrid
search (vector + full-text, reciprocal rank fusion) and return the matching chunks with source
file and page. They do not generate an answer: the calling agent is the LLM.

Configuration: `rag/config.yaml` in this project (the default), or the file (or folder holding
`config.yaml`) named by the env var VIVADO_MCP_RAG_CONFIG. Relative paths in it are resolved
against the config's folder. Embeddings come from a local HTTP server: OVMS (OpenAI-compatible
`/v1/embeddings`), LM Studio or Ollama.

Heavy dependencies (lancedb, pyarrow, pyyaml, pymupdf) are optional and imported lazily:
install them with `uv sync --extra rag` and run the server with `uv run --extra rag`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_ENV = "VIVADO_MCP_RAG_CONFIG"
MAX_TOP_K = 50
_HTTP_TIMEOUT = 120
_EMBED_BATCH = 32
_BACKENDS = ("ovms", "lmstudio", "lm-studio", "openai", "ollama")

Embedder = Callable[[str], list[float]]


class RagError(Exception):
    """Configuration, dependency or backend problem, phrased for the agent."""


class _UnreachableError(RagError):
    """The embedding server could not be connected to."""


def default_config_path() -> Path:
    """`<project>/rag/config.yaml` when running from a source checkout, else ~/.vivado_mcp/rag."""
    project = Path(__file__).resolve().parents[2]
    if (project / "pyproject.toml").is_file():
        return project / "rag" / "config.yaml"
    return Path.home() / ".vivado_mcp" / "rag" / "config.yaml"


@dataclass
class RagSettings:
    config_path: Path
    db_path: Path
    table_name: str = "documents"
    top_k: int = 8
    use_hybrid: bool = True
    fts_weight: float = 0.3
    chunk_size: int = 512
    chunk_overlap: int = 64
    embed_backend: str = "ovms"  # ovms | lmstudio | openai | ollama
    embed_model: str = ""
    embed_base_url: str = "http://localhost:8000"

    def describe(self) -> dict[str, object]:
        return {
            "config": str(self.config_path),
            "database": str(self.db_path),
            "table": self.table_name,
            "embedding": {
                "backend": self.embed_backend,
                "model": self.embed_model,
                "base_url": self.embed_base_url,
            },
            "retrieval": {
                "top_k": self.top_k,
                "use_hybrid": self.use_hybrid,
                "fts_weight": self.fts_weight,
            },
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def missing_deps_message(name: str) -> str:
    return (
        f"Document search needs the optional '{name}' package. Install the extra with "
        "`uv sync --extra rag` in the vivado-mcp folder, and start the server with "
        "`uv run --extra rag vivado-mcp`."
    )


def _import_yaml():
    try:
        import yaml
    except ImportError as exc:
        raise RagError(missing_deps_message("pyyaml")) from exc
    return yaml


def load_settings(config: str | os.PathLike[str] | None = None) -> RagSettings:
    """Load the RAG config: `config`, else $VIVADO_MCP_RAG_CONFIG, else the project default."""
    raw_path = config if config is not None else os.environ.get(CONFIG_ENV, "").strip()
    path = Path(raw_path).expanduser() if raw_path else default_config_path()
    if path.is_dir():
        path = path / "config.yaml"
    if not path.is_file():
        raise RagError(
            f"Document search config not found: {path}. Create it (see rag/config.yaml in the "
            f"vivado-mcp project) or set {CONFIG_ENV} to its path."
        )

    yaml = _import_yaml()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RagError(f"Cannot parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RagError(f"{path} does not contain a YAML mapping.")

    base = path.parent
    emb = raw.get("embedding") or {}
    ret = raw.get("retrieval") or {}
    chk = raw.get("chunking") or {}
    db = raw.get("database") or {}

    backend = str(emb.get("backend", "ovms")).lower()
    if backend not in _BACKENDS:
        raise RagError(
            f"{path}: embedding.backend {backend!r} is not supported "
            f"(use one of: {', '.join(_BACKENDS)})."
        )
    model = str(emb.get("model", "")).strip()
    if not model:
        raise RagError(f"{path}: embedding.model is not set.")
    base_url = str(emb.get("base_url", "http://localhost:8000"))

    db_path = Path(str(db.get("path", "db"))).expanduser()
    if not db_path.is_absolute():
        db_path = base / db_path

    return RagSettings(
        config_path=path,
        db_path=db_path,
        table_name=str(db.get("table_name", "documents")),
        top_k=int(ret.get("top_k", 8)),
        use_hybrid=bool(ret.get("use_hybrid", True)),
        fts_weight=float(ret.get("fts_weight", 0.3)),
        chunk_size=int(chk.get("chunk_size", 512)),
        chunk_overlap=int(chk.get("chunk_overlap", 64)),
        embed_backend=backend,
        embed_model=model,
        embed_base_url=base_url,
    )


# ---------------------------------------------------------------------------
# Embeddings (ingestion and queries must use the same model)
# ---------------------------------------------------------------------------


class EmbeddingClient:
    """embed_query for searching, embed_documents for ingestion."""

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


def _post_json(url: str, payload: dict[str, Any], what: str) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RagError(f"{what} at {url} returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise _UnreachableError(
            f"Cannot reach {what} at {url}: {exc.reason}. Is the server running?"
        ) from exc
    except (OSError, ValueError) as exc:
        raise RagError(f"{what} at {url} failed: {exc}") from exc


class _HTTPEmbedder(EmbeddingClient):
    """OpenAI-compatible (OVMS, LM Studio) or Ollama /api/embed."""

    def __init__(self, url: str, model: str, what: str, ollama: bool = False) -> None:
        self.url, self.model, self.what, self.ollama = url, model, what, ollama
        # "localhost" resolves to ::1 first. Servers such as OVMS listen on IPv4 only, and on
        # Windows the refused IPv6 attempt costs ~2 s per request. Use 127.0.0.1 and fall back
        # to the configured name only if that is refused.
        self._fallback: str | None = None
        parts = urllib.parse.urlsplit(url)
        if (parts.hostname or "").lower() == "localhost":
            netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
            self._fallback, self.url = url, urllib.parse.urlunsplit(parts._replace(netloc=netloc))

    def _post(self, payload: dict[str, Any]) -> Any:
        try:
            return _post_json(self.url, payload, self.what)
        except _UnreachableError:
            if self._fallback is None:
                raise
            self.url, self._fallback = self._fallback, None
            return _post_json(self.url, payload, self.what)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH):
            batch = texts[start : start + _EMBED_BATCH]
            res = self._post({"model": self.model, "input": batch})
            try:
                if self.ollama:
                    vectors = res["embeddings"]
                else:
                    items = sorted(res["data"], key=lambda d: d.get("index", 0))
                    vectors = [item["embedding"] for item in items]
                if len(vectors) != len(batch):
                    raise ValueError(f"{len(vectors)} vectors for {len(batch)} texts")
                out.extend([float(x) for x in v] for v in vectors)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise RagError(
                    f"Unexpected reply from {self.what} at {self.url}: {str(res)[:300]}"
                ) from exc
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def make_embedder(settings: RagSettings) -> EmbeddingClient:
    base = settings.embed_base_url.rstrip("/")
    backend = settings.embed_backend
    if backend == "ovms":
        return _HTTPEmbedder(f"{base}/v1/embeddings", settings.embed_model, "OVMS embeddings")
    if backend in ("lmstudio", "lm-studio", "openai"):
        return _HTTPEmbedder(f"{base}/embeddings", settings.embed_model, "LM Studio embeddings")
    return _HTTPEmbedder(f"{base}/api/embed", settings.embed_model, "Ollama embeddings", True)


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


def import_lancedb():
    try:
        import lancedb
    except ImportError as exc:
        raise RagError(missing_deps_message("lancedb")) from exc
    return lancedb


def preload() -> bool:
    """Import the native libraries (numpy, pyarrow, lancedb) now, on the calling thread.

    MUST run before the MCP stdio loop starts. On Windows that loop keeps a blocking ReadFile
    pending on stdin, and loading numpy's DLLs while it is pending stalls until the next byte
    arrives on stdin: the first search_docs call hung until the client gave up (pi: 60 s) and
    sent another request. Importing here, while nothing reads stdin, avoids that.
    Returns False when the optional `rag` extra is not installed (document tools then report it).
    """
    try:
        import lancedb  # noqa: F401  (pulls in numpy and pyarrow)
        import pyarrow  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def connect(settings: RagSettings):
    return import_lancedb().connect(settings.db_path.absolute().as_posix())


def table_names(db) -> list[str]:
    try:
        names = db.list_tables()
        return list(getattr(names, "tables", names))
    except AttributeError:  # older lancedb
        return list(db.table_names())


def _ingest_hint() -> str:
    return "Ingest PDFs first: `uv run --extra rag vivado-docs ingest <pdf or folder>`."


def _open_table(settings: RagSettings):
    if not settings.db_path.is_dir():
        raise RagError(f"Document database not found: {settings.db_path}. {_ingest_hint()}")
    db = connect(settings)
    names = table_names(db)
    if settings.table_name not in names:
        raise RagError(
            f"Table '{settings.table_name}' does not exist in {settings.db_path} "
            f"(tables: {', '.join(names) or 'none'}). {_ingest_hint()}"
        )
    return db.open_table(settings.table_name)


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _embedding_dim(table) -> int | None:
    try:
        return int(table.schema.field("embedding").type.list_size)
    except (KeyError, AttributeError):
        return None


def list_documents(settings: RagSettings) -> list[dict[str, object]]:
    """One entry per ingested source file: chunk count, page range, ingest time."""
    try:
        table = _open_table(settings)
    except RagError:
        if not settings.db_path.is_dir():
            return []
        raise
    total = table.count_rows()
    if total == 0:
        return []
    rows = (
        table.search()
        .select(["source_file", "page_number", "ingested_at"])
        .limit(total)
        .to_arrow()
        .to_pylist()
    )
    docs: dict[str, dict[str, Any]] = {}
    for row in rows:
        doc = docs.setdefault(
            row["source_file"],
            {"chunks": 0, "pages": set(), "ingested_at": row.get("ingested_at")},
        )
        doc["chunks"] += 1
        if row.get("page_number") is not None:
            doc["pages"].add(int(row["page_number"]))
    out = []
    for name in sorted(docs):
        doc = docs[name]
        pages = sorted(doc["pages"])
        ingested = doc["ingested_at"]
        out.append(
            {
                "source_file": name,
                "chunks": doc["chunks"],
                "pages_with_text": len(pages),
                "first_page": pages[0] if pages else None,
                "last_page": pages[-1] if pages else None,
                "ingested_at": ingested.isoformat() if hasattr(ingested, "isoformat") else None,
            }
        )
    return out


def search(
    settings: RagSettings,
    query: str,
    top_k: int | None = None,
    source_file: str | None = None,
    embedder: Embedder | None = None,
) -> dict[str, object]:
    """Hybrid search: vector + full-text merged by weighted reciprocal rank fusion."""
    query = (query or "").strip()
    if not query:
        raise RagError("query must not be empty.")
    k = settings.top_k if top_k is None or top_k <= 0 else int(top_k)
    k = min(k, MAX_TOP_K)

    table = _open_table(settings)
    if table.count_rows() == 0:
        raise RagError(f"The document database {settings.db_path} is empty. {_ingest_hint()}")

    where = f"source_file = {sql_str(source_file)}" if source_file else None
    vector = (embedder or make_embedder(settings).embed_query)(query)
    dim = _embedding_dim(table)
    if dim is not None and len(vector) != dim:
        raise RagError(
            f"The query embedding has {len(vector)} dimensions but the database stores "
            f"{dim}. The embedding model in {settings.config_path} "
            f"({settings.embed_model}) must be the one the documents were ingested with."
        )

    columns = ["chunk_id", "text", "source_file", "page_number"]
    vq = (
        table.search(vector, vector_column_name="embedding")
        .select([*columns, "_distance"])
        .limit(k * 2)
    )
    if where:
        vq = vq.where(where, prefilter=True)
    vector_rows = vq.to_list()

    fts_rows: list[dict[str, Any]] = []
    fts_note = None
    if settings.use_hybrid:
        try:
            fq = table.search(query, query_type="fts").select([*columns, "_score"]).limit(k * 2)
            if where:
                fq = fq.where(where, prefilter=True)
            fts_rows = fq.to_list()
        except Exception as exc:  # no FTS index yet, or a query syntax lancedb rejects
            fts_note = (
                "full-text search unavailable, used vector search only "
                f"(`vivado-docs reindex` rebuilds the index): {exc}"
            )

    weight = settings.fts_weight if fts_rows else 0.0
    scores: dict[str, float] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(vector_rows):
        cid = row["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + (1.0 - weight) / (rank + 1)
        by_id.setdefault(cid, {}).update(row)
    for rank, row in enumerate(fts_rows):
        cid = row["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + weight / (rank + 1)
        by_id.setdefault(cid, {}).update(row)

    results: list[dict[str, object]] = []
    seen_texts: set[str] = set()
    for cid, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        row = by_id[cid]
        if row["text"] in seen_texts:
            continue
        seen_texts.add(row["text"])
        results.append(
            {
                "rank": len(results) + 1,
                "source_file": row["source_file"],
                "page_number": row["page_number"],
                "score": round(score, 6),
                "vector_distance": row.get("_distance"),
                "text": row["text"],
            }
        )
        if len(results) >= k:
            break

    out: dict[str, object] = {
        "query": query,
        "source_file": source_file,
        "hybrid": settings.use_hybrid and fts_note is None,
        "results": results,
    }
    if fts_note:
        out["note"] = fts_note
    return out


def read_page(settings: RagSettings, source_file: str, page_number: int) -> dict[str, object]:
    """All stored chunks of one page, in stored order (neighbouring chunks overlap)."""
    if not source_file:
        raise RagError("source_file must not be empty.")
    table = _open_table(settings)
    where = f"source_file = {sql_str(source_file)} AND page_number = {int(page_number)}"
    rows = (
        table.search()
        .where(where)
        .select(["text", "source_file", "page_number"])
        .limit(10_000)
        .to_list()
    )
    if not rows:
        known = {doc["source_file"] for doc in list_documents(settings)}
        if source_file not in known:
            raise RagError(
                f"No document named {source_file!r}. Known documents: "
                f"{', '.join(sorted(known)) or 'none'}."
            )
        return {
            "source_file": source_file,
            "page_number": int(page_number),
            "chunks": [],
            "note": "No text stored for this page (it may be a diagram, blank, or out of range).",
        }
    return {
        "source_file": source_file,
        "page_number": int(page_number),
        "chunks": [row["text"] for row in rows],
    }
