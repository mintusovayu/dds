"""
Чистые функции формирования SQL-запросов для индексирования.

Модуль не зависит от IDatabase, IEventBus, TextIndexer.
Используется и в TextIndexer (основной поток), и в
extract_worker (дочерний процесс ProcessPoolExecutor).

Принципы:
- Функция уровня модуля (сериализуема через pickle).
- Не содержит изменяемого состояния.
- Зависит только от ITextExtractor и ITextDocument.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..domain import config
from ..domain.interfaces import ITextExtractor
from .text_normalizer import normalize_text


def build_document_queries(
    doc_id: str,
    file_path: str,
    file_hash: str,
    file_size: int,
    last_modified: str,
    abs_file_path: str,
    text_extractor: ITextExtractor,
) -> list[tuple[str, tuple]] | None:
    """Формирует SQL-запросы для индексирования документа.

    Открывает документ через ``text_extractor``, извлекает
    текст всех страниц и формирует список запросов.
    Не выполняет запись в БД.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Попытка открыть документ через                      |
    |   | ``text_extractor.open_document(abs_file_path)``.     |
    |   | При ошибке возвращается ``None``.                   |
    +---+-----------------------------------------------------+
    | 2 | Получение количества страниц через                  |
    |   | ``document.page_count()``.                          |
    +---+-----------------------------------------------------+
    | 3 | Формирование начальных DELETE-запросов:             |
    |   | a. ``DELETE FROM text_index_fts WHERE doc_id = ?``  |
    |   | b. ``DELETE FROM documents WHERE doc_id = ?``       |
    +---+-----------------------------------------------------+
    | 4 | Формирование INSERT-запроса для метаданных           |
    |   | документа (``INSERT OR REPLACE INTO documents``).   |
    +---+-----------------------------------------------------+
    | 5 | Для каждой страницы от ``0`` до ``page_count - 1``:  |
    |   | a. ``text = document.get_page_text(page_num)``       |
    |   | b. Если ``config.SKIP_EMPTY_TEXT_PAGES`` и текст      |
    |   |    пуст → пропуск страницы.                          |
    |   | c. Добавление INSERT-запроса для FTS, включающего   |
    |   |    оригинальный текст и нормализованную версию.     |
    +---+-----------------------------------------------------+
    | 6 | Возврат списка запросов.                            |
    +---+-----------------------------------------------------+

    Примечание:
        Документ гарантированно закрывается в блоке ``finally``.

    Args:
        doc_id: Уникальный идентификатор документа.
        file_path: Относительный путь к файлу документа.
        file_hash: Хеш файла документа.
        file_size: Размер файла в байтах.
        last_modified: Дата и время последнего изменения файла
            в формате ISO 8601.
        abs_file_path: Абсолютный путь к файлу документа.
        text_extractor: Объект ``ITextExtractor`` для открытия
            документа.

    Returns:
        Список кортежей ``(SQL-запрос, параметры)``, либо ``None``
        при ошибке открытия или извлечения.
    """
    try:
        document = text_extractor.open_document(abs_file_path)
    except Exception:  # noqa: BLE001
        return None

    try:
        page_count = document.page_count()
        queries: list[tuple[str, tuple]] = []

        queries.append(("DELETE FROM text_index_fts WHERE doc_id = ?", (doc_id,)))
        queries.append(("DELETE FROM documents WHERE doc_id = ?", (doc_id,)))

        indexed_at = datetime.now(UTC).isoformat()
        queries.append(
            (
                (
                    "INSERT OR REPLACE INTO documents "
                    "(doc_id, file_path, file_hash, file_size, page_count, "
                    " indexed_at, last_modified, cached_size, cached_mtime) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    doc_id,
                    file_path,
                    file_hash,
                    file_size,
                    page_count,
                    indexed_at,
                    last_modified,
                    file_size,
                    last_modified,
                ),
            )
        )

        for page_num in range(page_count):
            text = document.get_page_text(page_num)
            if config.SKIP_EMPTY_TEXT_PAGES and not text.strip():
                continue
            queries.append(
                (
                    (
                        "INSERT INTO text_index_fts "
                        "(doc_id, page_number, text_content, normalized_text) "
                        "VALUES (?, ?, ?, ?)"
                    ),
                    (doc_id, page_num, text, normalize_text(text)),
                )
            )
        return queries
    except Exception:  # noqa: BLE001
        return None
    finally:
        document.close()
