"""PDF ingestion (vivado-docs) followed by search through the MCP-side reader."""

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("yaml")
fitz = pytest.importorskip("pymupdf")

from vivado_mcp import rag_docs, rag_ingest  # noqa: E402

FILLER = " This sentence is filler text so that the page passes the minimum length check."
PAGES = [
    "Clock sources. The 100 MHz oscillator drives GCLK on pin Y9." + FILLER * 8,
    "x",  # too short: treated as a diagram page and skipped
    "Configuration jumpers JP7 to JP11 select the boot mode." + FILLER * 8,
]
WORDS = ["clock", "jumper", "ddr"]


def fake_embed_docs(texts):
    return [[float(w in t.lower()) + 0.01 for w in WORDS] for t in texts]


def fake_embed_query(text):
    return fake_embed_docs([text])[0]


def _make_pdf(path, pages):
    doc = fitz.open()
    for number, text in enumerate(pages, start=1):
        page = doc.new_page()
        # short header/footer lines are stripped by the parser; small font = long body lines
        body = "\n".join(["Board Guide v1", text, f"Page {number}"])
        page.insert_textbox(fitz.Rect(30, 30, 580, 800), body, fontsize=7)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "sources").mkdir()
    pdf = tmp_path / "sources" / "board.pdf"
    _make_pdf(pdf, PAGES)
    (tmp_path / "config.yaml").write_text(
        "embedding:\n  backend: ovms\n  model: m\n"
        "  base_url: http://127.0.0.1:9\nchunking:\n  chunk_size: 120\n  chunk_overlap: 20\n"
        "database:\n  path: db\n",
        encoding="utf-8",
    )
    return rag_docs.load_settings(tmp_path / "config.yaml"), pdf


def test_split_text_respects_size_and_overlap():
    text = " ".join(f"word{i}" for i in range(200))
    chunks = rag_ingest.split_text(text, 100, 20)
    assert all(len(c) <= 100 for c in chunks)
    assert len(chunks) > 5
    # consecutive chunks share their boundary words
    assert chunks[0].split()[-1] in chunks[1]


def test_ingest_then_search_and_read(setup):
    settings, pdf = setup
    logs = []
    added = rag_ingest.ingest(settings, [pdf.parent], embed=fake_embed_docs, log=logs.append)
    assert added > 0 and any("full-text index rebuilt" in m for m in logs)

    docs = rag_docs.list_documents(settings)
    assert [d["source_file"] for d in docs] == ["board.pdf"]
    assert (docs[0]["first_page"], docs[0]["last_page"], docs[0]["pages_with_text"]) == (1, 3, 2)

    out = rag_docs.search(settings, "jumper", embedder=fake_embed_query)
    assert out["hybrid"] is True
    assert out["results"][0]["page_number"] == 3 and "JP7" in out["results"][0]["text"]

    page = " ".join(rag_docs.read_page(settings, "board.pdf", 1)["chunks"])
    assert "Y9" in page and "Board Guide" not in page and "Page 1" not in page


def test_reingest_skips_unless_forced(setup):
    settings, pdf = setup
    first = rag_ingest.ingest(settings, [pdf], embed=fake_embed_docs, log=lambda m: None)
    assert rag_ingest.ingest(settings, [pdf], embed=fake_embed_docs, log=lambda m: None) == 0
    again = rag_ingest.ingest(
        settings, [pdf], force=True, embed=fake_embed_docs, log=lambda m: None
    )
    assert again == first
    assert rag_docs.list_documents(settings)[0]["chunks"] == first


def test_dimension_change_is_refused(setup):
    settings, pdf = setup
    rag_ingest.ingest(settings, [pdf], embed=fake_embed_docs, log=lambda m: None)
    _make_pdf(pdf.parent / "other.pdf", PAGES[:1])
    with pytest.raises(rag_docs.RagError, match="dimensions"):
        rag_ingest.ingest(
            settings, [pdf.parent / "other.pdf"], embed=lambda t: [[0.0] * 8 for _ in t],
            log=lambda m: None,
        )


def test_cli_list_and_remove(setup, capsys):
    settings, pdf = setup
    rag_ingest.ingest(settings, [pdf], embed=fake_embed_docs, log=lambda m: None)
    cfg = str(settings.config_path)
    assert rag_ingest.main(["--config", cfg, "list"]) == 0
    assert "board.pdf" in capsys.readouterr().out
    assert rag_ingest.main(["--config", cfg, "remove", "board.pdf"]) == 0
    assert rag_docs.list_documents(settings) == []
    assert rag_ingest.main(["--config", cfg, "remove", "board.pdf"]) == 1


def test_cli_search_and_page(setup, capsys, monkeypatch):
    settings, pdf = setup
    rag_ingest.ingest(settings, [pdf], embed=fake_embed_docs, log=lambda m: None)
    class FakeEmbedder:
        embed_query = staticmethod(fake_embed_query)

    monkeypatch.setattr(rag_docs, "make_embedder", lambda s: FakeEmbedder())
    cfg = str(settings.config_path)
    assert rag_ingest.main(["--config", cfg, "search", "jumper", "-k", "2"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[1] board.pdf p.3") and "JP7" in out and "[3]" not in out
    assert rag_ingest.main(["--config", cfg, "search", "jumper", "--json"]) == 0
    assert '"page_number": 3' in capsys.readouterr().out
    assert rag_ingest.main(["--config", cfg, "page", "board.pdf", "1"]) == 0
    assert "Y9" in capsys.readouterr().out
    assert rag_ingest.main(["--config", cfg, "page", "nope.pdf", "1"]) == 1


def test_cli_reports_unreachable_embedding_server(setup, capsys):
    settings, pdf = setup
    assert rag_ingest.main(["--config", str(settings.config_path), "ingest", str(pdf)]) == 1
    assert "Cannot reach OVMS" in capsys.readouterr().err
