"""Document-search tools over a LanceDB (built here with tiny fake embeddings)."""

import asyncio
import datetime
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

lancedb = pytest.importorskip("lancedb")
pa = pytest.importorskip("pyarrow")
pytest.importorskip("yaml")

from vivado_mcp import rag_docs, server  # noqa: E402

DIM = 4
# Each "topic" maps to a unit vector; the fake embedder maps query words onto them.
TOPICS = {"clock": [1, 0, 0, 0], "jumper": [0, 1, 0, 0], "ddr": [0, 0, 1, 0], "led": [0, 0, 0, 1]}

ROWS = [
    ("zed.pdf", 12, "The 100 MHz clock oscillator drives pin Y9 (GCLK).", "clock"),
    ("zed.pdf", 12, "Clock pin Y9 uses bank 13, LVCMOS33.", "clock"),
    ("zed.pdf", 20, "Boot mode jumpers JP7-JP11 select JTAG or SD boot.", "jumper"),
    ("zed.pdf", 30, "DDR3 memory is connected to the PS DDR controller.", "ddr"),
    ("other's.pdf", 3, "User LED LD0 is on pin T22.", "led"),
]


def fake_embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    for word, topic in TOPICS.items():
        if word in text.lower():
            vec = [a + b for a, b in zip(vec, topic, strict=True)]
    return vec if any(vec) else [0.25] * DIM


def _build_db(root, fts=True):
    db = lancedb.connect((root / "database" / "db").as_posix())
    schema = pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("embedding", pa.list_(pa.float32(), DIM)),
            pa.field("source_file", pa.string()),
            pa.field("page_number", pa.int32()),
            pa.field("doc_type", pa.string()),
            pa.field("ingested_at", pa.timestamp("us", tz="UTC")),
        ]
    )
    table = db.create_table("documents", schema=schema)
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    table.add(
        [
            {
                "chunk_id": f"c{i}",
                "text": text,
                "embedding": TOPICS[topic],
                "source_file": src,
                "page_number": page,
                "doc_type": "other",
                "ingested_at": now,
            }
            for i, (src, page, text, topic) in enumerate(ROWS)
        ]
    )
    if fts:
        table.create_fts_index("text", replace=True)
    return table


def _write_config(root, embedding_block: str = "", db_path: str = "database/db"):
    emb = embedding_block or (
        "embedding:\n  backend: ovms\n  model: fake\n  base_url: http://127.0.0.1:9\n"
    )
    (root / "config.yaml").write_text(
        emb + f"retrieval:\n  top_k: 3\n  use_hybrid: true\n  fts_weight: 0.3\n"
        f"database:\n  path: {db_path}\n  table_name: documents\n",
        encoding="utf-8",
    )
    return root / "config.yaml"


@pytest.fixture
def kb(tmp_path):
    _build_db(tmp_path)
    return rag_docs.load_settings(_write_config(tmp_path))


def test_missing_config_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv(rag_docs.CONFIG_ENV, str(tmp_path / "nope.yaml"))
    out = asyncio.run(server.search_docs("clock"))
    assert out["status"] == "error" and "config not found" in out["error"]


def test_settings_resolve_relative_db_and_accept_folder(tmp_path):
    _write_config(tmp_path)
    s = rag_docs.load_settings(tmp_path)  # folder instead of file
    assert s.db_path == tmp_path / "database" / "db"
    assert (s.top_k, s.use_hybrid, s.fts_weight) == (3, True, 0.3)
    assert s.embed_backend == "ovms" and s.embed_base_url == "http://127.0.0.1:9"


def test_unsupported_backend_and_missing_model_are_rejected(tmp_path):
    _write_config(tmp_path, "embedding:\n  backend: google\n  model: x\n")
    with pytest.raises(rag_docs.RagError, match="not supported"):
        rag_docs.load_settings(tmp_path)
    _write_config(tmp_path, "embedding:\n  backend: ovms\n")
    with pytest.raises(rag_docs.RagError, match="model is not set"):
        rag_docs.load_settings(tmp_path)


def test_list_documents(kb):
    docs = {d["source_file"]: d for d in rag_docs.list_documents(kb)}
    assert docs["zed.pdf"]["chunks"] == 4
    assert (docs["zed.pdf"]["first_page"], docs["zed.pdf"]["last_page"]) == (12, 30)
    assert docs["other's.pdf"]["chunks"] == 1


def test_search_ranks_by_meaning_and_cites_pages(kb):
    out = rag_docs.search(kb, "Which clock pin?", embedder=fake_embed)
    assert out["hybrid"] is True
    first = out["results"][0]
    assert first["source_file"] == "zed.pdf" and first["page_number"] == 12
    assert "Y9" in first["text"]
    assert len(out["results"]) <= 3


def test_search_source_filter_with_quote(kb):
    out = rag_docs.search(kb, "clock led", source_file="other's.pdf", embedder=fake_embed)
    assert {r["source_file"] for r in out["results"]} == {"other's.pdf"}


def test_search_dimension_mismatch_is_explained(kb):
    with pytest.raises(rag_docs.RagError, match="dimensions"):
        rag_docs.search(kb, "clock", embedder=lambda q: [0.0] * 768)


def test_search_without_fts_index_falls_back(tmp_path):
    _build_db(tmp_path, fts=False)
    s = rag_docs.load_settings(_write_config(tmp_path))
    out = rag_docs.search(s, "jumper settings", embedder=fake_embed)
    assert out["hybrid"] is False and "reindex" in out["note"]
    assert out["results"][0]["page_number"] == 20


def test_read_page(kb):
    page = rag_docs.read_page(kb, "zed.pdf", 12)
    assert len(page["chunks"]) == 2
    empty = rag_docs.read_page(kb, "zed.pdf", 99)
    assert empty["chunks"] == [] and "note" in empty
    with pytest.raises(rag_docs.RagError, match="Known documents"):
        rag_docs.read_page(kb, "nope.pdf", 1)


def test_empty_table_and_missing_db(tmp_path):
    db = lancedb.connect((tmp_path / "database" / "db").as_posix())
    db.create_table(
        "documents",
        schema=pa.schema([pa.field("chunk_id", pa.string()), pa.field("text", pa.string())]),
    )
    s = rag_docs.load_settings(_write_config(tmp_path))
    assert rag_docs.list_documents(s) == []
    with pytest.raises(rag_docs.RagError, match="empty"):
        rag_docs.search(s, "clock", embedder=fake_embed)
    s2 = rag_docs.load_settings(_write_config(tmp_path, db_path="nowhere"))
    assert rag_docs.list_documents(s2) == []
    with pytest.raises(rag_docs.RagError, match="not found"):
        rag_docs.search(s2, "clock", embedder=fake_embed)


def test_shipped_config_is_the_default(monkeypatch):
    monkeypatch.delenv(rag_docs.CONFIG_ENV, raising=False)
    s = rag_docs.load_settings()
    assert s.config_path == rag_docs.default_config_path()
    assert s.config_path.parent.name == "rag" and s.db_path == s.config_path.parent / "db"
    assert s.embed_backend == "ovms" and s.chunk_size == 512


class _FakeOpenAI(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, body))
        data = {"data": [{"embedding": fake_embed(body["input"][0])}]}
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def test_tools_end_to_end_with_ovms_style_endpoint(tmp_path, monkeypatch):
    httpd = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    httpd.seen = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _build_db(tmp_path)
        cfg = _write_config(
            tmp_path,
            "embedding:\n  backend: ovms\n  model: bge\n"
            f"  base_url: http://127.0.0.1:{httpd.server_port}\n",
        )
        monkeypatch.setenv(rag_docs.CONFIG_ENV, str(cfg))

        listing = asyncio.run(server.list_docs())
        assert listing["status"] == "ok" and len(listing["documents"]) == 2
        assert listing["embedding"]["model"] == "bge"

        out = asyncio.run(server.search_docs("DDR memory", top_k=1))
        assert out["status"] == "ok" and out["results"][0]["page_number"] == 30
        assert len(out["results"]) == 1
        assert httpd.seen[0] == ("/v1/embeddings", {"model": "bge", "input": ["DDR memory"]})

        page = asyncio.run(server.read_doc_page("zed.pdf", 20))
        assert page["status"] == "ok" and "JP7" in page["chunks"][0]
    finally:
        httpd.shutdown()


def test_unreachable_embedding_server_is_reported(kb):
    with pytest.raises(rag_docs.RagError, match="Cannot reach OVMS"):
        rag_docs.search(kb, "clock")


def test_localhost_is_contacted_over_ipv4_with_fallback():
    emb = rag_docs._HTTPEmbedder("http://localhost:8000/v1/embeddings", "m", "OVMS embeddings")
    assert emb.url == "http://127.0.0.1:8000/v1/embeddings"
    assert emb._fallback == "http://localhost:8000/v1/embeddings"
    other = rag_docs._HTTPEmbedder("http://10.0.0.5:8000/v1/embeddings", "m", "OVMS embeddings")
    assert other.url == "http://10.0.0.5:8000/v1/embeddings" and other._fallback is None


def test_preload_imports_native_libraries():
    import sys

    assert rag_docs.preload() is True
    assert {"lancedb", "pyarrow", "numpy"} <= set(sys.modules)
