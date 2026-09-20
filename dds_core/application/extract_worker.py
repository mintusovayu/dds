"""
Рабочая функция извлечения текста для ProcessPoolExecutor.

Функция уровня модуля (сериализуема через pickle).
Выполняется в дочернем процессе пула.

Примечание по архитектуре:
Функция создаёт PyMuPDFTextExtractor внутри процесса,
что является вынужденным нарушением слоистой архитектуры
(application → infrastructure). Это оправдано требованием
сериализуемости для ProcessPoolExecutor: дочерний процесс
не может получить доступ к объектам основного процесса.
"""

from __future__ import annotations

from ..infrastructure.pymupdf_text_extractor import PyMuPDFTextExtractor
from .query_builder import build_document_queries


def extract_document_queries(
    abs_file_path: str,
    doc_id: str,
    relative_path: str,
    file_hash: str,
    file_size: int,
    last_modified: str,
) -> list[tuple[str, tuple]] | None:
    """Извлекает текст документа в дочернем процессе.

    Создаёт экземпляр ``PyMuPDFTextExtractor`` внутри процесса
    и вызывает ``build_document_queries`` для формирования
    SQL-запросов.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Создание экземпляра ``PyMuPDFTextExtractor``.       |
    +---+-----------------------------------------------------+
    | 2 | Вызов ``build_document_queries`` с параметрами.      |
    +---+-----------------------------------------------------+
    | 3 | Возврат списка SQL-запросов или ``None`` при ошибке. |
    +---+-----------------------------------------------------+

    Примечание:
        Функция выполняется в дочернем процессе ``ProcessPoolExecutor``.
        При любой ошибке (открытие, извлечение, формирование запросов)
        возвращается ``None``, а не пробрасывается исключение.

    Args:
        abs_file_path: Абсолютный путь к PDF-файлу.
        doc_id: Идентификатор документа.
        relative_path: Относительный путь файла (от каталога РД).
        file_hash: Хеш файла (SHA-256).
        file_size: Размер файла в байтах.
        last_modified: Дата изменения (ISO 8601).

    Returns:
        Список кортежей ``(SQL-запрос, параметры)``,
        либо ``None`` при ошибке открытия/извлечения.
    """
    try:
        extractor = PyMuPDFTextExtractor()
        return build_document_queries(
            doc_id=doc_id,
            file_path=relative_path,
            file_hash=file_hash,
            file_size=file_size,
            last_modified=last_modified,
            abs_file_path=abs_file_path,
            text_extractor=extractor,
        )
    except Exception:
        return None
