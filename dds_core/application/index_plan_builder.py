"""
Построение плана индексации документа.

Модуль содержит чистую функцию :func:`build_index_plan`, которая
открывает PDF-документ через абстракцию ``ITextExtractor``,
извлекает текстовое содержимое всех страниц, применяет нормализацию
и формирует :class:`DocumentIndexPlan` — доменную модель, описывающую
**что** нужно записать в БД.

Замена ``query_builder.build_document_queries``:

До Фазы 5 функция ``query_builder.build_document_queries``
формировала **SQL-запросы** напрямую в application-слое, что
нарушало слоистость: SQL — специфика конкретного бэкенда БД
(SQLite FTS5), а не доменное правило.

Фаза 5 разделяет ответственность:

+----------------------------------+----------------------------------+
| Слой                             | Ответственность                  |
+==================================+==================================+
| :func:`build_index_plan`         | Открывает PDF, обходит страницы, |
| (application, этот модуль)       | нормализует текст, формирует     |
|                                  | ``DocumentIndexPlan``.           |
+----------------------------------+----------------------------------+
| ``SqliteIndexWriter``            | Преобразует ``DocumentIndexPlan``|
| (infrastructure)                 | в SQL-запросы и выполняет их.    |
+----------------------------------+----------------------------------+

Application-слой больше не содержит SQL-строк, что позволяет
заменить SQLite на другой бэкенд без изменения application-кода
(достаточно реализовать новый ``IIndexWriter``).

Архитектурная роль в конвейере:

+---+-----------------------------------------------------+
| № | Описание                                            |
+===+=====================================================+
| 1 | ``ScanPipeline._extract_worker`` вызывает            |
|   | ``process_runner.run(index_plan_worker, ...)``,      |
|   | где ``index_plan_worker`` =                          |
|   | ``build_index_plan_in_subprocess``                   |
|   | (см. ``dds_core/subprocess_tasks/pdf_workers.py``).  |
+---+-----------------------------------------------------+
| 2 | В subprocess ``build_index_plan_in_subprocess``      |
|   | создаёт ``PyMuPDFTextExtractor`` и вызывает          |
|   | :func:`build_index_plan`.                            |
+---+-----------------------------------------------------+
| 3 | :func:`build_index_plan` открывает PDF, обходит      |
|   | страницы, формирует ``DocumentIndexPlan``.           |
+---+-----------------------------------------------------+
| 4 | План сериализуется через ``pickle`` и возвращается   |
|   | родителю через ``ProcessTaskRunner``.                |
+---+-----------------------------------------------------+
| 5 | ``ScanPipeline`` накапливает планы в буфер и         |
|   | передаёт в ``TextIndexer.write_index_plans``,        |
|   | который делегирует в ``IIndexWriter``.               |
+---+-----------------------------------------------------+

Функция также используется напрямую в ``TextIndexer.prepare_index_plan``
(потоковый fallback, если ``ProcessTaskRunner`` не задан — тесты,
edge-сценарии).

Принципы:
- Функция уровня модуля — pickle-совместима (для вызова из subprocess).
- Не содержит изменяемого состояния.
- Зависит только от абстракций (``ITextExtractor``) и domain-моделей.
- Не выполняет логирования; при ошибке возвращает ``None`` —
  вызывающий код интерпретирует это как «документ не проиндексирован».
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..domain import config
from ..domain.index_plan import DocumentIndexPlan, PageRecord
from ..domain.interfaces import ITextExtractor
from ..domain.text_normalization import normalize_text


def build_index_plan(
    doc_id: str,
    file_path: str,
    file_hash: str,
    file_size: int,
    last_modified: str,
    abs_file_path: str,
    text_extractor: ITextExtractor,
) -> DocumentIndexPlan | None:
    """Формирует план индексации документа.

    Открывает PDF-документ через ``text_extractor``, извлекает
    текстовое содержимое всех страниц, применяет нормализацию
    (``normalize_text``) и формирует ``DocumentIndexPlan``. Не
    выполняет записи в БД — это ответственность ``IIndexWriter``.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Открытие документа через                           |
    |    | ``text_extractor.open_document(abs_file_path)``.   |
    |    | При ошибке — возврат ``None``.                     |
    +----+----------------------------------------------------+
    | 2  | Получение ``page_count`` через                     |
    |    | ``document.page_count()``.                         |
    +----+----------------------------------------------------+
    | 3  | Для каждой страницы от ``0`` до ``page_count-1``:  |
    |    | a. ``text = document.get_page_text(page_num)``.    |
    |    | b. Если ``config.SKIP_EMPTY_TEXT_PAGES`` и текст   |
    |    |    пуст (после ``.strip()``) → пропуск страницы.   |
    |    | c. ``normalized = normalize_text(text)``.          |
    |    | d. Добавление ``PageRecord`` в список.             |
    +----+----------------------------------------------------+
    | 4  | Формирование ``DocumentIndexPlan`` с кортежем      |
    |    | ``pages`` (иммутабельно).                          |
    +----+----------------------------------------------------+
    | 5  | Закрытие документа (``finally``).                  |
    +----+----------------------------------------------------+
    | 6  | Возврат плана.                                     |
    +----+----------------------------------------------------+

    Обработка ошибок:

    - **Открытие документа.** ``FileNotFoundError``, ``OSError``,
      ``RuntimeError`` от PyMuPDF (повреждённый PDF) → ``None``.
    - **Извлечение текста страницы.** Типичные ошибки страницы
      (``ValueError``, ``MemoryError``, ``OSError``) изолированы
      в :meth:`PyMuPDFTextDocument.get_page_text`, который
      возвращает пустую строку. Пустая строка пропускается при
      ``SKIP_EMPTY_TEXT_PAGES=True``; иначе — включается в план
      с пустым текстом.
    - **Критические ошибки PyMuPDF.** ``RuntimeError`` от
      ``_capture_stderr`` (когда PyMuPDF пишет «error» в stderr)
      пробрасывается — план становится ``None``, весь документ
      помечается ошибочным.
    - **Любая другая ошибка** → ``None``.

    Инварианты возвращаемого плана:

    - ``plan.doc_id == doc_id``, ``plan.file_path == file_path``,
      ``plan.file_hash == file_hash``, ``plan.file_size == file_size``,
      ``plan.last_modified == last_modified``.
    - ``plan.page_count == document.page_count()``.
    - ``plan.pages`` — отсортирован по возрастанию ``page_number``
      (порядок обхода страниц).
    - ``len(plan.pages) <= plan.page_count`` (часть страниц может
      быть пропущена при ``SKIP_EMPTY_TEXT_PAGES=True``).
    - Для каждой ``page`` в ``plan.pages``:
      ``page.normalized_text == normalize_text(page.text)``.

    Picklability:
        Функция уровня модуля — сериализуется через ``pickle`` и
        может выполняться в subprocess через ``ProcessTaskRunner``.
        Возвращаемый ``DocumentIndexPlan`` — ``frozen=True``
        dataclass с ``tuple``-полями — также pickle-совместим.

    Args:
        doc_id: Уникальный идентификатор документа (UUID).
            Генерируется в ``ScanPipeline._hash_single_file``.
        file_path: Относительный путь к файлу документа
            (от каталога РД). Записывается в
            ``documents.file_path``.
        file_hash: Хеш файла (SHA-256). Записывается в
            ``documents.file_hash``.
        file_size: Размер файла в байтах. Записывается в
            ``documents.file_size`` и ``documents.cached_size``.
        last_modified: Дата последнего изменения файла в формате
            ISO 8601. Записывается в ``documents.last_modified``
            и ``documents.cached_mtime``.
        abs_file_path: Абсолютный путь к PDF-файлу в файловой
            системе. Используется для открытия документа.
        text_extractor: Объект ``ITextExtractor`` для открытия
            документа. В production — ``PyMuPDFTextExtractor``,
            созданный в subprocess-worker'е или в composition root.

    Returns:
        :class:`DocumentIndexPlan` с метаданными документа и
        кортежем записей страниц (``PageRecord``), либо ``None``
        при любой ошибке открытия или извлечения текста.

    Example:
        ::

            plan = build_index_plan(
                doc_id="abc-123",
                file_path="раздел_01/чертёж.pdf",
                file_hash="a3f2...",
                file_size=102400,
                last_modified="2024-01-15T10:30:00+00:00",
                abs_file_path="/mnt/rd/раздел_01/чертёж.pdf",
                text_extractor=PyMuPDFTextExtractor(),
            )
            if plan is None:
                # Документ помечается как ошибочный
                ...
            else:
                index_writer.write_plan(plan)
    """
    try:
        document = text_extractor.open_document(abs_file_path)
    except Exception:
        return None

    try:
        page_count = document.page_count()
        indexed_at = datetime.now(UTC).isoformat()

        pages: list[PageRecord] = []
        for page_num in range(page_count):
            text = document.get_page_text(page_num)
            if config.SKIP_EMPTY_TEXT_PAGES and not text.strip():
                continue
            pages.append(
                PageRecord(
                    page_number=page_num,
                    text=text,
                    normalized_text=normalize_text(text),
                )
            )

        return DocumentIndexPlan(
            doc_id=doc_id,
            file_path=file_path,
            file_hash=file_hash,
            file_size=file_size,
            last_modified=last_modified,
            page_count=page_count,
            indexed_at=indexed_at,
            pages=tuple(pages),
        )
    except Exception:
        return None
    finally:
        document.close()
