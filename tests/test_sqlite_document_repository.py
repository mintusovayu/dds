"""
Тесты ``SqliteDocumentRepository`` — read-side доступа к документам.

Покрывают все методы Protocol ``IDocumentRepository`` на реальной
SQLite (через ``SQLiteAdapter``): поиск по хешу и пути, чтение
метаданных, полный SELECT, чтение текста страницы, а также
LSP-совместимость с Protocol и иммутабельность ``DocumentMetadata``.

Фаза 8, ADR-008.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from dds_core.domain.interfaces import IDocumentRepository
from dds_core.domain.models import Document, DocumentMetadata
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter
from dds_core.infrastructure.sqlite_document_repository import (
    SqliteDocumentRepository,
)

# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------

_ISO_TS = "2024-01-01T00:00:00+00:00"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def adapter(tmp_path: Path) -> Iterator[SQLiteAdapter]:
    """Свежий SQLite-адаптер с созданной схемой БД."""
    db = SQLiteAdapter(str(tmp_path / "test.db"))
    DatabaseManager(db).ensure_all()
    yield db
    db.close()


@pytest.fixture
def repository(adapter: SQLiteAdapter) -> SqliteDocumentRepository:
    """Репозиторий поверх свежего адаптера."""
    return SqliteDocumentRepository(adapter)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _insert_document(
    adapter: SQLiteAdapter,
    doc_id: str,
    file_path: str,
    file_hash: str,
    *,
    file_size: int = 1024,
    page_count: int = 1,
    cached_size: int | None = None,
    cached_mtime: str | None = None,
) -> None:
    """Вставляет запись в ``documents`` (для setup тестов)."""
    if cached_size is None:
        cached_size = file_size
    if cached_mtime is None:
        cached_mtime = _ISO_TS
    adapter.execute_write(
        "INSERT INTO documents "
        "(doc_id, file_path, file_hash, file_size, page_count, "
        " indexed_at, last_modified, cached_size, cached_mtime) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            doc_id,
            file_path,
            file_hash,
            file_size,
            page_count,
            _ISO_TS,
            _ISO_TS,
            cached_size,
            cached_mtime,
        ),
    )


def _insert_page(
    adapter: SQLiteAdapter,
    doc_id: str,
    page_number: int,
    text: str,
) -> None:
    """Вставляет страницу в ``text_index_fts`` (для setup тестов)."""
    adapter.execute_write(
        "INSERT INTO text_index_fts "
        "(doc_id, page_number, text_content, normalized_text) "
        "VALUES (?, ?, ?, ?)",
        (doc_id, page_number, text, text.lower()),
    )


# ----------------------------------------------------------------------
# get_by_hash
# ----------------------------------------------------------------------


def test_get_by_hash_returns_doc_id(
    adapter: SQLiteAdapter,
    repository: SqliteDocumentRepository,
) -> None:
    """Поиск по существующему хешу возвращает doc_id."""
    _insert_document(adapter, "doc-1", "path/1.pdf", "hash-1")
    assert repository.get_by_hash("hash-1") == "doc-1"


def test_get_by_hash_returns_none_for_unknown(
    repository: SqliteDocumentRepository,
) -> None:
    """Поиск по отсутствующему хешу возвращает None."""
    assert repository.get_by_hash("nonexistent") is None


# ----------------------------------------------------------------------
# get_by_path
# ----------------------------------------------------------------------


def test_get_by_path_returns_doc_id(
    adapter: SQLiteAdapter,
    repository: SqliteDocumentRepository,
) -> None:
    """Поиск по существующему пути возвращает doc_id."""
    _insert_document(adapter, "doc-1", "раздел_01/чертёж.pdf", "hash-1")
    assert repository.get_by_path("раздел_01/чертёж.pdf") == "doc-1"


def test_get_by_path_returns_none_for_unknown(
    repository: SqliteDocumentRepository,
) -> None:
    """Поиск по отсутствующему пути возвращает None."""
    assert repository.get_by_path("nonexistent.pdf") is None


# ----------------------------------------------------------------------
# get_metadata
# ----------------------------------------------------------------------


def test_get_metadata_returns_document_metadata(
    adapter: SQLiteAdapter,
    repository: SqliteDocumentRepository,
) -> None:
    """get_metadata возвращает DocumentMetadata с полными полями."""
    _insert_document(
        adapter,
        "doc-1",
        "path/1.pdf",
        "hash-1",
        file_size=2048,
        cached_size=2048,
        cached_mtime="2024-06-15T10:30:00+00:00",
    )
    metadata = repository.get_metadata("path/1.pdf")
    assert metadata is not None
    assert isinstance(metadata, DocumentMetadata)
    assert metadata.doc_id == "doc-1"
    assert metadata.file_hash == "hash-1"
    assert metadata.cached_size == 2048
    assert metadata.cached_mtime == "2024-06-15T10:30:00+00:00"


def test_get_metadata_returns_none_for_unknown(
    repository: SqliteDocumentRepository,
) -> None:
    """get_metadata для отсутствующего пути возвращает None."""
    assert repository.get_metadata("nonexistent.pdf") is None


def test_document_metadata_is_frozen() -> None:
    """DocumentMetadata — frozen dataclass."""
    metadata = DocumentMetadata(
        doc_id="d",
        file_hash="h",
        cached_size=1,
        cached_mtime="t",
    )
    with pytest.raises(FrozenInstanceError):
        metadata.doc_id = "other"  # type: ignore[misc]


# ----------------------------------------------------------------------
# get_by_id
# ----------------------------------------------------------------------


def test_get_by_id_returns_document(
    adapter: SQLiteAdapter,
    repository: SqliteDocumentRepository,
) -> None:
    """get_by_id возвращает Document с полными полями."""
    _insert_document(
        adapter,
        "doc-1",
        "path/1.pdf",
        "hash-1",
        file_size=4096,
        page_count=7,
    )
    document = repository.get_by_id("doc-1")
    assert document is not None
    assert isinstance(document, Document)
    assert document.doc_id == "doc-1"
    assert document.file_path == "path/1.pdf"
    assert document.file_hash == "hash-1"
    assert document.file_size == 4096
    assert document.page_count == 7
    assert document.indexed_at == _ISO_TS
    assert document.last_modified == _ISO_TS


def test_get_by_id_returns_none_for_unknown(
    repository: SqliteDocumentRepository,
) -> None:
    """get_by_id для отсутствующего doc_id возвращает None."""
    assert repository.get_by_id("nonexistent") is None


# ----------------------------------------------------------------------
# load_all
# ----------------------------------------------------------------------


def test_load_all_returns_empty_dict_for_empty_table(
    repository: SqliteDocumentRepository,
) -> None:
    """load_all для пустой таблицы возвращает пустой словарь."""
    assert repository.load_all() == {}


def test_load_all_keys_by_file_path(
    adapter: SQLiteAdapter,
    repository: SqliteDocumentRepository,
) -> None:
    """load_all использует file_path как ключ словаря."""
    _insert_document(adapter, "doc-1", "path/1.pdf", "hash-1")
    _insert_document(adapter, "doc-2", "path/2.pdf", "hash-2")
    result = repository.load_all()
    assert set(result.keys()) == {"path/1.pdf", "path/2.pdf"}


def test_load_all_returns_all_documents(
    adapter: SQLiteAdapter,
    repository: SqliteDocumentRepository,
) -> None:
    """load_all возвращает все записи с корректными полями."""
    _insert_document(
        adapter,
        "doc-1",
        "path/1.pdf",
        "hash-1",
        cached_size=100,
        cached_mtime="2024-01-01T00:00:00+00:00",
    )
    _insert_document(
        adapter,
        "doc-2",
        "path/2.pdf",
        "hash-2",
        cached_size=200,
        cached_mtime="2024-02-01T00:00:00+00:00",
    )
    result = repository.load_all()

    assert result["path/1.pdf"].doc_id == "doc-1"
    assert result["path/1.pdf"].file_hash == "hash-1"
    assert result["path/1.pdf"].cached_size == 100
    assert result["path/1.pdf"].cached_mtime == "2024-01-01T00:00:00+00:00"

    assert result["path/2.pdf"].doc_id == "doc-2"
    assert result["path/2.pdf"].file_hash == "hash-2"
    assert result["path/2.pdf"].cached_size == 200
    assert result["path/2.pdf"].cached_mtime == "2024-02-01T00:00:00+00:00"


# ----------------------------------------------------------------------
# get_page_text
# ----------------------------------------------------------------------


def test_get_page_text_returns_content(
    adapter: SQLiteAdapter,
    repository: SqliteDocumentRepository,
) -> None:
    """get_page_text возвращает текст существующей страницы."""
    _insert_document(adapter, "doc-1", "path/1.pdf", "hash-1")
    _insert_page(adapter, "doc-1", 0, "hello world")
    _insert_page(adapter, "doc-1", 1, "second page")

    assert repository.get_page_text("doc-1", 0) == "hello world"
    assert repository.get_page_text("doc-1", 1) == "second page"


def test_get_page_text_returns_empty_for_unknown_page(
    adapter: SQLiteAdapter,
    repository: SqliteDocumentRepository,
) -> None:
    """get_page_text для отсутствующей страницы возвращает пустую строку."""
    _insert_document(adapter, "doc-1", "path/1.pdf", "hash-1")
    _insert_page(adapter, "doc-1", 0, "hello")
    assert repository.get_page_text("doc-1", 99) == ""


def test_get_page_text_returns_empty_for_unknown_doc(
    repository: SqliteDocumentRepository,
) -> None:
    """get_page_text для отсутствующего doc_id возвращает пустую строку."""
    assert repository.get_page_text("nonexistent", 0) == ""


# ----------------------------------------------------------------------
# LSP
# ----------------------------------------------------------------------


def test_repository_is_lsp_compliant(
    repository: SqliteDocumentRepository,
) -> None:
    """SqliteDocumentRepository реализует IDocumentRepository."""
    assert isinstance(repository, IDocumentRepository)
