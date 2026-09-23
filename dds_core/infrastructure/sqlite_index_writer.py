"""
Реализация ``IIndexWriter`` через SQLite FTS5.

Модуль предоставляет класс :class:`SqliteIndexWriter`, который
преобразует доменную модель ``DocumentIndexPlan`` в SQL-запросы
для SQLite FTS5 и выполняет их через абстракцию ``IDatabase``.

SQL-специфика, вынесенная из application-слоя:

До Фазы 5 SQL-строки для индексации формировались в
``dds_core/application/query_builder.py``, что нарушало слоистость:
application не должен знать о конкретном бэкенде БД. Фаза 5
переносит SQL-формирование в infrastructure:

+----------------------------------+----------------------------------+
| Слой                             | Ответственность                  |
+==================================+==================================+
| ``build_index_plan``             | Формирует ``DocumentIndexPlan`` —|
| (application)                    | доменную модель с метаданными    |
|                                  | и страницами.                    |
+----------------------------------+----------------------------------+
| :class:`SqliteIndexWriter`       | Преобразует ``DocumentIndexPlan``|
| (infrastructure, этот модуль)    | в SQL-запросы SQLite FTS5 и      |
|                                  | выполняет их через ``IDatabase``.|
+----------------------------------+----------------------------------+

Такое разделение позволяет заменить SQLite на другой бэкенд
(например, PostgreSQL) без изменения application-кода: достаточно
реализовать новый ``IIndexWriter``.

Схема БД, с которой работает writer:

+-----------------------+--------------------------------------------+
| Таблица               | Колонки, заполняемые writer'ом             |
+=======================+============================================+
| ``documents``         | ``doc_id``, ``file_path``, ``file_hash``,  |
|                       | ``file_size``, ``page_count``,             |
|                       | ``indexed_at``, ``last_modified``,         |
|                       | ``cached_size``, ``cached_mtime``          |
+-----------------------+--------------------------------------------+
| ``text_index_fts``    | ``doc_id``, ``page_number``,               |
|                       | ``text_content``, ``normalized_text``      |
+-----------------------+--------------------------------------------+

Поля ``object_code``, ``discipline_code``, ``document_type_code``,
``unmatched_flag`` **не заполняются** при первичной индексации —
они проставляются вторичным сканированием
(``DocumentMetadataService.update_all_documents``).

Идемпотентность:

- **``write_plan``** — перед ``INSERT OR REPLACE INTO documents``
  и ``INSERT INTO text_index_fts`` выполняется ``DELETE FROM ...``
  для соответствующих ``doc_id``. Это гарантирует чистую
  перезапись без остатка старых страниц.
- **``write_plans_batch``** — каждый план обёрнут в ``SAVEPOINT``
  (см. ``SQLiteAdapter.execute_write_batched_documents``).
  Ошибка в одном плане не влияет на остальные.
- **``remove_document``** — ``DELETE`` несуществующего ``doc_id``
  не выбрасывает исключение (no-op).

Потокобезопасность:

Класс не хранит изменяемого состояния. Потокобезопасность записи
обеспечивается ``SQLiteAdapter`` (``_write_lock`` на выделенном
write-соединении).

Принципы:
- Реализует ``IIndexWriter`` (инверсия зависимостей).
- Единственная точка формирования SQL для записи индекса.
- Не содержит бизнес-логики индексирования (это в ``build_index_plan``).
- Не выполняет логирования.

Реализуемые интерфейсы:
    ``IIndexWriter`` — абстракция записи плана индексации.
"""

from __future__ import annotations

from ..domain import config
from ..domain.index_plan import DocumentIndexPlan
from ..domain.interfaces import IDatabase

# ----------------------------------------------------------------------
# Шаблоны SQL-запросов
# ----------------------------------------------------------------------

_SQL_DELETE_FTS = "DELETE FROM text_index_fts WHERE doc_id = ?"
"""Удаление всех страниц документа из FTS-индекса.

Используется перед вставкой новых страниц для чистого upsert.
"""

_SQL_DELETE_DOCUMENT = "DELETE FROM documents WHERE doc_id = ?"
"""Удаление записи документа из таблицы ``documents``.

Используется перед ``INSERT OR REPLACE`` для гарантированной
чистой перезаписи (на случай, если схема когда-либо изменится
и ``INSERT OR REPLACE`` не покроет все колонки).
"""

_SQL_INSERT_DOCUMENT = (
    "INSERT OR REPLACE INTO documents "
    "(doc_id, file_path, file_hash, file_size, page_count, "
    " indexed_at, last_modified, cached_size, cached_mtime) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
"""Вставка/обновление метаданных документа.

``cached_size`` и ``cached_mtime`` заполняются теми же значениями,
что ``file_size`` и ``last_modified`` — это инвариант ленивого
хеширования (см. ``dds_core/application/scan_pipeline.py``).
"""

_SQL_INSERT_PAGE = (
    "INSERT INTO text_index_fts "
    "(doc_id, page_number, text_content, normalized_text) "
    "VALUES (?, ?, ?, ?)"
)
"""Вставка страницы документа в полнотекстовый индекс.

``text_content`` — оригинальный текст, ``normalized_text`` —
нормализованная версия (получена в ``build_index_plan``).
"""


# ----------------------------------------------------------------------
# Реализация IIndexWriter
# ----------------------------------------------------------------------


class SqliteIndexWriter:
    """Запись плана индексации в SQLite FTS5.

    Преобразует ``DocumentIndexPlan`` в SQL-запросы и выполняет их
    через ``IDatabase``. Реализует ``IIndexWriter``.

    Пример использования::

        writer = SqliteIndexWriter(db_adapter)

        # Одиночная запись.
        writer.write_plan(plan)

        # Батчевая запись с промежуточными COMMIT.
        success, failed = writer.write_plans_batch(plans)

        # Удаление документа.
        writer.remove_document("doc_001")

    Attributes:
        _db: Абстракция базы данных для выполнения запросов.
    """

    def __init__(self, db: IDatabase) -> None:
        """Инициализирует writer.

        Args:
            db: Реализация ``IDatabase`` для выполнения SQL-запросов.
                В production — ``SQLiteAdapter``.
        """
        self._db = db

    def write_plan(self, plan: DocumentIndexPlan) -> None:
        """Записывает план индексации одного документа в БД.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | ``_plan_to_queries(plan)`` → список SQL-запросов.  |
        +----+----------------------------------------------------+
        | 2  | ``db.execute_write_many(queries)`` → выполнение    |
        |    | всех запросов в одной транзакции.                  |
        +----+----------------------------------------------------+

        Атомарность:
            Все запросы выполняются в одной транзакции
            (``execute_write_many``). При ошибке любого запроса
            вся транзакция откатывается.

        Args:
            plan: План индексации документа.

        Raises:
            RuntimeError: Если адаптер БД закрыт.
            sqlite3.Error: Любая ошибка записи. Транзакция
                откатывается.
        """
        queries = self._plan_to_queries(plan)
        self._db.execute_write_many(queries)

    def write_plans_batch(
        self,
        plans: list[DocumentIndexPlan],
        commit_interval: int = config.SCAN_COMMIT_INTERVAL,
    ) -> tuple[int, int]:
        """Записывает батч планов с SAVEPOINT-изоляцией.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | ``document_batches = [_plan_to_queries(p) for p    |
        |    | in plans]`` — конвертация планов в списки запросов.|
        +----+----------------------------------------------------+
        | 2  | ``db.execute_write_batched_documents(              |
        |    | document_batches, commit_interval)`` — выполнение. |
        +----+----------------------------------------------------+
        | 3  | Возврат ``(success, failed)``.                     |
        +----+----------------------------------------------------+

        SAVEPOINT-изоляция:
            Каждый план обёрнут в ``SAVEPOINT`` (внутри
            ``execute_write_batched_documents``). Ошибка одного
            плана не откатывает успешно записанные планы.

        Args:
            plans: Список планов индексации. Пустой список →
                ``(0, 0)``.
            commit_interval: Количество планов между промежуточными
                ``COMMIT``. По умолчанию ``config.SCAN_COMMIT_INTERVAL``
                (500).

        Returns:
            Кортеж ``(успешно записанных, ошибочных)``.

        Raises:
            RuntimeError: Если адаптер БД закрыт.
            sqlite3.Error: При фатальной ошибке транзакции.
        """
        if not plans:
            return 0, 0

        document_batches = [self._plan_to_queries(plan) for plan in plans]
        return self._db.execute_write_batched_documents(
            document_batches,
            commit_interval=commit_interval,
        )

    def remove_document(self, doc_id: str) -> None:
        """Удаляет документ из индекса.

        Удаляет страницы документа из ``text_index_fts`` и запись
        из ``documents``. Обе операции атомарны.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``DELETE FROM text_index_fts WHERE doc_id = ?``     |
        +---+-----------------------------------------------------+
        | 2 | ``DELETE FROM documents WHERE doc_id = ?``          |
        +---+-----------------------------------------------------+
        | 3 | ``db.execute_write_many([...])`` — атомарно.        |
        +---+-----------------------------------------------------+

        Идемпотентность:
            Удаление несуществующего ``doc_id`` не выбрасывает
            исключение (``DELETE`` без совпадений — no-op).

        Args:
            doc_id: Идентификатор документа.

        Raises:
            RuntimeError: Если адаптер БД закрыт.
            sqlite3.Error: Любая ошибка записи.
        """
        queries = [
            (_SQL_DELETE_FTS, (doc_id,)),
            (_SQL_DELETE_DOCUMENT, (doc_id,)),
        ]
        self._db.execute_write_many(queries)

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _plan_to_queries(
        self,
        plan: DocumentIndexPlan,
    ) -> list[tuple[str, tuple]]:
        """Преобразует ``DocumentIndexPlan`` в список SQL-запросов.

        Порядок запросов (важен для корректной перезаписи):

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``DELETE FROM text_index_fts WHERE doc_id = ?``     |
        |   | — удаление старых страниц.                          |
        +---+-----------------------------------------------------+
        | 2 | ``DELETE FROM documents WHERE doc_id = ?``          |
        |   | — удаление старых метаданных.                       |
        +---+-----------------------------------------------------+
        | 3 | ``INSERT OR REPLACE INTO documents (...)``          |
        |   | — вставка новых метаданных.                         |
        +---+-----------------------------------------------------+
        | 4 | ``INSERT INTO text_index_fts (...)`` — для каждой   |
        |   | страницы плана.                                     |
        +---+-----------------------------------------------------+

        Соответствует порядку, который ранее использовался в
        ``query_builder.build_document_queries``.

        Args:
            plan: План индексации документа.

        Returns:
            Список кортежей ``(SQL-запрос, параметры)``.
        """
        queries: list[tuple[str, tuple]] = [
            (_SQL_DELETE_FTS, (plan.doc_id,)),
            (_SQL_DELETE_DOCUMENT, (plan.doc_id,)),
            (
                _SQL_INSERT_DOCUMENT,
                (
                    plan.doc_id,
                    plan.file_path,
                    plan.file_hash,
                    plan.file_size,
                    plan.page_count,
                    plan.indexed_at,
                    plan.last_modified,
                    plan.file_size,  # cached_size
                    plan.last_modified,  # cached_mtime
                ),
            ),
        ]

        for page in plan.pages:
            queries.append(
                (
                    _SQL_INSERT_PAGE,
                    (
                        plan.doc_id,
                        page.page_number,
                        page.text,
                        page.normalized_text,
                    ),
                )
            )

        return queries
