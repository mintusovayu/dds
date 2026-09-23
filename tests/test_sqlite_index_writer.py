"""
Тесты записи плана индексации через ``SqliteIndexWriter``.

Назначение
----------
Проверка контракта
:class:`~dds_core.infrastructure.sqlite_index_writer.SqliteIndexWriter`:
преобразование доменного ``DocumentIndexPlan`` в SQL-запросы и
их выполнение через ``IDatabase``.

Модуль введён в Фазе 5 (DocumentIndexPlan). Заменяет часть
покрытия, ранее относившегося к ``query_builder.build_document_queries``
и ``TextIndexer.write_documents_batch``.

Проверяемые сценарии
--------------------

+-------------------------------------+--------------------------------+
| Группа                              | Что проверяется                |
+=====================================+================================+
| ``write_plan``: успех               | Документ и страницы записаны   |
|                                     | в БД.                          |
+-------------------------------------+--------------------------------+
| ``write_plan``: идемпотентность     | Повторная запись того же       |
|                                     | ``doc_id`` перезаписывает      |
|                                     | существующую запись.           |
+-------------------------------------+--------------------------------+
| ``write_plan``: перезапись          | Старые страницы удаляются      |
|                                     | перед вставкой новых.          |
+-------------------------------------+--------------------------------+
| ``write_plans_batch``: успех        | N валидных → ``(N, 0)``.       |
+-------------------------------------+--------------------------------+
| ``write_plans_batch``: пусто        | ``[]`` → ``(0, 0)``.           |
+-------------------------------------+--------------------------------+
| ``write_plans_batch``: SAVEPOINT    | Битый план не ломает           |
|                                     | остальные (изоляция).          |
+-------------------------------------+--------------------------------+
| ``write_plans_batch``:              | ``commit_interval=1`` —        |
| ``commit_interval``                 | коммит после каждого плана.    |
+-------------------------------------+--------------------------------+
| ``write_plans_batch``:              | ``commit_interval`` больше     |
| ``commit_interval`` > batch         | длины — единственный финальный |
|                                     | коммит.                        |
+-------------------------------------+--------------------------------+
| ``remove_document``: успех          | Удаление документа и его       |
|                                     | страниц.                       |
+-------------------------------------+--------------------------------+
| ``remove_document``: идемпотентность| Удаление несуществующего —     |
|                                     | no-op.                         |
+-------------------------------------+--------------------------------+
| ``_plan_to_queries``: структура     | Порядок и количество SQL-      |
|                                     | запросов.                      |
+-------------------------------------+--------------------------------+
| LSP                                 | ``isinstance(writer,           |
|                                     | IIndexWriter)``.               |
+-------------------------------------+--------------------------------+
| Полный цикл                         | ``write_plan`` →               |
|                                     | ``remove_document`` →          |
|                                     | ``write_plan``.                |
+-------------------------------------+--------------------------------+

Стратегия тестирования
----------------------
- **Реальная SQLite на ``tmp_path``.** Предмет теста — SQL и
  SAVEPOINT; мок ``IDatabase`` не дал бы проверки фактического
  состояния БД. Согласовано с ``test_sqlite_savepoint.py``.
- **Реальная схема через ``DatabaseManager.ensure_all``.** Тест
  не расходится с production-структурой.
- **«Битый план» через ``file_hash=None``.** Нарушает ``NOT NULL``
  на ``documents.file_hash`` → ``sqlite3.IntegrityError``. Это
  локализованный способ проверить SAVEPOINT-изоляцию без
  monkeypatch'а. ``# type: ignore[arg-type]`` оправдан: тест
  намеренно передаёт значение, недопустимое с точки зрения
  типа, но достижимое на уровне данных.
- **Проверки через ``adapter.execute``.** Реальные SELECT'ы
  подтверждают фактическое состояние БД.

Границы
-------
- **``SQLiteAdapter.execute_write_batched_documents``** — уже
  покрыт в ``test_sqlite_savepoint.py``; здесь только делегирование.
- **``SqliteIndexWriter._plan_to_queries``** — покрыт косвенно
  через ``write_plan`` + один структурный тест.
- **Фатальные ошибки транзакции** (закрытая БД) — не в области.

Запуск
------
::

    pytest tests/test_sqlite_index_writer.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет production-файлы (все ресурсы — в tmp);
    - каждый тест изолирован (function-scoped fixtures);
    - тесты детерминированы: одинаковый вход → одинаковый результат.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from dds_core.domain.index_plan import DocumentIndexPlan, PageRecord
from dds_core.domain.index_writer import IIndexWriter
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter
from dds_core.infrastructure.sqlite_index_writer import SqliteIndexWriter

# =====================================================================
# Константы
# =====================================================================

_INDEXED_AT = "2024-01-15T11:00:00+00:00"
_LAST_MODIFIED = "2024-01-15T10:30:00+00:00"


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def adapter(tmp_path: Path) -> Iterator[SQLiteAdapter]:
    """Свежий ``SQLiteAdapter`` со схемой ядра.

    Схема создаётся через production-путь
    (``DatabaseManager.ensure_all``), что гарантирует соответствие
    актуальной структуре таблиц (``documents``, ``text_index_fts``).

    Args:
        tmp_path: Встроенная фикстура pytest.

    Yields:
        Настроенный ``SQLiteAdapter``; закрывается в teardown.
    """
    db_path = tmp_path / "index_writer_test.db"
    adapter = SQLiteAdapter(str(db_path))
    try:
        DatabaseManager(adapter).ensure_all()
        yield adapter
    finally:
        adapter.close()


@pytest.fixture
def writer(adapter: SQLiteAdapter) -> SqliteIndexWriter:
    """``SqliteIndexWriter`` поверх свежего адаптера.

    Args:
        adapter: Свежий адаптер со схемой.

    Returns:
        Настроенный writer.
    """
    return SqliteIndexWriter(adapter)


# =====================================================================
# Helpers
# =====================================================================


def _make_plan(
    doc_id: str,
    file_hash: str,
    *,
    file_path: str = "",
    file_size: int = 1024,
    pages: tuple[PageRecord, ...] | None = None,
) -> DocumentIndexPlan:
    """Создаёт ``DocumentIndexPlan`` с заданными параметрами.

    Args:
        doc_id: Идентификатор документа (PRIMARY KEY).
        file_hash: Хеш файла (UNIQUE NOT NULL).
        file_path: Относительный путь; по умолчанию — ``f"{doc_id}.pdf"``.
        file_size: Размер файла в байтах.
        pages: Кортеж страниц; по умолчанию — одна страница с
            текстом, равным ``doc_id``.

    Returns:
        Готовый план.
    """
    if pages is None:
        pages = (
            PageRecord(
                page_number=0,
                text=f"text_for_{doc_id}",
                normalized_text=f"text_for_{doc_id}",
            ),
        )
    return DocumentIndexPlan(
        doc_id=doc_id,
        file_path=file_path or f"{doc_id}.pdf",
        file_hash=file_hash,
        file_size=file_size,
        last_modified=_LAST_MODIFIED,
        page_count=len(pages),
        indexed_at=_INDEXED_AT,
        pages=pages,
    )


def _count_documents(adapter: SQLiteAdapter) -> int:
    """Возвращает количество записей в ``documents``."""
    rows = adapter.execute("SELECT COUNT(*) FROM documents")
    return int(rows[0][0]) if rows else 0


def _count_pages(adapter: SQLiteAdapter, doc_id: str) -> int:
    """Возвращает количество страниц документа в ``text_index_fts``."""
    rows = adapter.execute(
        "SELECT COUNT(*) FROM text_index_fts WHERE doc_id = ?",
        (doc_id,),
    )
    return int(rows[0][0]) if rows else 0


def _doc_exists(adapter: SQLiteAdapter, doc_id: str) -> bool:
    """Проверяет наличие документа в ``documents``."""
    rows = adapter.execute(
        "SELECT 1 FROM documents WHERE doc_id = ?",
        (doc_id,),
    )
    return len(rows) > 0


def _page_texts(adapter: SQLiteAdapter, doc_id: str) -> list[tuple[int, str, str]]:
    """Возвращает список ``(page_number, text, normalized_text)``.

    Порядок — по возрастанию ``page_number``.
    """
    rows = adapter.execute(
        "SELECT page_number, text_content, normalized_text "
        "FROM text_index_fts WHERE doc_id = ? "
        "ORDER BY page_number",
        (doc_id,),
    )
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def _make_broken_plan(doc_id: str) -> DocumentIndexPlan:
    """Создаёт план с ``file_hash=None`` для проверки изоляции.

    Нарушает ``NOT NULL`` constraint на ``documents.file_hash``:
    SQLite отвергнет INSERT с ``sqlite3.IntegrityError``. Это
    локализованный способ спровоцировать ошибку записи без
    monkeypatch'а.

    В реальном использовании ``file_hash`` всегда непустой
    (SHA-256 hex), но writer должен изолировать любые ошибки
    отдельного плана от остальных.

    Args:
        doc_id: Идентификатор документа.

    Returns:
        План с невалидным ``file_hash``.
    """
    return DocumentIndexPlan(
        doc_id=doc_id,
        file_path=f"{doc_id}.pdf",
        file_hash=None,  # type: ignore[arg-type]
        file_size=0,
        last_modified=_LAST_MODIFIED,
        page_count=0,
        indexed_at=_INDEXED_AT,
        pages=(),
    )


# =====================================================================
# Раздел 1. write_plan: успех и идемпотентность
# =====================================================================


def test_write_plan_persists_document_and_pages(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Успешная запись одиночного плана.

    Проверяет:
        - запись появилась в ``documents``;
        - все страницы записаны в ``text_index_fts``;
        - поля документа соответствуют плану.
    """
    plan = _make_plan(
        "doc_001",
        "hash_001",
        file_path="раздел_01/док.pdf",
        file_size=2048,
        pages=(
            PageRecord(0, "First page", "first page"),
            PageRecord(1, "Second page", "second page"),
        ),
    )

    writer.write_plan(plan)

    assert _count_documents(adapter) == 1
    assert _doc_exists(adapter, "doc_001")
    assert _count_pages(adapter, "doc_001") == 2

    rows = adapter.execute(
        "SELECT file_path, file_size, page_count, indexed_at, "
        "       last_modified, cached_size, cached_mtime "
        "FROM documents WHERE doc_id = ?",
        ("doc_001",),
    )
    assert len(rows) == 1
    file_path, file_size, page_count, indexed_at, last_modified, c_size, c_mtime = rows[0]
    assert str(file_path) == "раздел_01/док.pdf"
    assert int(file_size) == 2048
    assert int(page_count) == 2
    assert str(indexed_at) == _INDEXED_AT
    assert str(last_modified) == _LAST_MODIFIED
    # Инвариант ленивого хеширования: cached_* = file_*.
    assert int(c_size) == 2048
    assert str(c_mtime) == _LAST_MODIFIED


def test_write_plan_persists_page_texts_in_order(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Тексты страниц сохраняются в порядке ``page_number``."""
    plan = _make_plan(
        "doc_002",
        "hash_002",
        pages=(
            PageRecord(0, "alpha", "alpha"),
            PageRecord(1, "beta", "beta"),
            PageRecord(2, "gamma", "gamma"),
        ),
    )

    writer.write_plan(plan)

    texts = _page_texts(adapter, "doc_002")
    assert texts == [
        (0, "alpha", "alpha"),
        (1, "beta", "beta"),
        (2, "gamma", "gamma"),
    ]


def test_write_plan_idempotent_overwrites_same_doc_id(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Повторная запись с тем же ``doc_id`` перезаписывает.

    Проверяет:
        - количество документов осталось ``1``;
        - поля обновлены до новых значений;
        - старые страницы удалены.
    """
    plan_v1 = _make_plan(
        "doc_003",
        "hash_003_v1",
        file_size=100,
        pages=(PageRecord(0, "v1_page_0", "v1_page_0"),),
    )
    plan_v2 = _make_plan(
        "doc_003",
        "hash_003_v2",
        file_size=200,
        pages=(
            PageRecord(0, "v2_page_0", "v2_page_0"),
            PageRecord(1, "v2_page_1", "v2_page_1"),
        ),
    )

    writer.write_plan(plan_v1)
    writer.write_plan(plan_v2)

    assert _count_documents(adapter) == 1
    assert _count_pages(adapter, "doc_003") == 2

    rows = adapter.execute(
        "SELECT file_hash, file_size FROM documents WHERE doc_id = ?",
        ("doc_003",),
    )
    assert str(rows[0][0]) == "hash_003_v2"
    assert int(rows[0][1]) == 200

    texts = _page_texts(adapter, "doc_003")
    assert texts == [
        (0, "v2_page_0", "v2_page_0"),
        (1, "v2_page_1", "v2_page_1"),
    ]


# =====================================================================
# Раздел 2. write_plans_batch
# =====================================================================


def test_write_plans_batch_empty_returns_zero(
    writer: SqliteIndexWriter,
) -> None:
    """``write_plans_batch([])`` → ``(0, 0)``."""
    success, failed = writer.write_plans_batch([])

    assert success == 0
    assert failed == 0


def test_write_plans_batch_all_success(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """N валидных планов → ``(N, 0)``; все записи сохранены."""
    plans = [_make_plan(f"doc_{i:03d}", f"hash_{i:03d}") for i in range(5)]

    success, failed = writer.write_plans_batch(plans)

    assert success == 5
    assert failed == 0
    assert _count_documents(adapter) == 5
    for i in range(5):
        assert _doc_exists(adapter, f"doc_{i:03d}")


def test_write_plans_batch_savepoint_isolation(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Битый план не ломает остальные.

    Сценарий:
        - батч ``[valid_1, broken, valid_2]``;
        - ``broken`` имеет ``file_hash=None`` → ``IntegrityError``
          на INSERT в ``documents``;
        - SAVEPOINT откатывает только ``broken``;
        - ``valid_1`` и ``valid_2`` записаны.

    Проверяет:
        - результат ``(2, 1)``;
        - ``valid_1`` и ``valid_2`` присутствуют в БД;
        - ``broken`` отсутствует.
    """
    plans = [
        _make_plan("doc_ok_1", "hash_ok_1"),
        _make_broken_plan("doc_broken"),
        _make_plan("doc_ok_2", "hash_ok_2"),
    ]

    success, failed = writer.write_plans_batch(plans)

    assert success == 2
    assert failed == 1
    assert _doc_exists(adapter, "doc_ok_1")
    assert _doc_exists(adapter, "doc_ok_2")
    assert not _doc_exists(adapter, "doc_broken")


def test_write_plans_batch_commit_interval_one(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """``commit_interval=1`` — коммит после каждого плана."""
    plans = [_make_plan(f"doc_{i}", f"hash_{i}") for i in range(5)]

    success, failed = writer.write_plans_batch(plans, commit_interval=1)

    assert success == 5
    assert failed == 0
    assert _count_documents(adapter) == 5


def test_write_plans_batch_commit_interval_larger_than_batch(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """``commit_interval`` больше длины — единственный финальный коммит."""
    plans = [_make_plan(f"doc_{i}", f"hash_{i}") for i in range(3)]

    success, failed = writer.write_plans_batch(plans, commit_interval=1000)

    assert success == 3
    assert failed == 0
    assert _count_documents(adapter) == 3


def test_write_plans_batch_with_multiple_failures(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Несколько битых планов → корректные счётчики."""
    plans = [
        _make_plan("doc_ok_1", "hash_ok_1"),
        _make_broken_plan("doc_bad_1"),
        _make_plan("doc_ok_2", "hash_ok_2"),
        _make_broken_plan("doc_bad_2"),
        _make_plan("doc_ok_3", "hash_ok_3"),
    ]

    success, failed = writer.write_plans_batch(plans)

    assert success == 3
    assert failed == 2
    for doc_id in ("doc_ok_1", "doc_ok_2", "doc_ok_3"):
        assert _doc_exists(adapter, doc_id)
    for doc_id in ("doc_bad_1", "doc_bad_2"):
        assert not _doc_exists(adapter, doc_id)


# =====================================================================
# Раздел 3. remove_document
# =====================================================================


def test_remove_document_removes_document_and_pages(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Удаление документа удаляет его из ``documents`` и ``text_index_fts``."""
    plan = _make_plan(
        "doc_to_remove",
        "hash_remove",
        pages=(
            PageRecord(0, "p0", "p0"),
            PageRecord(1, "p1", "p1"),
        ),
    )
    writer.write_plan(plan)
    assert _count_documents(adapter) == 1
    assert _count_pages(adapter, "doc_to_remove") == 2

    writer.remove_document("doc_to_remove")

    assert _count_documents(adapter) == 0
    assert _count_pages(adapter, "doc_to_remove") == 0


def test_remove_document_idempotent(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Удаление несуществующего ``doc_id`` — no-op.

    Метод не должен выбрасывать исключение (DELETE без совпадений
    в SQLite — валидная операция).
    """
    writer.remove_document("nonexistent_doc")

    assert _count_documents(adapter) == 0


def test_remove_document_does_not_affect_others(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Удаление одного документа не затрагивает остальные."""
    writer.write_plan(_make_plan("doc_a", "hash_a"))
    writer.write_plan(_make_plan("doc_b", "hash_b"))

    writer.remove_document("doc_a")

    assert not _doc_exists(adapter, "doc_a")
    assert _doc_exists(adapter, "doc_b")
    assert _count_documents(adapter) == 1


# =====================================================================
# Раздел 4. Структура _plan_to_queries
# =====================================================================


def test_plan_to_queries_structure(
    writer: SqliteIndexWriter,
) -> None:
    """``_plan_to_queries`` формирует 3 + N запросов.

    Порядок:
        1. DELETE FROM text_index_fts
        2. DELETE FROM documents
        3. INSERT OR REPLACE INTO documents
        4..N. INSERT INTO text_index_fts (для каждой страницы)

    Тест фиксирует структуру: если порядок или количество изменится,
    тест поймает регресс.
    """
    plan = _make_plan(
        "doc_struct",
        "hash_struct",
        pages=(
            PageRecord(0, "p0", "p0"),
            PageRecord(1, "p1", "p1"),
            PageRecord(2, "p2", "p2"),
        ),
    )

    queries = writer._plan_to_queries(plan)

    assert len(queries) == 3 + 3

    sql_1, params_1 = queries[0]
    assert "DELETE FROM text_index_fts" in sql_1
    assert params_1 == ("doc_struct",)

    sql_2, params_2 = queries[1]
    assert "DELETE FROM documents" in sql_2
    assert params_2 == ("doc_struct",)

    sql_3, params_3 = queries[2]
    assert "INSERT OR REPLACE INTO documents" in sql_3
    assert params_3[0] == "doc_struct"
    assert params_3[1] == "doc_struct.pdf"
    assert params_3[2] == "hash_struct"
    # cached_size / cached_mtime == file_size / last_modified
    assert params_3[7] == params_3[3]  # cached_size == file_size
    assert params_3[8] == params_3[6]  # cached_mtime == last_modified

    for i, expected_page_number in enumerate((0, 1, 2)):
        sql_page, params_page = queries[3 + i]
        assert "INSERT INTO text_index_fts" in sql_page
        assert params_page == (
            "doc_struct",
            expected_page_number,
            f"p{expected_page_number}",
            f"p{expected_page_number}",
        )


# =====================================================================
# Раздел 5. Протокол и полный цикл
# =====================================================================


def test_writer_implements_protocol(writer: SqliteIndexWriter) -> None:
    """``SqliteIndexWriter`` удовлетворяет ``IIndexWriter``.

    LSP-проверка в runtime (в дополнение к статическому анализу).
    """
    assert isinstance(writer, IIndexWriter)


def test_full_cycle_write_remove_write(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """Полный цикл: ``write_plan`` → ``remove_document`` → ``write_plan``.

    Проверяет, что после удаления можно записать новый план с тем
    же ``doc_id`` (и другим ``file_hash``).
    """
    plan_v1 = _make_plan(
        "doc_cycle",
        "hash_v1",
        pages=(PageRecord(0, "v1", "v1"),),
    )
    writer.write_plan(plan_v1)
    assert _count_pages(adapter, "doc_cycle") == 1

    writer.remove_document("doc_cycle")
    assert not _doc_exists(adapter, "doc_cycle")
    assert _count_pages(adapter, "doc_cycle") == 0

    plan_v2 = _make_plan(
        "doc_cycle",
        "hash_v2",
        pages=(
            PageRecord(0, "v2_p0", "v2_p0"),
            PageRecord(1, "v2_p1", "v2_p1"),
        ),
    )
    writer.write_plan(plan_v2)

    assert _doc_exists(adapter, "doc_cycle")
    assert _count_pages(adapter, "doc_cycle") == 2

    rows = adapter.execute(
        "SELECT file_hash FROM documents WHERE doc_id = ?",
        ("doc_cycle",),
    )
    assert str(rows[0][0]) == "hash_v2"


def test_write_plan_with_zero_pages(
    adapter: SQLiteAdapter,
    writer: SqliteIndexWriter,
) -> None:
    """План без страниц (пустой PDF) → документ записан, страниц нет."""
    plan = _make_plan(
        "doc_empty",
        "hash_empty",
        pages=(),
    )

    writer.write_plan(plan)

    assert _doc_exists(adapter, "doc_empty")
    assert _count_pages(adapter, "doc_empty") == 0

    rows = adapter.execute(
        "SELECT page_count FROM documents WHERE doc_id = ?",
        ("doc_empty",),
    )
    assert int(rows[0][0]) == 0
