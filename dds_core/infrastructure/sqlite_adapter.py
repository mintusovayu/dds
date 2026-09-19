"""
Реализация интерфейса IDatabase через SQLite с пулом соединений.

Единственная точка доступа к базе данных SQLite в ядре DDS.
Все компоненты используют интерфейс ``IDatabase``, не зная
о конкретной реализации.

Модуль обеспечивает потокобезопасный доступ к SQLite:
- операции чтения выполняются через пул соединений, что позволяет
  нескольким потокам читать одновременно;
- операции записи сериализуются через выделенное соединение
  и ``threading.Lock``.

Оптимизация производительности (Фаза 1):

+-------------------------------+------------------------------------------+
| Настройка                     | Эффект                                   |
+===============================+==========================================+
| ``journal_mode=WAL``          | Параллельное чтение и запись             |
|                               | без блокировок.                          |
+-------------------------------+------------------------------------------+
| ``synchronous=NORMAL``        | fsync только при чекпоинте.              |
|                               | Ускорение записи в 3–10 раз.             |
+-------------------------------+------------------------------------------+
| ``cache_size=-65536``         | Кэш страниц 64 МБ.                       |
|                               | Ускорение повторных запросов.            |
+-------------------------------+------------------------------------------+
| ``mmap_size=268435456``       | Memory-mapped I/O 256 МБ.                |
|                               | Ускорение операций чтения.               |
+-------------------------------+------------------------------------------+
| ``wal_autocheckpoint=10000``  | Снижение частоты чекпоинтов              |
|                               | при массовом сканировании.               |
+-------------------------------+------------------------------------------+
| ``foreign_keys=ON``           | Целостность данных между таблицами.      |
+-------------------------------+------------------------------------------+
| ``temp_store=MEMORY``         | Временные таблицы и сортировки в памяти. |
+-------------------------------+------------------------------------------+
| ``journal_size_limit=64MB``   | Ограничение размера WAL-журнала.         |
+-------------------------------+------------------------------------------+

Потокобезопасность (Фаза 3, обновлено):

+-------------------------------+------------------------------------------+
| Операция                      | Механизм                                 |
+===============================+==========================================+
| Чтение (``execute``)          | Пул соединений (``ConnectionPool``).     |
|                               | Каждое соединение выдаётся эксклюзивно  |
|                               | одному потоку на время запроса.          |
+-------------------------------+------------------------------------------+
| Запись (``execute_write``,    | Выделенное соединение + ``threading.Lock``.|
| ``execute_write_many``,       | Сериализация операций записи.            |
| ``execute_write_returning_id``,|                                          |
| ``execute_write_batched_documents``)|                                     |
+-------------------------------+------------------------------------------+

Событийная модель (Фаза 5, оптимизация):

Адаптер может публиковать события через шину событий:
- ``DatabaseQuerySlow`` — при превышении порога длительности запроса.
  Включает план выполнения (``EXPLAIN QUERY PLAN``), если разрешено.
- ``DatabaseError`` — при возникновении ошибки SQLite.

Публикация включается, если в конструктор передан ``event_bus``.

Параметры порога и EXPLAIN настраиваются в ``dds_core.domain.config``:
- ``SLOW_QUERY_THRESHOLD_MS`` — порог медленного запроса.
- ``EXPLAIN_SLOW_QUERIES`` — флаг выполнения EXPLAIN для медленных запросов.

Принципы:
- Реализует интерфейс ``IDatabase`` (инверсия зависимостей).
- Не содержит бизнес-логики.
- Не выполняет логирование, но может публиковать события.
- Скрывает детали управления соединениями за пулом.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from ..domain import config
from ..domain.events import DatabaseError, DatabaseQuerySlow
from ..domain.interfaces import IEventBus
from .connection_factory import create_sqlite_connection
from .connection_pool import ConnectionPool

# ----------------------------------------------------------------------
# Адаптер SQLite
# ----------------------------------------------------------------------


class SQLiteAdapter:
    """Реализация ``IDatabase`` через SQLite с пулом соединений.

    Потокобезопасный доступ к БД с поддержкой WAL-режима
    и внешних ключей.

    Пример использования::

        adapter = SQLiteAdapter("/path/to/dds_database.db", event_bus=event_bus)
        adapter.ensure_table("documents", "CREATE TABLE IF NOT EXISTS ...")

        results = adapter.execute(
            "SELECT * FROM documents WHERE doc_id = ?",
            ("doc_001",),
        )

        adapter.execute_write(
            "INSERT INTO documents ...",
            (...,),
        )

        scan_id = adapter.execute_write_returning_id(
            "INSERT INTO scan_state ...",
            (...),
        )

        success, failed = adapter.execute_write_batched_documents(
            document_batches,
            commit_interval=200,
        )

        pool_stats = adapter.get_pool_stats()

        adapter.close()

    Атрибуты:

    +---------------------+----------------------------------------------+
    | Атрибут             | Описание                                     |
    +=====================+==============================================+
    | ``_db_path``        | Путь к файлу базы данных.                    |
    +---------------------+----------------------------------------------+
    | ``_write_connection``| Выделенное соединение для записи.           |
    |                     | ``None`` если закрыто.                      |
    +---------------------+----------------------------------------------+
    | ``_write_lock``     | ``threading.Lock`` для сериализации          |
    |                     | операций записи.                             |
    +---------------------+----------------------------------------------+
    | ``_read_pool``      | Пул соединений для чтения.                  |
    |                     | ``None`` если закрыто.                      |
    +---------------------+----------------------------------------------+
    | ``_event_bus``      | Шина событий (опционально).                 |
    +---------------------+----------------------------------------------+
    """

    def __init__(
        self,
        db_path: str,
        read_connections: int | None = None,
        write_connections: int | None = None,
        event_bus: IEventBus | None = None,
    ) -> None:
        """Инициализирует адаптер.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Сохранить ``db_path``.                              |
        +---+-----------------------------------------------------+
        | 2 | Определить количество read-соединений (если не      |
        |   | задано, использовать ``config.DB_READ_CONNECTIONS``).|
        +---+-----------------------------------------------------+
        | 3 | Определить количество write-соединений (если не     |
        |   | задано, использовать ``config.DB_WRITE_CONNECTIONS``).|
        |   | Обычно 1.                                          |
        +---+-----------------------------------------------------+
        | 4 | Создать ``_write_lock``.                            |
        +---+-----------------------------------------------------+
        | 5 | Создать ``_read_pool`` через ``ConnectionPool``.    |
        |   | Пул заполняется соединениями, созданными фабрикой. |
        +---+-----------------------------------------------------+
        | 6 | Создать ``_write_connection`` через фабрику.        |
        |   | Используется для всех операций записи.              |
        +---+-----------------------------------------------------+
        | 7 | Сохранить ``event_bus`` (опционально).              |
        +---+-----------------------------------------------------+

        Args:
            db_path: Путь к файлу БД. Создаётся при первом
                подключении. ``":memory:"`` для тестов.
            read_connections: Количество соединений в пуле чтения.
                Если ``None``, используется ``config.DB_READ_CONNECTIONS``.
            write_connections: Количество выделенных соединений записи.
                Если ``None``, используется ``config.DB_WRITE_CONNECTIONS``.
                Должно быть 1 (поддержка нескольких не реализована,
                но параметр оставлен для гибкости).
            event_bus: Шина событий для публикации ``DatabaseQuerySlow``
                и ``DatabaseError``. Если ``None``, события не публикуются.

        Raises:
            sqlite3.Error: Если создание соединений не удалось.
            ValueError: Если ``write_connections`` != 1 (в текущей версии).
        """
        self._db_path = db_path

        if read_connections is None:
            read_connections = config.DB_READ_CONNECTIONS
        if write_connections is None:
            write_connections = config.DB_WRITE_CONNECTIONS

        if write_connections != 1:
            raise ValueError(
                f"Поддерживается только одно соединение для записи, получено: {write_connections}"
            )

        self._write_lock = threading.Lock()

        self._read_pool = ConnectionPool(
            db_path=db_path,
            size=read_connections,
            connection_factory=create_sqlite_connection,
        )

        self._write_connection = create_sqlite_connection(db_path)
        self._event_bus = event_bus

    # ------------------------------------------------------------------
    # Свойства
    # ------------------------------------------------------------------

    def get_db_path(self) -> str:
        """Возвращает путь к файлу базы данных."""
        return self._db_path

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    def execute(self, query: str, params: tuple = ()) -> list:
        """Выполняет запрос на чтение.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Проверка, что пул чтения не закрыт.                 |
        +---+-----------------------------------------------------+
        | 2 | ``conn = self._read_pool.acquire_read()`` —         |
        |   | получить соединение из пула.                        |
        +---+-----------------------------------------------------+
        | 3 | Выполнить запрос, измерить время.                    |
        +---+-----------------------------------------------------+
        | 4 | Если время превышает порог, опубликовать            |
        |   | ``DatabaseQuerySlow`` с планом выполнения (если     |
        |   | разрешено).                                        |
        +---+-----------------------------------------------------+
        | 5 | При ``sqlite3.Error`` опубликовать ``DatabaseError`` |
        |   | и пробросить исключение.                            |
        +---+-----------------------------------------------------+
        | 6 | Вернуть соединение в пул (``finally``).             |
        +---+-----------------------------------------------------+

        Args:
            query: SQL-запрос (SELECT).
            params: Параметры запроса.

        Returns:
            Список кортежей. Пустой список если нет результатов.

        Raises:
            RuntimeError: Если адаптер закрыт.
            sqlite3.Error: Запрос не удался.
        """
        if self._read_pool is None or self._read_pool.is_closed:
            raise RuntimeError("Адаптер закрыт. Чтение невозможно.")

        conn = self._read_pool.acquire_read()
        try:
            start = time.monotonic()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            duration_ms = (time.monotonic() - start) * 1000

            if duration_ms > config.SLOW_QUERY_THRESHOLD_MS:
                explain_plan = ""
                if config.EXPLAIN_SLOW_QUERIES:
                    explain_plan = self._get_explain_plan(conn, query, params)
                self._publish_slow_query(query, duration_ms, explain_plan)

            return rows
        except sqlite3.Error as e:
            self._publish_db_error(query, type(e).__name__, str(e))
            raise
        finally:
            self._read_pool.release_read(conn)

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------

    def execute_write(self, query: str, params: tuple = ()) -> None:
        """Выполняет запрос на запись.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Проверка, что write-соединение существует.          |
        +---+-----------------------------------------------------+
        | 2 | Захват ``_write_lock``.                             |
        +---+-----------------------------------------------------+
        | 3 | Выполнить запрос, измерить время.                    |
        +---+-----------------------------------------------------+
        | 4 | ``commit()`` и проверка порога медленного запроса.  |
        +---+-----------------------------------------------------+
        | 5 | При ``sqlite3.Error`` → rollback, публикация          |
        |   | ``DatabaseError``, проброс исключения.              |
        +---+-----------------------------------------------------+
        | 6 | Освобождение ``_write_lock`` (``finally``).         |
        +---+-----------------------------------------------------+

        Args:
            query: SQL-запрос (INSERT, UPDATE, DELETE, CREATE).
            params: Параметры запроса.

        Raises:
            RuntimeError: Если адаптер закрыт.
            sqlite3.Error: Запрос не удался. Транзакция откатывается.
        """
        if self._write_connection is None:
            raise RuntimeError("Адаптер закрыт. Запись невозможна.")

        with self._write_lock:
            try:
                start = time.monotonic()
                self._write_connection.execute(query, params)
                self._write_connection.commit()
                duration_ms = (time.monotonic() - start) * 1000

                if duration_ms > config.SLOW_QUERY_THRESHOLD_MS:
                    explain_plan = ""
                    if config.EXPLAIN_SLOW_QUERIES:
                        explain_plan = self._get_explain_plan(self._write_connection, query, params)
                    self._publish_slow_query(query, duration_ms, explain_plan)

            except sqlite3.Error as e:
                self._write_connection.rollback()
                self._publish_db_error(query, type(e).__name__, str(e))
                raise

    def execute_write_returning_id(self, query: str, params: tuple = ()) -> int:
        """Выполняет запрос на запись и возвращает идентификатор последней вставленной строки.

        Атомарность гарантирована: ``INSERT`` и получение ``lastrowid``
        выполняются в рамках одного соединения под блокировкой записи.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Проверка, что write-соединение существует.          |
        +---+-----------------------------------------------------+
        | 2 | Захват ``_write_lock``.                             |
        +---+-----------------------------------------------------+
        | 3 | Выполнение запроса и фиксация транзакции.           |
        +---+-----------------------------------------------------+
        | 4 | Возврат ``cursor.lastrowid``.                       |
        +---+-----------------------------------------------------+

        При ошибке:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Откат транзакции (``ROLLBACK``).                    |
        +---+-----------------------------------------------------+
        | 2 | Публикация события ``DatabaseError`` (если шина     |
        |   | доступна).                                          |
        +---+-----------------------------------------------------+
        | 3 | Проброс исключения.                                 |
        +---+-----------------------------------------------------+

        Args:
            query: SQL-запрос (``INSERT``).
            params: Параметры запроса.

        Returns:
            Целочисленный идентификатор последней вставленной строки.

        Raises:
            RuntimeError: Если адаптер закрыт.
            sqlite3.Error: Если запрос не удался.
        """
        if self._write_connection is None:
            raise RuntimeError("Адаптер закрыт. Запись невозможна.")

        with self._write_lock:
            try:
                cursor = self._write_connection.execute(query, params)
                self._write_connection.commit()
                return cursor.lastrowid
            except sqlite3.Error as e:
                self._write_connection.rollback()
                self._publish_db_error(query, type(e).__name__, str(e))
                raise

    def execute_write_many(
        self,
        queries: list[tuple[str, tuple]],
    ) -> None:
        """Выполняет несколько запросов в одной транзакции.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Проверка write-соединения.                          |
        +---+-----------------------------------------------------+
        | 2 | Захват ``_write_lock``.                             |
        +---+-----------------------------------------------------+
        | 3 | Инициализация ``current_query = ""``.               |
        +---+-----------------------------------------------------+
        | 4 | Для каждого запроса:                                |
        |   | a. ``current_query = query`` — запомнить текущий.   |
        |   | b. ``execute(query, params)``.                      |
        +---+-----------------------------------------------------+
        | 5 | ``commit()``.                                       |
        +---+-----------------------------------------------------+
        | 6 | При ошибке: rollback, публикация ``DatabaseError``  |
        |   | с ``current_query`` (запрос, на котором произошла   |
        |   | ошибка), проброс исключения.                        |
        +---+-----------------------------------------------------+

        Примечание:
            Медленные запросы не отслеживаются для пакета,
            так как измерение каждого запроса может замедлить запись.
            Если требуется, можно измерять общую длительность транзакции.

            При ошибке публикуется именно тот запрос, на котором
            произошло исключение, а не первый запрос пакета. Это
            обеспечивает точную диагностику.

        Args:
            queries: Список кортежей ``(запрос, параметры)``.

        Raises:
            RuntimeError: Если адаптер закрыт.
            sqlite3.Error: Любой запрос не удался.
                Вся транзакция откатывается.
        """
        if self._write_connection is None:
            raise RuntimeError("Адаптер закрыт. Запись невозможна.")

        with self._write_lock:
            current_query = ""
            try:
                for query, params in queries:
                    current_query = query
                    self._write_connection.execute(query, params)
                self._write_connection.commit()
            except sqlite3.Error as e:
                self._write_connection.rollback()
                self._publish_db_error(current_query, type(e).__name__, str(e))
                raise

    def execute_write_batched_documents(
        self,
        document_batches: list[list[tuple[str, tuple]]],
        commit_interval: int = config.SCAN_COMMIT_INTERVAL,
    ) -> tuple[int, int]:
        """Выполняет запись группы документов с батчевыми коммитами.

        Каждый документ (список запросов) оборачивается в ``SAVEPOINT``
        для обеспечения атомарности: при ошибке в любом запросе документа
        откатывается только этот документ, остальные продолжаются.
        Каждые ``commit_interval`` документов выполняется ``COMMIT``.

        Внутри обработки каждого документа запросы группируются по
        одинаковому SQL и выполняются через ``executemany`` для
        повышения производительности (см. ``_execute_queries_grouped``).

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Проверка write-соединения.                          |
        +---+-----------------------------------------------------+
        | 2 | Захват ``_write_lock``.                             |
        +---+-----------------------------------------------------+
        | 3 | Инициализация счётчиков ``success = 0``,             |
        |   | ``failed = 0``.                                     |
        +---+-----------------------------------------------------+
        | 4 | Для каждого документа ``doc_queries``:              |
        |   | a. Создать ``SAVEPOINT doc_{i}``.                   |
        |   | b. Вызвать ``_execute_queries_grouped`` для         |
        |   |    выполнения запросов документа.                  |
        |   | c. ``RELEASE SAVEPOINT doc_{i}``, ``success++``.    |
        |   | d. При ошибке:                                     |
        |   |    - ``ROLLBACK TO SAVEPOINT doc_{i}``.             |
        |   |    - ``RELEASE SAVEPOINT doc_{i}``.                 |
        |   |    - ``failed++``.                                  |
        |   | e. Если ``(i+1) % commit_interval == 0``:           |
        |   |    - ``COMMIT``.                                    |
        +---+-----------------------------------------------------+
        | 5 | Финальный ``COMMIT``.                               |
        +---+-----------------------------------------------------+
        | 6 | При ошибке верхнего уровня: откат всей транзакции,  |
        |   | публикация ``DatabaseError``, проброс исключения.   |
        +---+-----------------------------------------------------+

        Args:
            document_batches: Список документов, каждый из которых
                представлен списком запросов ``(запрос, параметры)``.
            commit_interval: Интервал промежуточных коммитов (в документах).
                По умолчанию берётся из ``config.SCAN_COMMIT_INTERVAL``.

        Returns:
            Кортеж ``(успешных документов, ошибочных документов)``.

        Raises:
            RuntimeError: Если адаптер закрыт.
            sqlite3.Error: При фатальной ошибке транзакции.
        """
        if self._write_connection is None:
            raise RuntimeError("Адаптер закрыт.")
        success = 0
        failed = 0
        with self._write_lock:
            try:
                for i, doc_queries in enumerate(document_batches):
                    sp = f"doc_{i}"
                    try:
                        self._write_connection.execute(f"SAVEPOINT {sp}")
                        # Группируем одинаковые запросы внутри документа
                        self._execute_queries_grouped(self._write_connection, doc_queries)
                        self._write_connection.execute(f"RELEASE SAVEPOINT {sp}")
                        success += 1
                    except sqlite3.Error:
                        self._write_connection.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        self._write_connection.execute(f"RELEASE SAVEPOINT {sp}")
                        failed += 1
                    if (i + 1) % commit_interval == 0:
                        self._write_connection.commit()
                self._write_connection.commit()
            except sqlite3.Error as e:
                self._write_connection.rollback()
                self._publish_db_error("", type(e).__name__, str(e))
                raise
        return success, failed

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def ensure_table(self, name: str, schema: str) -> None:
        """Создаёт таблицу если не существует.

        Args:
            name: Имя таблицы.
            schema: SQL-схема.

        Raises:
            RuntimeError: Если адаптер закрыт.
            sqlite3.Error: Создание не удалось.
        """
        self.execute_write(schema)

    def table_exists(self, name: str) -> bool:
        """Проверяет существование таблицы или VIEW.

        Args:
            name: Имя таблицы или VIEW.

        Returns:
            ``True`` если существует, ``False`` иначе.
        """
        results = self.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,),
        )
        return len(results) > 0

    def get_pool_stats(self) -> dict:
        """Возвращает статистику пула соединений для диагностики.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Сбор информации о пуле чтения и write-соединении.   |
        +---+-----------------------------------------------------+
        | 2 | Формирование словаря со следующими ключами:         |
        |   | - ``read_pool_size``: размер пула чтения.           |
        |   | - ``read_pool_closed``: флаг закрытия пула чтения.  |
        |   | - ``write_connection_active``: активно ли           |
        |   |   соединение записи.                                |
        +---+-----------------------------------------------------+
        | 3 | Возврат словаря.                                    |
        +---+-----------------------------------------------------+

        Returns:
            Словарь со статистикой пула соединений.
        """
        return {
            "read_pool_size": self._read_pool.size if self._read_pool else 0,
            "read_pool_closed": self._read_pool.is_closed if self._read_pool else True,
            "write_connection_active": self._write_connection is not None,
        }

    def close(self) -> None:
        """Закрывает все соединения с БД.

        Метод идемпотентен.
        """
        if self._write_connection is not None:
            try:
                self._write_connection.close()
            except sqlite3.Error:
                pass
            self._write_connection = None

        if self._read_pool is not None:
            self._read_pool.close_all()
            self._read_pool = None

    # ------------------------------------------------------------------
    # Приватные методы публикации событий и EXPLAIN
    # ------------------------------------------------------------------

    def _execute_queries_grouped(
        self,
        conn: sqlite3.Connection,
        queries: list[tuple[str, tuple]],
    ) -> None:
        """Группирует последовательные одинаковые запросы и выполняет
        их через ``executemany``.

        Метод НЕ управляет транзакциями: не вызывает ``commit`` или
        ``rollback``. Управление транзакциями остаётся на вызывающем
        коде (например, в ``execute_write_batched_documents``).

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Инициализация индекса ``i = 0``.                    |
        +---+-----------------------------------------------------+
        | 2 | Пока ``i < len(queries)``:                          |
        |   | a. Взять ``sql, params`` текущего запроса.          |
        |   | b. Собрать в ``batch_params`` все последующие        |
        |   |    запросы с тем же ``sql``.                        |
        |   | c. Если ``len(batch_params) > 1``:                   |
        |   |    - ``conn.executemany(sql, batch_params)``.       |
        |   |    Иначе:                                            |
        |   |    - ``conn.execute(sql, params)``.                 |
        |   | d. Переместить ``i`` на следующую группу.           |
        +---+-----------------------------------------------------+

        Args:
            conn: Соединение SQLite (обычно write-соединение).
            queries: Список кортежей ``(SQL-запрос, параметры)``.
                Ожидается, что запросы для одного документа идут
                последовательно.
        """
        i = 0
        while i < len(queries):
            sql, params = queries[i]
            batch_params = [params]
            j = i + 1
            # Собираем все последующие запросы с тем же SQL
            while j < len(queries) and queries[j][0] == sql:
                batch_params.append(queries[j][1])
                j += 1
            if len(batch_params) > 1:
                conn.executemany(sql, batch_params)
            else:
                conn.execute(sql, params)
            i = j

    def _get_explain_plan(
        self,
        conn: sqlite3.Connection,
        query: str,
        params: tuple,
    ) -> str:
        """Возвращает план выполнения запроса через ``EXPLAIN QUERY PLAN``.

        Метод пытается выполнить EXPLAIN QUERY PLAN для заданного
        запроса с параметрами. В случае ошибки возвращает пустую строку.

        Args:
            conn: соединение SQLite.
            query: исходный запрос.
            params: параметры запроса.

        Returns:
            Строка с планом выполнения или пустая строка при ошибке.
        """
        try:
            cursor = conn.execute(f"EXPLAIN QUERY PLAN {query}", params)
            rows = cursor.fetchall()
            return "\n".join(str(row) for row in rows)
        except Exception:  # noqa: BLE001 — не должны падать из-за EXPLAIN
            return ""

    def _publish_slow_query(
        self,
        query: str,
        duration_ms: float,
        explain_plan: str = "",
    ) -> None:
        """Публикует событие ``DatabaseQuerySlow``, если шина доступна.

        Args:
            query: SQL-запрос (без параметров).
            duration_ms: Длительность выполнения в миллисекундах.
            explain_plan: Результат EXPLAIN QUERY PLAN (может быть пустым).
        """
        if self._event_bus is not None:
            self._event_bus.publish(
                DatabaseQuerySlow(
                    correlation_id="",
                    source="SQLiteAdapter",
                    stage="db",
                    query=query,
                    duration_ms=duration_ms,
                    explain_plan=explain_plan,
                )
            )

    def _publish_db_error(
        self,
        query: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """Публикует событие ``DatabaseError``, если шина доступна.

        Args:
            query: SQL-запрос.
            error_type: Тип исключения.
            error_message: Сообщение об ошибке.
        """
        if self._event_bus is not None:
            self._event_bus.publish(
                DatabaseError(
                    correlation_id="",
                    source="SQLiteAdapter",
                    stage="db",
                    query=query,
                    error_type=error_type,
                    error_message=error_message,
                )
            )
