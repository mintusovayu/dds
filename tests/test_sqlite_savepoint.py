"""
Тесты батчевой записи SQLite через SAVEPOINT.

Назначение
----------
Проверка поведения метода ``SQLiteAdapter.execute_write_batched_documents``,
который реализует батчевую запись документов с двумя уровнями
транзакционной защиты:

1. **SAVEPOINT на каждый документ.** Если хотя бы один запрос
   документа падает, весь документ откатывается (ROLLBACK TO
   SAVEPOINT). Остальные документы в батче продолжают обработку
   независимо.

2. **Промежуточные COMMIT.** Каждые ``commit_interval`` документов
   выполняется промежуточный ``COMMIT`` — снижает накопление
   незакоммиченных изменений при массовом индексировании (например,
   10 000 PDF).

Финальный ``COMMIT`` в конце батча фиксирует изменения документов,
успешно прошедших SAVEPOINT.

Проверяемые инварианты
----------------------
- Пустой батч возвращает ``(0, 0)``.
- Все документы с валидными запросами → ``(N, 0)``; данные сохранены.
- Один документ с ошибкой в середине батча → ``(N-1, 1)``; успешные
  документы сохранены, ошибочный полностью отсутствует в БД.
- Частичный сбой внутри документа (последний из нескольких запросов
  падает) → весь документ откатывается: промежуточные вставки не
  сохраняются.
- Несколько документов с ошибками → ``(N-K, K)``; все ошибочные
  отсутствуют, все успешные сохранены.
- Последовательные ошибки в разных документах не влияют друг на
  друга (SAVEPOINT изолирован между итерациями).
- Разные значения ``commit_interval`` не влияют на итоговый результат
  при отсутствии ошибок.

Стратегия тестирования
----------------------
- **Реальная SQLite на ``tmp_path``.** Не мокается: предмет теста —
  именно поведение SQLite-транзакций (SAVEPOINT, ROLLBACK, COMMIT).
- **Реальная схема через ``DatabaseManager.ensure_all``.** Таблица
  ``documents`` — источник для проверок; схема соответствует
  production-варианту.
- **Простые INSERT-запросы.** Каждый документ — список INSERT-ов в
  ``documents``. Ошибки моделируются нарушением PRIMARY KEY
  (дубликат ``doc_id``) — детерминированно, без необходимости
  мокать БД или эмулировать I/O-ошибки.
- **Проверки через ``adapter.execute``.** Реальные SELECT'ы
  подтверждают фактическое состояние БД, а не только возвращаемое
  значение счётчиков.

Границы
-------
- **Ошибки уровня соединения** (disk full, SQLITE_BUSY, закрытая БД)
  не моделируются: их поведение зависит от окружения. Проверка
  базовой семантики savepoint/rollback — задача этого файла;
  recovery-сценарии — интеграционные тесты.
- **Производительность** — вне области (см. ``bench_slow.py``).
- **Параллельный доступ** из нескольких потоков — вне области
  (SQLiteAdapter сериализует запись через ``_write_lock``;
  соответствующий тест — при необходимости отдельно).

Запуск
------
::

    pytest tests/test_sqlite_savepoint.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет production-файлы (все ресурсы — в tmp);
    - каждый тест изолирован (function-scoped fixtures);
    - тесты детерминированы: одинаковый вход → одинаковый результат
      независимо от порядка выполнения и окружения.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from dds_core.infrastructure.database import DatabaseManager
from dds_core.infrastructure.sqlite_adapter import SQLiteAdapter

# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def adapter(tmp_path: Path) -> Iterator[SQLiteAdapter]:
    """Свежий ``SQLiteAdapter`` со схемой ядра на временной БД.

    Схема создаётся через production-путь
    (``DatabaseManager.ensure_all``) — гарантирует, что тесты
    работают с актуальной структурой таблиц, включая
    ``documents`` (PK ``doc_id``, UNIQUE ``file_hash``).

    Args:
        tmp_path: Встроенная фикстура pytest; уникальный каталог
            на каждый тест.

    Yields:
        Настроенный ``SQLiteAdapter``; закрывается в teardown.
    """
    db_path = tmp_path / "savepoint_test.db"
    adapter = SQLiteAdapter(str(db_path))
    try:
        DatabaseManager(adapter).ensure_all()
        yield adapter
    finally:
        adapter.close()


# =====================================================================
# Вспомогательные функции
# =====================================================================


def _insert_doc_query(doc_id: str, suffix: str) -> tuple[str, tuple]:
    """Формирует INSERT-запрос для таблицы ``documents``.

    Все три обязательных NOT NULL столбца (``doc_id``, ``file_path``,
    ``file_hash``) заполняются значениями, производными от
    ``suffix``. Уникальность ``file_hash`` обеспечивается самим
    суффиксом.

    Args:
        doc_id: Значение PRIMARY KEY.
        suffix: Строковый суффикс для генерации path/hash.

    Returns:
        Кортеж ``(SQL, params)`` для передачи в батч.
    """
    return (
        "INSERT INTO documents (doc_id, file_path, file_hash) VALUES (?, ?, ?)",
        (doc_id, f"path_{suffix}.pdf", f"hash_{suffix}"),
    )


def _count_documents(adapter: SQLiteAdapter) -> int:
    """Возвращает количество строк в таблице ``documents``.

    Args:
        adapter: Адаптер с открытым соединением.

    Returns:
        Количество записей.
    """
    rows = adapter.execute("SELECT COUNT(*) FROM documents")
    return int(rows[0][0]) if rows else 0


def _doc_exists(adapter: SQLiteAdapter, doc_id: str) -> bool:
    """Проверяет наличие документа по ``doc_id``.

    Args:
        adapter: Адаптер с открытым соединением.
        doc_id: Идентификатор документа.

    Returns:
        ``True``, если запись существует.
    """
    rows = adapter.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,))
    return len(rows) > 0


# =====================================================================
# Тесты
# =====================================================================


def test_empty_batch_returns_zero_counts(adapter: SQLiteAdapter) -> None:
    """Пустой батч — no-op, возвращает ``(0, 0)``.

    Проверяет:
        - результат метода ``(0, 0)``;
        - таблица ``documents`` осталась пустой.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    success, failed = adapter.execute_write_batched_documents([])

    assert success == 0
    assert failed == 0
    assert _count_documents(adapter) == 0


def test_all_documents_succeed(adapter: SQLiteAdapter) -> None:
    """Все документы успешно записаны.

    Сценарий:
        - 5 документов, каждый — 1 валидный INSERT;
        - без ошибок.

    Проверяет:
        - результат ``(5, 0)``;
        - таблица содержит 5 записей;
        - каждый ``doc_id`` присутствует.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    doc_ids = [f"doc_{i}" for i in range(5)]
    batches = [[_insert_doc_query(doc_id, suffix=str(i))] for i, doc_id in enumerate(doc_ids)]

    success, failed = adapter.execute_write_batched_documents(batches)

    assert success == 5
    assert failed == 0
    assert _count_documents(adapter) == 5
    for doc_id in doc_ids:
        assert _doc_exists(adapter, doc_id), f"doc_id={doc_id!r} отсутствует"


def test_single_document_failure_isolated_from_others(
    adapter: SQLiteAdapter,
) -> None:
    """Ошибка в одном документе не влияет на остальные.

    Сценарий:
        - батч из 3 документов: [valid_1, invalid, valid_2];
        - invalid вставляет дубликат ``doc_id`` — нарушение PRIMARY KEY;
        - SAVEPOINT откатывает только invalid; valid_1 и valid_2
          фиксируются.

    Проверяет:
        - результат ``(2, 1)``;
        - таблица содержит 2 записи (valid_1, valid_2);
        - ошибочный ``doc_id`` отсутствует в БД.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    # Предварительно создаём документ, который будет конфликтовать.
    adapter.execute_write(
        "INSERT INTO documents (doc_id, file_path, file_hash) VALUES (?, ?, ?)",
        ("doc_conflict", "conflict.pdf", "conflict_hash"),
    )

    valid_1 = [_insert_doc_query("doc_valid_1", suffix="v1")]
    invalid = [_insert_doc_query("doc_conflict", suffix="conflict")]
    valid_2 = [_insert_doc_query("doc_valid_2", suffix="v2")]

    success, failed = adapter.execute_write_batched_documents([valid_1, invalid, valid_2])

    assert success == 2
    assert failed == 1
    # Успешные документы присутствуют.
    assert _doc_exists(adapter, "doc_valid_1")
    assert _doc_exists(adapter, "doc_valid_2")
    # Ошибочный документ не добавил новых записей.
    # (doc_conflict уже был в БД до батча; проверяем, что дубликата нет.)
    conflict_count = adapter.execute(
        "SELECT COUNT(*) FROM documents WHERE doc_id = ?", ("doc_conflict",)
    )
    assert int(conflict_count[0][0]) == 1


def test_partial_document_failure_rolls_back_entire_document(
    adapter: SQLiteAdapter,
) -> None:
    """Частичный сбой в документе → весь документ откатывается.

    Сценарий:
        - документ содержит 4 INSERT-запроса;
        - первые 3 вставляют валидные ``doc_id``;
        - 4-й вставляет дубликат ``doc_id``, конфликтующий с
          предварительно созданным документом;
        - ``_execute_queries_grouped`` группирует все 4 запроса в
          один ``executemany`` (одинаковый SQL);
        - падение последнего запроса → SAVEPOINT откатывает
          изменения от первых трёх.

    Проверяет:
        - результат ``(0, 1)``;
        - ни один из 3 первых ``doc_id`` не сохранён;
        - ``doc_conflict`` не добавлен повторно.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    # Предварительно создаём конфликтующий документ.
    adapter.execute_write(
        "INSERT INTO documents (doc_id, file_path, file_hash) VALUES (?, ?, ?)",
        ("doc_conflict", "conflict.pdf", "conflict_hash"),
    )

    partial_doc = [
        _insert_doc_query("doc_a", suffix="a"),
        _insert_doc_query("doc_b", suffix="b"),
        _insert_doc_query("doc_c", suffix="c"),
        _insert_doc_query("doc_conflict", suffix="conflict"),
    ]

    success, failed = adapter.execute_write_batched_documents([partial_doc])

    assert success == 0
    assert failed == 1
    # Ни один из промежуточных документов не сохранён.
    assert not _doc_exists(adapter, "doc_a")
    assert not _doc_exists(adapter, "doc_b")
    assert not _doc_exists(adapter, "doc_c")
    # Конфликтующий документ остался в единственном экземпляре.
    assert _count_documents(adapter) == 1


def test_multiple_documents_fail(adapter: SQLiteAdapter) -> None:
    """Несколько документов с ошибками → счётчики корректны.

    Сценарий:
        - 5 документов, 2 из них — с ошибками (дубликаты ``doc_id``
          предварительно созданных документов);
        - 3 валидных документа сохранены, 2 откатываются.

    Проверяет:
        - результат ``(3, 2)``;
        - в БД 3 успешных ``doc_id`` + 2 предварительно созданных.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    # Предварительно создаём 2 документа для конфликта.
    for i in (1, 2):
        adapter.execute_write(
            "INSERT INTO documents (doc_id, file_path, file_hash) VALUES (?, ?, ?)",
            (f"doc_pre_{i}", f"pre_{i}.pdf", f"pre_hash_{i}"),
        )

    batches = [
        [_insert_doc_query("doc_ok_1", suffix="ok1")],
        [_insert_doc_query("doc_pre_1", suffix="pre1")],  # конфликт
        [_insert_doc_query("doc_ok_2", suffix="ok2")],
        [_insert_doc_query("doc_pre_2", suffix="pre2")],  # конфликт
        [_insert_doc_query("doc_ok_3", suffix="ok3")],
    ]

    success, failed = adapter.execute_write_batched_documents(batches)

    assert success == 3
    assert failed == 2
    assert _doc_exists(adapter, "doc_ok_1")
    assert _doc_exists(adapter, "doc_ok_2")
    assert _doc_exists(adapter, "doc_ok_3")
    # 3 успешных + 2 предварительных = 5.
    assert _count_documents(adapter) == 5


def test_consecutive_failures_isolated_between_documents(
    adapter: SQLiteAdapter,
) -> None:
    """Последовательные ошибки в разных документах не мешают друг другу.

    Сценарий:
        - батч из 3 документов: [invalid_1, invalid_2, valid];
        - invalid_1 и invalid_2 оба конфликтуют с одним и тем же
          предварительным документом;
        - каждый SAVEPOINT откатывает свой документ независимо;
        - valid успешно фиксируется.

    Проверяет:
        - результат ``(1, 2)``;
        - в БД только предварительный документ и valid.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    adapter.execute_write(
        "INSERT INTO documents (doc_id, file_path, file_hash) VALUES (?, ?, ?)",
        ("doc_conflict", "conflict.pdf", "conflict_hash"),
    )

    batches = [
        [_insert_doc_query("doc_conflict", suffix="c1")],
        [_insert_doc_query("doc_conflict", suffix="c2")],
        [_insert_doc_query("doc_valid", suffix="valid")],
    ]

    success, failed = adapter.execute_write_batched_documents(batches)

    assert success == 1
    assert failed == 2
    assert _doc_exists(adapter, "doc_valid")
    # Только предварительный + valid = 2.
    assert _count_documents(adapter) == 2


def test_commit_interval_one_persists_all_successful_documents(
    adapter: SQLiteAdapter,
) -> None:
    """``commit_interval=1`` — коммит после каждого документа.

    Проверяет, что поведение метода одинаково корректно при
    минимальном интервале коммита: все успешные документы
    сохранены, данные доступны после завершения батча.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    batches = [[_insert_doc_query(f"doc_{i}", suffix=str(i))] for i in range(5)]

    success, failed = adapter.execute_write_batched_documents(
        batches,
        commit_interval=1,
    )

    assert success == 5
    assert failed == 0
    assert _count_documents(adapter) == 5


def test_commit_interval_larger_than_batch(adapter: SQLiteAdapter) -> None:
    """``commit_interval`` больше размера батча → единственный COMMIT в конце.

    Проверяет, что отсутствие промежуточных коммитов не влияет на
    сохранение данных: финальный ``COMMIT`` фиксирует всё.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    batches = [[_insert_doc_query(f"doc_{i}", suffix=str(i))] for i in range(3)]

    success, failed = adapter.execute_write_batched_documents(
        batches,
        commit_interval=1000,
    )

    assert success == 3
    assert failed == 0
    assert _count_documents(adapter) == 3


def test_commit_interval_with_mixed_results(adapter: SQLiteAdapter) -> None:
    """Промежуточные коммиты не влияют на откат ошибочных документов.

    Сценарий:
        - ``commit_interval=2``;
        - батч: [valid_1, valid_2, invalid, valid_3, valid_4];
        - после 2-го документа — COMMIT (valid_1, valid_2 зафиксированы);
        - 3-й документ ошибочный → SAVEPOINT откат;
        - 4-й и 5-й успешны → финальный COMMIT.

    Проверяет:
        - результат ``(4, 1)``;
        - в БД 4 успешных документа;
        - ошибочный отсутствует.

    Args:
        adapter: Свежий адаптер со схемой.
    """
    adapter.execute_write(
        "INSERT INTO documents (doc_id, file_path, file_hash) VALUES (?, ?, ?)",
        ("doc_conflict", "conflict.pdf", "conflict_hash"),
    )

    batches = [
        [_insert_doc_query("doc_1", suffix="1")],
        [_insert_doc_query("doc_2", suffix="2")],
        [_insert_doc_query("doc_conflict", suffix="conflict")],
        [_insert_doc_query("doc_3", suffix="3")],
        [_insert_doc_query("doc_4", suffix="4")],
    ]

    success, failed = adapter.execute_write_batched_documents(
        batches,
        commit_interval=2,
    )

    assert success == 4
    assert failed == 1
    assert _doc_exists(adapter, "doc_1")
    assert _doc_exists(adapter, "doc_2")
    assert _doc_exists(adapter, "doc_3")
    assert _doc_exists(adapter, "doc_4")
    # 4 успешных + 1 предварительный = 5.
    assert _count_documents(adapter) == 5
