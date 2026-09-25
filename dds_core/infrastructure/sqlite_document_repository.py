"""
SQLite-реализация read-side доступа к документам.

Модуль предоставляет класс :class:`SqliteDocumentRepository`,
который реализует :class:`~dds_core.domain.interfaces.IDocumentRepository`
поверх SQLite. Единственная точка SQL-запросов к таблицам
``documents`` и ``text_index_fts`` для чтения метаданных и
текстового содержимого.

Архитектурная роль (Фаза 8, ADR-008):

+----------------------------------+----------------------------------+
| Слой                             | Ответственность                  |
+==================================+==================================+
| ``IDocumentRepository`` (domain) | Контракт read-side доступа       |
|                                  | к документам.                    |
+----------------------------------+----------------------------------+
| :class:`SqliteDocumentRepository`| Единственная точка SQL-запросов  |
| (infrastructure, этот модуль)    | к ``documents`` и                |
|                                  | ``text_index_fts``.              |
+----------------------------------+----------------------------------+
| ``TextIndexer``, ``SearchEngine``,| Потребители контракта;           |
| ``DocumentCache`` (application)  | не содержат SQL-строк.           |
+----------------------------------+----------------------------------+

До Фазы 8 SQL-строки для read-операций формировались в
application-слое (``TextIndexer``, ``SearchEngine``,
``DocumentCache``), что нарушало слоистость: детали схемы БД
протекали в прикладной слой. Перенос в infrastructure завершает
работу, начатую ADR-005 для write-side: теперь application-слой
не содержит SQL ни на запись, ни на чтение.

Используется ``IDatabase`` (не ``sqlite3.Connection`` напрямую) —
это позволяет сохранить единый пул соединений и адаптер
событий БД (``DatabaseQuerySlow``, ``DatabaseError``).

Принципы:
    - Реализует ``IDocumentRepository`` (инверсия зависимостей).
    - Единственная точка SQL для read-side документов.
    - Не содержит бизнес-логики.
    - Не выполняет логирования.
    - Потокобезопасен: не хранит изменяемого состояния;
      потокобезопасность операций БД обеспечивается
      ``SQLiteAdapter``.

Реализуемые интерфейсы:
    ``IDocumentRepository`` — абстракция read-side доступа
    к документам.
"""

from __future__ import annotations

from ..domain.interfaces import IDatabase
from ..domain.models import Document, DocumentMetadata

# ----------------------------------------------------------------------
# SQL-шаблоны
# ----------------------------------------------------------------------

_SQL_GET_BY_HASH = "SELECT doc_id FROM documents WHERE file_hash = ?"
"""Поиск документа по хешу файла (SHA-256)."""

_SQL_GET_BY_PATH = "SELECT doc_id FROM documents WHERE file_path = ?"
"""Поиск документа по относительному пути."""

_SQL_GET_METADATA = (
    "SELECT doc_id, file_hash, cached_size, cached_mtime FROM documents WHERE file_path = ?"
)
"""Чтение метаданных для ленивого хеширования."""

_SQL_GET_BY_ID = (
    "SELECT doc_id, file_path, file_hash, file_size, "
    "page_count, indexed_at, last_modified "
    "FROM documents WHERE doc_id = ?"
)
"""Чтение полных метаданных документа по идентификатору."""

_SQL_LOAD_ALL = "SELECT doc_id, file_path, file_hash, cached_size, cached_mtime FROM documents"
"""Загрузка метаданных всех документов одним SELECT-запросом."""

_SQL_GET_PAGE_TEXT = "SELECT text_content FROM text_index_fts WHERE doc_id = ? AND page_number = ?"
"""Чтение текстового содержимого страницы."""


# ----------------------------------------------------------------------
# Реализация IDocumentRepository
# ----------------------------------------------------------------------


class SqliteDocumentRepository:
    """Read-side репозиторий документов поверх SQLite.

    Реализует :class:`IDocumentRepository`. Единственная точка
    SQL-запросов к таблицам ``documents`` и ``text_index_fts``
    для чтения метаданных и текстового содержимого.

    Пример использования::

        db = SQLiteAdapter("dds_database.db")
        repository = SqliteDocumentRepository(db)

        doc_id = repository.get_by_hash("a3f2...")
        metadata = repository.get_metadata("раздел_01/чертёж.pdf")
        document = repository.get_by_id("doc_001")
        all_metadata = repository.load_all()
        text = repository.get_page_text("doc_001", 0)

    Attributes:
        _db: Абстракция базы данных для выполнения SQL-запросов.
    """

    def __init__(self, db: IDatabase) -> None:
        """Инициализирует репозиторий.

        Args:
            db: Реализация ``IDatabase`` для выполнения SQL-запросов.
                В production — ``SQLiteAdapter``.
        """
        self._db = db

    def get_by_hash(self, file_hash: str) -> str | None:
        """Возвращает ``doc_id`` документа с указанным хешем.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``SELECT doc_id FROM documents                       |
        |   | WHERE file_hash = ?``.                              |
        +---+-----------------------------------------------------+
        | 2 | Если строка найдена → возврат ``doc_id``.           |
        +---+-----------------------------------------------------+
        | 3 | Иначе — возврат ``None``.                           |
        +---+-----------------------------------------------------+

        Потокобезопасность:
            Операция чтения; в ``SQLiteAdapter`` не блокируется.

        Args:
            file_hash: Хеш файла (SHA-256).

        Returns:
            ``doc_id`` документа или ``None``.
        """
        rows = self._db.execute(_SQL_GET_BY_HASH, (file_hash,))
        return str(rows[0][0]) if rows else None

    def get_by_path(self, file_path: str) -> str | None:
        """Возвращает ``doc_id`` документа по относительному пути.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``SELECT doc_id FROM documents                       |
        |   | WHERE file_path = ?``.                              |
        +---+-----------------------------------------------------+
        | 2 | Если строка найдена → возврат ``doc_id``.           |
        +---+-----------------------------------------------------+
        | 3 | Иначе — возврат ``None``.                           |
        +---+-----------------------------------------------------+

        Args:
            file_path: Относительный путь к файлу документа.

        Returns:
            ``doc_id`` документа или ``None``.
        """
        rows = self._db.execute(_SQL_GET_BY_PATH, (file_path,))
        return str(rows[0][0]) if rows else None

    def get_metadata(self, file_path: str) -> DocumentMetadata | None:
        """Возвращает метаданные документа для ленивого хеширования.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``SELECT doc_id, file_hash, cached_size,             |
        |   | cached_mtime FROM documents WHERE file_path = ?``.   |
        +---+-----------------------------------------------------+
        | 2 | Если строка найдена → формирование                   |
        |   | :class:`DocumentMetadata` с приведением типов.       |
        +---+-----------------------------------------------------+
        | 3 | Иначе — возврат ``None``.                           |
        +---+-----------------------------------------------------+

        Приведение типов (``str``, ``int``) защищает от возможных
        расхождений между драйвером SQLite и аннотациями модели
        (например, если колонка определена как TEXT, но содержит
        целочисленное значение).

        Args:
            file_path: Относительный путь к файлу документа.

        Returns:
            :class:`DocumentMetadata` или ``None``.
        """
        rows = self._db.execute(_SQL_GET_METADATA, (file_path,))
        if not rows:
            return None
        row = rows[0]
        return DocumentMetadata(
            doc_id=str(row[0]),
            file_hash=str(row[1]),
            cached_size=int(row[2]),
            cached_mtime=str(row[3]),
        )

    def get_by_id(self, doc_id: str) -> Document | None:
        """Возвращает полные метаданные документа по идентификатору.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``SELECT doc_id, file_path, file_hash, file_size,    |
        |   | page_count, indexed_at, last_modified                |
        |   | FROM documents WHERE doc_id = ?``.                  |
        +---+-----------------------------------------------------+
        | 2 | Если строка найдена → формирование :class:`Document`.|
        +---+-----------------------------------------------------+
        | 3 | Иначе — возврат ``None``.                           |
        +---+-----------------------------------------------------+

        Args:
            doc_id: Идентификатор документа.

        Returns:
            :class:`Document` или ``None``.
        """
        rows = self._db.execute(_SQL_GET_BY_ID, (doc_id,))
        if not rows:
            return None
        row = rows[0]
        return Document(
            doc_id=str(row[0]),
            file_path=str(row[1]),
            file_hash=str(row[2]),
            file_size=int(row[3]),
            page_count=int(row[4]),
            indexed_at=str(row[5]),
            last_modified=str(row[6]),
        )

    def load_all(self) -> dict[str, DocumentMetadata]:
        """Загружает метаданные всех документов одним SELECT-запросом.

        Используется :class:`DocumentCache` для предзагрузки
        таблицы ``documents`` в память перед фазой хеширования.
        Позволяет заменить N отдельных SELECT-запросов на один.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``SELECT doc_id, file_path, file_hash, cached_size,  |
        |   | cached_mtime FROM documents``.                       |
        +---+-----------------------------------------------------+
        | 2 | Для каждой строки — формирование                     |
        |   | :class:`DocumentMetadata`.                          |
        +---+-----------------------------------------------------+
        | 3 | Ключ словаря — ``file_path``.                       |
        +---+-----------------------------------------------------+

        Returns:
            Словарь ``{file_path: DocumentMetadata}``. Пустой
            словарь, если таблица ``documents`` пуста.
        """
        rows = self._db.execute(_SQL_LOAD_ALL)
        result: dict[str, DocumentMetadata] = {}
        for row in rows:
            result[str(row[1])] = DocumentMetadata(
                doc_id=str(row[0]),
                file_hash=str(row[2]),
                cached_size=int(row[3]),
                cached_mtime=str(row[4]),
            )
        return result

    def get_page_text(self, doc_id: str, page_number: int) -> str:
        """Возвращает текстовое содержимое страницы документа.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | ``SELECT text_content FROM text_index_fts            |
        |   | WHERE doc_id = ? AND page_number = ?``.              |
        +---+-----------------------------------------------------+
        | 2 | Если строка найдена → возврат текста.               |
        +---+-----------------------------------------------------+
        | 3 | Иначе — возврат пустой строки.                      |
        +---+-----------------------------------------------------+

        Примечание:
            Пустая строка — валидный результат для страниц без
            текстового слоя (сканы без OCR). Не путать с
            отсутствием записи в ``text_index_fts``.

        Args:
            doc_id: Идентификатор документа.
            page_number: Номер страницы (0-based).

        Returns:
            Текст страницы или пустая строка.
        """
        rows = self._db.execute(_SQL_GET_PAGE_TEXT, (doc_id, page_number))
        return str(rows[0][0]) if rows else ""
