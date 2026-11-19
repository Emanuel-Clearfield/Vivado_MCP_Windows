"""Build the document knowledge base: PDF -> page text -> overlapping chunks -> embeddings.

CLI (`vivado-docs`, or `python -m vivado_mcp.rag_ingest`):

    vivado-docs ingest rag/sources          # every *.pdf in the folder (or one file)
    vivado-docs list
    vivado-docs remove ZedBoard_HW_UG_v2_2.pdf
    vivado-docs reindex                     # rebuild the full-text index
    vivado-docs search "GCLK pin" -k 5      # same search as the search_docs MCP tool
    vivado-docs page ZedBoard_HW_UG_v2_2.pdf 25

Uses the same config as the MCP tools (rag/config.yaml or $VIVADO_MCP_RAG_CONFIG).
Already-ingested files (by file name) are skipped unless --force.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
import textwrap
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vivado_mcp import rag_docs
from vivado_mcp.rag_docs import RagError, RagSettings

_MIN_CHARS = 100  # pages with less text are usually diagrams or blank
_SEPARATORS = ["\n\n", "\n", " ", ""]


@dataclass
class Chunk:
    text: str
    source_file: str
    page_number: int


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------


def _strip_header_footer(lines: list[str]) -> list[str]:
    """Drop up to two short (< 80 chars) lines at the top and bottom of a page."""
    if len(lines) <= 4:
        return lines
    start = 0
    while start < 2 and start < len(lines) and len(lines[start].strip()) < 80:
        start += 1
    end = len(lines)
    while end > len(lines) - 2 and end > start and len(lines[end - 1].strip()) < 80:
        end -= 1
    return lines[start:end]


def parse_pdf(path: Path) -> list[tuple[int, str]]:
    """(page_number, text) for every page with enough text. Page numbers start at 1."""
    try:
        import pymupdf
    except ImportError as exc:
        raise RagError(rag_docs.missing_deps_message("pymupdf")) from exc
    pages: list[tuple[int, str]] = []
    with pymupdf.open(str(path)) as doc:
        for index, page in enumerate(doc):
            raw = page.get_text("text")
            if len(raw.strip()) < _MIN_CHARS:
                continue
            text = "\n".join(_strip_header_footer(raw.splitlines())).strip()
            if len(text) >= _MIN_CHARS:
                pages.append((index + 1, text))
    return pages


# ---------------------------------------------------------------------------
# Chunking: recursive character splitter that prefers paragraph, line, word boundaries
# ---------------------------------------------------------------------------


def split_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    result: list[str] = []
    _recursive_split(text, chunk_size, chunk_overlap, _SEPARATORS, result)
    return result


def _recursive_split(
    text: str, size: int, overlap: int, separators: list[str], result: list[str]
) -> None:
    if len(text) <= size:
        if text.strip():
            result.append(text.strip())
        return

    separator, remaining = "", []
    for i, sep in enumerate(separators):
        if sep and sep in text:
            separator, remaining = sep, separators[i + 1 :]
            break
    parts = text.split(separator) if separator else list(text)

    current: list[str] = []
    current_len = 0
    for part in parts:
        if current_len + len(part) + (len(separator) if current else 0) > size:
            if current:
                merged = separator.join(current).strip()
                if merged:
                    if len(merged) > size and remaining:
                        _recursive_split(merged, size, overlap, remaining, result)
                    else:
                        result.append(merged)
                # keep the tail of the buffer as overlap with the next chunk
                tail: list[str] = []
                tail_len = 0
                for p in reversed(current):
                    if tail_len + len(p) > overlap:
                        break
                    tail.insert(0, p)
                    tail_len += len(p)
                current, current_len = tail, tail_len
            if len(part) > size and remaining:
                _recursive_split(part, size, overlap, remaining, result)
                current, current_len = [], 0
                continue
        current.append(part)
        current_len += len(part)

    if current:
        merged = separator.join(current).strip()
        if merged:
            result.append(merged)


def chunk_pdf(path: Path, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    return [
        Chunk(text=t, source_file=path.name, page_number=page)
        for page, page_text in parse_pdf(path)
        for t in split_text(page_text, chunk_size, chunk_overlap)
        if t.strip()
    ]


def chunk_id(chunk: Chunk) -> str:
    payload = f"{chunk.source_file}:{chunk.page_number}:{chunk.text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------


def _schema(dim: int):
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("embedding", pa.list_(pa.float32(), dim)),
            pa.field("source_file", pa.string()),
            pa.field("page_number", pa.int32()),
            pa.field("doc_type", pa.string()),
            pa.field("ingested_at", pa.timestamp("us", tz="UTC")),
        ]
    )


def _open_or_create(settings: RagSettings, dim: int):
    settings.db_path.mkdir(parents=True, exist_ok=True)
    db = rag_docs.connect(settings)
    if settings.table_name in rag_docs.table_names(db):
        table = db.open_table(settings.table_name)
        existing = table.schema.field("embedding").type.list_size
        if existing != dim:
            raise RagError(
                f"The embedding model returns {dim} dimensions but {settings.db_path} stores "
                f"{existing}. Use the model the database was built with, or delete "
                f"{settings.db_path} and ingest everything again."
            )
        return table
    return db.create_table(settings.table_name, schema=_schema(dim))


def reindex(settings: RagSettings) -> None:
    """(Re)build the full-text index on the chunk text."""
    db = rag_docs.connect(settings)
    if settings.table_name not in rag_docs.table_names(db):
        return
    table = db.open_table(settings.table_name)
    if table.count_rows() == 0:
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        table.create_fts_index("text", replace=True)


def remove_document(settings: RagSettings, source_file: str) -> bool:
    known = {d["source_file"] for d in rag_docs.list_documents(settings)}
    if source_file not in known:
        return False
    table = rag_docs.connect(settings).open_table(settings.table_name)
    table.delete(f"source_file = {rag_docs.sql_str(source_file)}")
    return True


def ingest(
    settings: RagSettings,
    paths: list[Path],
    force: bool = False,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Ingest PDFs (files or folders of *.pdf). Returns the number of chunks added."""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.glob("*.pdf")))
        elif p.is_file():
            files.append(p)
        else:
            raise RagError(f"Not found: {p}")
    if not files:
        log("No PDF files found.")
        return 0

    if embed is None:
        embed = rag_docs.make_embedder(settings).embed_documents
    known = {d["source_file"] for d in rag_docs.list_documents(settings)}
    total = 0
    for pdf in files:
        if pdf.name in known and not force:
            log(f"Skipped {pdf.name} (already ingested; --force to replace).")
            continue
        chunks = chunk_pdf(pdf, settings.chunk_size, settings.chunk_overlap)
        if not chunks:
            log(f"Skipped {pdf.name} (no extractable text; scanned PDF?).")
            continue
        log(f"Embedding {pdf.name}: {len(chunks)} chunks ...")
        vectors = embed([c.text for c in chunks])
        table = _open_or_create(settings, len(vectors[0]))
        if pdf.name in known:
            table.delete(f"source_file = {rag_docs.sql_str(pdf.name)}")
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        rows = [
            {
                "chunk_id": chunk_id(c),
                "text": c.text,
                "embedding": v,
                "source_file": c.source_file,
                "page_number": c.page_number,
                "doc_type": "pdf",
                "ingested_at": now,
            }
            for c, v in zip(chunks, vectors, strict=True)
        ]
        table.merge_insert("chunk_id").when_not_matched_insert_all().execute(rows)
        log(f"Ingested {pdf.name}: {len(rows)} chunks.")
        total += len(rows)
    if total:
        reindex(settings)
        log(f"Added {total} chunks; full-text index rebuilt.")
    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vivado-docs", description="Manage the document knowledge base used by search_docs."
    )
    parser.add_argument("--config", help=f"config.yaml (default: ${rag_docs.CONFIG_ENV} or rag/)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ing = sub.add_parser("ingest", help="add PDFs (files or folders)")
    p_ing.add_argument("paths", nargs="+", type=Path)
    p_ing.add_argument("--force", action="store_true", help="re-ingest files already present")
    sub.add_parser("list", help="list ingested documents")
    p_rm = sub.add_parser("remove", help="remove a document by file name")
    p_rm.add_argument("source_file")
    sub.add_parser("reindex", help="rebuild the full-text index")
    sub.add_parser("config", help="show the configuration in use")
    p_s = sub.add_parser("search", help="search the documents (same as the search_docs tool)")
    p_s.add_argument("query")
    p_s.add_argument("-k", "--top-k", type=int, default=0, help="number of chunks")
    p_s.add_argument("-s", "--source", help="only this document (file name)")
    p_s.add_argument("--json", action="store_true", help="print the raw JSON result")
    p_pg = sub.add_parser("page", help="print all stored text of one page")
    p_pg.add_argument("source_file")
    p_pg.add_argument("page_number", type=int)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # PDF text has Unicode; Windows consoles are cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    try:
        settings = rag_docs.load_settings(args.config)
        if args.cmd == "ingest":
            ingest(settings, args.paths, force=args.force)
        elif args.cmd == "list":
            docs = rag_docs.list_documents(settings)
            if not docs:
                print(f"No documents in {settings.db_path}.")
            for d in docs:
                print(
                    f"{d['source_file']} | {d['chunks']} chunks | pages "
                    f"{d['first_page']}-{d['last_page']} | {str(d['ingested_at'])[:19]}"
                )
        elif args.cmd == "remove":
            if not remove_document(settings, args.source_file):
                print(f"'{args.source_file}' is not in the database.", file=sys.stderr)
                return 1
            reindex(settings)
            print(f"Removed {args.source_file}.")
        elif args.cmd == "reindex":
            reindex(settings)
            print("Full-text index rebuilt.")
        elif args.cmd == "config":
            print(json.dumps(settings.describe(), indent=2))
        elif args.cmd == "search":
            out = rag_docs.search(settings, args.query, args.top_k or None, args.source)
            if args.json:
                print(json.dumps(out, indent=2, default=str))
            else:
                if out.get("note"):
                    print(f"note: {out['note']}\n")
                if not out["results"]:
                    print("No matches.")
                for r in out["results"]:
                    print(
                        f"[{r['rank']}] {r['source_file']} p.{r['page_number']} "
                        f"(score {r['score']})"
                    )
                    print(textwrap.indent(r["text"].strip(), "    "))
                    print()
        elif args.cmd == "page":
            out = rag_docs.read_page(settings, args.source_file, args.page_number)
            if out.get("note"):
                print(out["note"])
            print("\n\n".join(out["chunks"]))
    except RagError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
