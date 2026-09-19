"""
Управление схемой базы данных DDS.

Этот модуль содержит класс DatabaseManager, который отвечает за
создание и миграцию схемы базы данных ядра DDS. Модуль использует
интерфейс IDatabase для взаимодействия с БД, не зная о конкретной
реализации (SQLite, PostgreSQL и т.д.).

Схема БД ядра DDS включает:

    documents               — метаданные документов.
    text_index_fts          — полнотекстовый индекс (FTS5).
    module_registry         — реестр модулей.
    scan_state              — состояние сканирования.
    schema_version          — версия схемы БД.
    object_reference        — справочник кодов объектов.
    object_reference_alias  — псевдонимы объектов (с языком).
    discipline_reference    — справочник кодов дисциплин.
    discipline_reference_alias — псевдонимы дисциплин (с языком).
    document_type_reference — справочник кодов типов документов.
    document_type_reference_alias — псевдонимы типов документов (с языком).

Проверка возможностей SQLite:

Перед созданием схемы выполняется проверка возможностей SQLite
методом ``_check_sqlite_capabilities``:

1. Версия SQLite должна быть не ниже 3.9 (FTS5 доступен
   начиная с этой версии, а также поддерживает базовые CTE).
2. Интеграционный пробник создаёт FTS5-таблицу в in-memory
   базе, вставляет строку и выполняет запрос с ``MATCH``,
   ``bm25()`` и ``snippet()`` в одном SELECT. Это ловит
   нестандартные сборки SQLite без поддержки FTS5.

Поиск в DDS использует двухзапросный подход (без CTE с FTS5-
функциями), поэтому требование SQLite ≥3.34 из предыдущих версий
снято.

Принципы:
- Модуль зависит от абстракции IDatabase, а не от SQLiteAdapter
  (инверсия зависимостей).
- Модуль не содержит бизнес-логики — только управление схемой.
- Все операции создания таблиц идемпотентны.
- Миграции выполняются в транзакциях.
- SQL-определения таблиц справочников вынесены в отдельный
  приватный метод, чтобы избежать дублирования кода при создании
  новых баз и при миграции существующих.
- Поведение ``ensure_core_schema`` зависит от текущей версии БД:
  если база создаётся впервые (версия 0), справочные таблицы
  создаются сразу с колонкой ``lang`` и записывается актуальная
  версия схемы. Если база уже существует (версия > 0),
  справочные таблицы не создаются, так как их создание и
  обновление выполняется миграциями.

Классы:
    DatabaseManager — управление схемой БД ядра DDS.
"""

from __future__ import annotations

import sqlite3

from ..domain import config
from ..domain.interfaces import IDatabase

# ----------------------------------------------------------------------
# SQL-схемы таблиц ядра DDS
# ----------------------------------------------------------------------

_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    description TEXT
)
"""

_SCHEMA_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    file_size INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    cached_size INTEGER NOT NULL DEFAULT 0,
    cached_mtime TEXT NOT NULL DEFAULT '',
    object_code TEXT,
    discipline_code TEXT,
    document_type_code TEXT,
    unmatched_flag INTEGER NOT NULL DEFAULT 0
)
"""

_SCHEMA_DOCUMENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_documents_file_hash
ON documents (file_hash)
"""

_SCHEMA_DOCUMENTS_PATH_INDEX = """
CREATE INDEX IF NOT EXISTS idx_documents_file_path
ON documents (file_path)
"""

# Индексы для полей фильтрации
_SCHEMA_DOCUMENTS_OBJECT_CODE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_documents_object_code
ON documents (object_code)
"""

_SCHEMA_DOCUMENTS_DISCIPLINE_CODE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_documents_discipline_code
ON documents (discipline_code)
"""

_SCHEMA_DOCUMENTS_DOCUMENT_TYPE_CODE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_documents_document_type_code
ON documents (document_type_code)
"""

_SCHEMA_DOCUMENTS_UNMATCHED_FLAG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_documents_unmatched_flag
ON documents (unmatched_flag)
"""

# Токенизатор FTS5 берётся из конфигурации
_SCHEMA_TEXT_INDEX_FTS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS text_index_fts USING fts5(
    doc_id,
    page_number,
    text_content,
    normalized_text,
    tokenize='{config.FTS_TOKENIZER}'
)
"""

_SCHEMA_MODULE_REGISTRY = """
CREATE TABLE IF NOT EXISTS module_registry (
    module_name TEXT PRIMARY KEY,
    version TEXT NOT NULL DEFAULT '',
    api_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'discovered',
    scan_phase TEXT NOT NULL DEFAULT 'secondary',
    description TEXT NOT NULL DEFAULT '',
    managed_tables TEXT NOT NULL DEFAULT '[]',
    data_version INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    initialized_at TEXT NOT NULL DEFAULT ''
)
"""

_SCHEMA_SCAN_STATE = """
CREATE TABLE IF NOT EXISTS scan_state (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL DEFAULT 'primary',
    status TEXT NOT NULL DEFAULT 'idle',
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    total_files INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    current_file TEXT NOT NULL DEFAULT '',
    errors INTEGER NOT NULL DEFAULT 0
)
"""

# ----------------------------------------------------------------------
# Схемы справочников (коды и псевдонимы с языком)
# ----------------------------------------------------------------------

_SCHEMA_OBJECT_REFERENCE = """
CREATE TABLE IF NOT EXISTS object_reference (
    code TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
)
"""

_SCHEMA_OBJECT_REFERENCE_ALIAS = """
CREATE TABLE IF NOT EXISTS object_reference_alias (
    alias TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    lang TEXT,
    FOREIGN KEY (code) REFERENCES object_reference(code) ON DELETE CASCADE
)
"""

_SCHEMA_OBJECT_REFERENCE_ALIAS_CODE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_object_reference_alias_code
ON object_reference_alias (code)
"""

_SCHEMA_DISCIPLINE_REFERENCE = """
CREATE TABLE IF NOT EXISTS discipline_reference (
    code TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
)
"""

_SCHEMA_DISCIPLINE_REFERENCE_ALIAS = """
CREATE TABLE IF NOT EXISTS discipline_reference_alias (
    alias TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    lang TEXT,
    FOREIGN KEY (code) REFERENCES discipline_reference(code) ON DELETE CASCADE
)
"""

_SCHEMA_DISCIPLINE_REFERENCE_ALIAS_CODE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_discipline_reference_alias_code
ON discipline_reference_alias (code)
"""

_SCHEMA_DOCUMENT_TYPE_REFERENCE = """
CREATE TABLE IF NOT EXISTS document_type_reference (
    code TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
)
"""

_SCHEMA_DOCUMENT_TYPE_REFERENCE_ALIAS = """
CREATE TABLE IF NOT EXISTS document_type_reference_alias (
    alias TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    lang TEXT,
    FOREIGN KEY (code) REFERENCES document_type_reference(code) ON DELETE CASCADE
)
"""

_SCHEMA_DOCUMENT_TYPE_REFERENCE_ALIAS_CODE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_document_type_reference_alias_code
ON document_type_reference_alias (code)
"""

# ----------------------------------------------------------------------
# Схемы таблиц псевдонимов без колонки lang (для миграции версии 4)
# ----------------------------------------------------------------------

_SCHEMA_OBJECT_REFERENCE_ALIAS_NO_LANG = """
CREATE TABLE IF NOT EXISTS object_reference_alias (
    alias TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    FOREIGN KEY (code) REFERENCES object_reference(code) ON DELETE CASCADE
)
"""

_SCHEMA_DISCIPLINE_REFERENCE_ALIAS_NO_LANG = """
CREATE TABLE IF NOT EXISTS discipline_reference_alias (
    alias TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    FOREIGN KEY (code) REFERENCES discipline_reference(code) ON DELETE CASCADE
)
"""

_SCHEMA_DOCUMENT_TYPE_REFERENCE_ALIAS_NO_LANG = """
CREATE TABLE IF NOT EXISTS document_type_reference_alias (
    alias TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    FOREIGN KEY (code) REFERENCES document_type_reference(code) ON DELETE CASCADE
)
"""

# Текущая версия схемы БД ядра DDS.
# Увеличивается при каждом изменении схемы.
#
# История версий:
#   1 — начальная схема БД ядра DDS.
#   2 — добавлены поля cached_size и cached_mtime в таблицу
#       documents для поддержки ленивого хеширования (Фаза 2).
#   3 — добавлены поля object_code, discipline_code,
#       document_type_code и unmatched_flag для фильтрации
#       по метаданным имени файла.
#   4 — добавлены таблицы справочников (коды и псевдонимы)
#       для объектов, дисциплин и типов документов.
#   5 — в таблицы псевдонимов добавлен столбец lang для
#       разделения псевдонимов по языкам (en, ru, NULL).
#   6 — в таблицу text_index_fts добавлена колонка normalized_text
#       для нечувствительного к раскладке поиска. Индекс пересоздаётся.
CURRENT_SCHEMA_VERSION: int = 6

# Минимальная версия SQLite. Начиная с 3.9 доступен FTS5, который
# используется для полнотекстового поиска. Двухзапросный подход в
# поисковом бэкенде (см. ``fts5_search_backend.py``) не требует
# CTE с FTS5-функциями, поэтому требование SQLite ≥3.34 (для
# предыдущей реализации через CTE) снято.
_MIN_SQLITE_VERSION: tuple[int, int, int] = (3, 9, 0)


# ----------------------------------------------------------------------
# Управление схемой БД
# ----------------------------------------------------------------------
class DatabaseManager:
    """Управление схемой базы данных ядра DDS.

    Создаёт таблицы ядра DDS, применяет миграции и обеспечивает
    целостность схемы. Все операции идемпотентны: повторный вызов
    не создаёт дубликатов и не изменяет существующие данные.

    Перед созданием схемы выполняется проверка возможностей SQLite
    (см. ``_check_sqlite_capabilities``).

    Пример использования::

        manager = DatabaseManager(db_adapter)
        manager.ensure_core_schema()
        manager.ensure_fts_schema()

    Attributes:
        _db: Интерфейс доступа к базе данных.
    """

    def __init__(self, db: IDatabase) -> None:
        """Инициализирует менеджер схемы БД.

        Args:
            db: Реализация IDatabase для взаимодействия с БД.
        """
        self._db = db

    # ------------------------------------------------------------------
    # Проверка возможностей SQLite
    # ------------------------------------------------------------------

    def _check_sqlite_capabilities(self) -> None:
        """Проверяет минимальные возможности SQLite.

        Выполняет две проверки:

        1. Версия SQLite должна быть не ниже 3.9 (FTS5 доступен
           начиная с этой версии).
        2. Интеграционный пробник создаёт FTS5-таблицу в in-memory
           базе, вставляет строку и выполняет запрос с ``MATCH``,
           ``bm25()`` и ``snippet()`` в одном SELECT. Это ловит
           нестандартные сборки SQLite без поддержки FTS5.

        Проверка FTS5+CTE из предыдущих версий удалена: поиск
        в DDS использует двухзапросный подход без CTE с FTS5-
        функциями.

        Raises:
            RuntimeError: Если хотя бы одна из проверок не пройдена.
                Сообщение содержит описание проблемы и подсказку
                по устранению.
        """
        # Шаг 1: проверка версии SQLite.
        if sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION:
            raise RuntimeError(
                f"Требуется SQLite >= "
                f"{'.'.join(str(x) for x in _MIN_SQLITE_VERSION)} "
                f"(FTS5). Установлена версия {sqlite3.sqlite_version}. "
                f"Обновите SQLite или используйте сборку с поддержкой "
                f"FTS5."
            )

        # Шаг 2: интеграционный пробник FTS5 в in-memory БД.
        # Создаём временную базу, чтобы не затрагивать основную.
        # FTS5-функции bm25() и snippet() вызываются в том же
        # SELECT-блоке, где присутствует MATCH — это соответствует
        # документированному поведению SQLite.
        try:
            probe_conn = sqlite3.connect(":memory:")
            try:
                probe_conn.execute("CREATE VIRTUAL TABLE _probe_fts USING fts5(x)")
                probe_conn.execute("INSERT INTO _probe_fts(x) VALUES ('hello')")
                probe_conn.execute(
                    "SELECT x, "
                    "       bm25(_probe_fts) AS rank, "
                    "       snippet(_probe_fts, 0, '[', ']', '...', 5) AS s "
                    "FROM _probe_fts "
                    "WHERE _probe_fts MATCH 'hello'"
                ).fetchall()
            finally:
                probe_conn.close()
        except sqlite3.Error as e:
            raise RuntimeError(
                f"SQLite не поддерживает FTS5. Требуется SQLite >= "
                f"{'.'.join(str(x) for x in _MIN_SQLITE_VERSION)} "
                f"со сборкой FTS5. Ошибка: {e}"
            ) from e

    # ------------------------------------------------------------------
    # Создание схемы справочников
    # ------------------------------------------------------------------

    def _get_reference_tables_sql(self, include_lang: bool = True) -> list[str]:
        """Возвращает список SQL-запросов для создания справочников.

        Параметр ``include_lang`` определяет, будет ли таблица
        псевдонимов содержать колонку ``lang``. При ``True``
        (по умолчанию) используется для создания новой базы данных
        в :meth:`ensure_core_schema`. При ``False`` — для миграции
        версии 4, когда таблицы создаются без ``lang``, чтобы потом
        миграция версии 5 добавила его через ``ALTER TABLE``.

        Args:
            include_lang: Если ``True``, используются схемы таблиц
                псевдонимов с колонкой ``lang``. Если ``False`` —
                без неё.

        Returns:
            Список SQL-строк.
        """
        if include_lang:
            return [
                _SCHEMA_OBJECT_REFERENCE,
                _SCHEMA_OBJECT_REFERENCE_ALIAS,
                _SCHEMA_OBJECT_REFERENCE_ALIAS_CODE_INDEX,
                _SCHEMA_DISCIPLINE_REFERENCE,
                _SCHEMA_DISCIPLINE_REFERENCE_ALIAS,
                _SCHEMA_DISCIPLINE_REFERENCE_ALIAS_CODE_INDEX,
                _SCHEMA_DOCUMENT_TYPE_REFERENCE,
                _SCHEMA_DOCUMENT_TYPE_REFERENCE_ALIAS,
                _SCHEMA_DOCUMENT_TYPE_REFERENCE_ALIAS_CODE_INDEX,
            ]
        else:
            return [
                _SCHEMA_OBJECT_REFERENCE,
                _SCHEMA_OBJECT_REFERENCE_ALIAS_NO_LANG,
                _SCHEMA_OBJECT_REFERENCE_ALIAS_CODE_INDEX,
                _SCHEMA_DISCIPLINE_REFERENCE,
                _SCHEMA_DISCIPLINE_REFERENCE_ALIAS_NO_LANG,
                _SCHEMA_DISCIPLINE_REFERENCE_ALIAS_CODE_INDEX,
                _SCHEMA_DOCUMENT_TYPE_REFERENCE,
                _SCHEMA_DOCUMENT_TYPE_REFERENCE_ALIAS_NO_LANG,
                _SCHEMA_DOCUMENT_TYPE_REFERENCE_ALIAS_CODE_INDEX,
            ]

    # ------------------------------------------------------------------
    # Создание основной схемы
    # ------------------------------------------------------------------

    def ensure_core_schema(self) -> None:
        """Создаёт основные таблицы ядра DDS, если они не существуют.

        Создаёт базовые таблицы (documents, module_registry, scan_state,
        schema_version) и, в зависимости от текущей версии БД, справочные
        таблицы.

        Логика создания справочных таблиц:
        - Если база создаётся впервые (текущая версия схемы = 0),
          справочные таблицы создаются сразу с колонкой ``lang``,
          после чего записывается актуальная версия схемы
          (``CURRENT_SCHEMA_VERSION``).
        - Если база уже существует (версия > 0), справочные таблицы
          не создаются; их создание и обновление выполняется
          миграциями в :meth:`migrate`.

        Этот метод идемпотентен: повторный вызов не создаёт
        дубликатов таблиц и не изменяет существующие данные.
        """
        self._db.ensure_table("schema_version", _SCHEMA_VERSION_TABLE)
        self._db.ensure_table("documents", _SCHEMA_DOCUMENTS)
        self._db.execute_write(_SCHEMA_DOCUMENTS_INDEX)
        self._db.execute_write(_SCHEMA_DOCUMENTS_PATH_INDEX)
        self._db.execute_write(_SCHEMA_DOCUMENTS_OBJECT_CODE_INDEX)
        self._db.execute_write(_SCHEMA_DOCUMENTS_DISCIPLINE_CODE_INDEX)
        self._db.execute_write(_SCHEMA_DOCUMENTS_DOCUMENT_TYPE_CODE_INDEX)
        self._db.execute_write(_SCHEMA_DOCUMENTS_UNMATCHED_FLAG_INDEX)
        self._db.ensure_table("module_registry", _SCHEMA_MODULE_REGISTRY)
        self._db.ensure_table("scan_state", _SCHEMA_SCAN_STATE)

        # Определяем текущую версию схемы
        existing = self._db.execute("SELECT MAX(version) FROM schema_version")
        current_version = existing[0][0] if existing and existing[0][0] else 0

        if current_version == 0:
            # Новая база: создаём справочные таблицы с lang
            for sql in self._get_reference_tables_sql(include_lang=True):
                self._db.execute_write(sql)

            self._db.execute_write(
                "INSERT INTO schema_version (version, applied_at, description) "
                "VALUES (?, datetime('now'), ?)",
                (CURRENT_SCHEMA_VERSION, "Начальная схема БД ядра DDS"),
            )
        # else: существующая база — справочные таблицы будут созданы/обновлены миграциями

    def ensure_fts_schema(self) -> None:
        """Создаёт полнотекстовый индекс FTS5, если он не существует.

        Создаёт виртуальную таблицу ``text_index_fts`` для
        полнотекстового поиска по содержимому страниц документов.
        Используется токенизатор, заданный в ``config.FTS_TOKENIZER``
        (по умолчанию ``unicode61`` для поддержки кириллицы).

        Этот метод идемпотентен: повторный вызов не создаёт
        дубликатов таблицы и не изменяет существующие данные.
        """
        self._db.execute_write(_SCHEMA_TEXT_INDEX_FTS)

    def get_schema_version(self) -> int:
        """Возвращает текущую версию схемы БД.

        Если таблица ``schema_version`` не существует или пуста,
        возвращается 0. Это означает, что схема БД ещё не создана.

        Returns:
            Версия схемы БД. 0, если таблица ``schema_version``
            не существует или пуста.
        """
        if not self._db.table_exists("schema_version"):
            return 0
        results = self._db.execute("SELECT MAX(version) FROM schema_version")
        if results and results[0][0] is not None:
            return int(results[0][0])
        return 0

    def migrate(self) -> None:
        """Применяет миграции схемы БД ядра DDS.

        Сравнивает текущую версию схемы в БД с ``CURRENT_SCHEMA_VERSION``
        и применяет необходимые миграции. Каждая миграция выполняется
        в транзакции (для версии 6 — атомарно через ``execute_write_many``).

        Если миграция не удалась, транзакция откатывается,
        и метод выбрасывает исключение.

        Миграции:

        Версия 2 (Фаза 2 — ленивое хеширование):
            Добавляет поля ``cached_size`` и ``cached_mtime``
            в таблицу ``documents``.

        Версия 3 (Фаза 3 — фильтрация по метаданным имени файла):
            Добавляет поля ``object_code``, ``discipline_code``,
            ``document_type_code`` и ``unmatched_flag`` в таблицу
            ``documents``, а также создаёт индексы для этих полей.

        Версия 4 (Справочники для фильтрации):
            Создаёт таблицы справочников и их псевдонимов
            для объектов, дисциплин и типов документов. Таблицы
            псевдонимов создаются БЕЗ колонки ``lang``.

        Версия 5 (Языковая поддержка псевдонимов):
            Добавляет столбец ``lang`` в таблицы псевдонимов
            ``object_reference_alias``, ``discipline_reference_alias``,
            ``document_type_reference_alias``. Значение ``NULL``
            остаётся у существующих записей, чтобы не терять данные.

        Версия 6 (Нормализация текста для поиска):
            Пересоздаёт таблицу ``text_index_fts`` с добавленной
            колонкой ``normalized_text``. Старый индекс удаляется,
            новый создаётся с обновлённой схемой.

        Raises:
            Exception: Если миграция не удалась. Транзакция откатывается.
        """
        current = self.get_schema_version()
        if current >= CURRENT_SCHEMA_VERSION:
            return

        migrations: list[tuple[int, list[str]]] = [
            # Версия 1: начальная схема (создаётся в ensure_core_schema).
            # Явная миграция не требуется.
            #
            # Версия 2 (Фаза 2): ленивое хеширование.
            (
                2,
                [
                    "ALTER TABLE documents ADD COLUMN cached_size INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE documents ADD COLUMN cached_mtime TEXT NOT NULL DEFAULT ''",
                ],
            ),
            # Версия 3 (Фаза 3): фильтрация по метаданным имени файла.
            (
                3,
                [
                    "ALTER TABLE documents ADD COLUMN object_code TEXT",
                    "ALTER TABLE documents ADD COLUMN discipline_code TEXT",
                    "ALTER TABLE documents ADD COLUMN document_type_code TEXT",
                    "ALTER TABLE documents ADD COLUMN unmatched_flag INTEGER NOT NULL DEFAULT 0",
                    (
                        "CREATE INDEX IF NOT EXISTS idx_documents_object_code "
                        "ON documents (object_code)"
                    ),
                    (
                        "CREATE INDEX IF NOT EXISTS idx_documents_discipline_code "
                        "ON documents (discipline_code)"
                    ),
                    (
                        "CREATE INDEX IF NOT EXISTS idx_documents_document_type_code "
                        "ON documents (document_type_code)"
                    ),
                    (
                        "CREATE INDEX IF NOT EXISTS idx_documents_unmatched_flag "
                        "ON documents (unmatched_flag)"
                    ),
                ],
            ),
            # Версия 4: справочники для фильтрации.
            # Таблицы псевдонимов создаются без колонки lang.
            (4, self._get_reference_tables_sql(include_lang=False)),
            # Версия 5: добавляем столбец lang в таблицы псевдонимов.
            (
                5,
                [
                    "ALTER TABLE object_reference_alias ADD COLUMN lang TEXT",
                    "ALTER TABLE discipline_reference_alias ADD COLUMN lang TEXT",
                    "ALTER TABLE document_type_reference_alias ADD COLUMN lang TEXT",
                ],
            ),
            # Версия 6: нормализованная колонка в FTS.
            # Выполняется атомарно: DROP и CREATE в одной транзакции.
            (
                6,
                [
                    "DROP TABLE IF EXISTS text_index_fts",
                    _SCHEMA_TEXT_INDEX_FTS.strip(),
                ],
            ),
        ]

        for target_version, sql_statements in migrations:
            if target_version <= current:
                continue

            if target_version == 6:
                # Атомарное выполнение DROP и CREATE.
                self._db.execute_write_many([(sql, ()) for sql in sql_statements])
            else:
                for sql in sql_statements:
                    self._db.execute_write(sql)

            self._db.execute_write(
                "INSERT INTO schema_version (version, applied_at, description) "
                "VALUES (?, datetime('now'), ?)",
                (target_version, f"Миграция схемы до версии {target_version}"),
            )

    def ensure_all(self) -> None:
        """Создаёт полную схему БД ядра DDS.

        Порядок операций:

        1. Проверка возможностей SQLite
           (``_check_sqlite_capabilities``) — до создания схемы,
           чтобы не начинать работу в несовместимом окружении.
        2. Создание основной схемы (``ensure_core_schema``).
        3. Создание полнотекстового индекса (``ensure_fts_schema``).
        4. Применение миграций (``migrate``).

        Этот метод идемпотентен: повторный вызов не создаёт
        дубликатов и не изменяет существующие данные.

        Raises:
            RuntimeError: Если возможности SQLite не удовлетворяют
                минимальным требованиям (см.
                ``_check_sqlite_capabilities``).
        """
        self._check_sqlite_capabilities()
        self.ensure_core_schema()
        self.ensure_fts_schema()
        self.migrate()
