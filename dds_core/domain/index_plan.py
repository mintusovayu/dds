"""
Доменная модель плана индексации документа.

Модуль определяет структуры данных, описывающие **что** нужно
записать в БД при индексации документа. SQL-специфика (имена таблиц,
колонок, синтаксис запросов) вынесена в infrastructure-реализацию
``IIndexWriter``.

Разделение ответственности:

+----------------------------------+----------------------------------+
| Слой                             | Ответственность                  |
+==================================+==================================+
| ``application``                  | Формирует ``DocumentIndexPlan``: |
|                                  | открывает PDF, извлекает текст,  |
|                                  | нормализует, пропускает пустые   |
|                                  | страницы.                        |
+----------------------------------+----------------------------------+
| ``infrastructure``               | Преобразует ``DocumentIndexPlan``|
|                                  | в SQL-запросы конкретного        |
|                                  | бэкенда (SQLite FTS5) и          |
|                                  | выполняет их.                    |
+----------------------------------+----------------------------------+

До Фазы 5 ``application.query_builder.build_document_queries``
формировал SQL-строки напрямую, что нарушало слоистость: SQL —
специфика SQLite, а не доменное правило. ``DocumentIndexPlan``
устраняет эту протечку.

Инварианты:

- ``page_count >= 0`` — количество страниц в исходном PDF.
- ``file_size >= 0`` — размер файла в байтах.
- ``len(pages) <= page_count`` — план может не содержать записей
  для пустых страниц (при ``SKIP_EMPTY_TEXT_PAGES=True``).
- ``pages`` отсортирован по возрастанию ``page_number``.
- ``doc_id``, ``file_path``, ``file_hash`` — непустые строки.

Принципы:
- Модели — чистые структуры данных без бизнес-логики.
- Модели иммутабельны (``frozen=True``) — безопасны для передачи
  между слоями и через ``pickle`` в subprocess.
- Модели не зависят от инфраструктурных компонентов.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageRecord:
    """Запись одной страницы документа для индексации.

    Соответствует одной строке в таблице ``text_index_fts``
    (колонки ``page_number``, ``text_content``, ``normalized_text``).

    Страницы без текстового слоя (сканы без OCR, пустые страницы)
    могут отсутствовать в ``DocumentIndexPlan.pages`` — это
    контролируется флагом ``config.SKIP_EMPTY_TEXT_PAGES`` на этапе
    формирования плана.

    Attributes:
        page_number: Номер страницы (0-based). Соответствует
            ``text_index_fts.page_number``.
        text: Оригинальный текст страницы (как извлечён из PDF).
            Сохраняется в колонку ``text_content``.
        normalized_text: Нормализованная версия текста для поиска,
            нечувствительного к раскладке клавиатуры. Получается
            функцией
            :func:`~dds_core.domain.text_normalization.normalize_text`.
            Сохраняется в колонку ``normalized_text``.

    Примечание:
        Dataclass помечен ``frozen=True`` — экземпляры иммутабельны.
        Это позволяет безопасно передавать план через слои и
        сериализовать через ``pickle`` без риска модификации.
    """

    page_number: int
    text: str
    normalized_text: str


@dataclass(frozen=True)
class DocumentIndexPlan:
    """План индексации одного документа.

    Содержит все данные, необходимые для записи документа в БД:
    метаданные (``doc_id``, ``file_path``, ``file_hash``, ...) и
    список страниц с текстом.

    Формируется в ``application``-слое функцией
    :func:`~dds_core.application.index_plan_builder.build_index_plan`.
    Передаётся в ``infrastructure``-слой, где реализация
    :class:`~dds_core.domain.index_writer.IIndexWriter` преобразует
    план в SQL-запросы и выполняет их.

    Жизненный цикл:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | ``build_index_plan`` (application) открывает PDF     |
    |   | через ``ITextExtractor``, обходит страницы,          |
    |   | формирует ``DocumentIndexPlan``.                     |
    +---+-----------------------------------------------------+
    | 2 | В subprocess (через ``ProcessTaskRunner``) план      |
    |   | сериализуется через ``pickle`` и передаётся родителю.|
    +---+-----------------------------------------------------+
    | 3 | ``IIndexWriter.write_plan`` (или                     |
    |   | ``write_plans_batch``) преобразует план в            |
    |   | SQL-запросы и выполняет их.                          |
    +---+-----------------------------------------------------+

    Attributes:
        doc_id: Уникальный идентификатор документа (UUID).
            Соответствует ``documents.doc_id``.
        file_path: Относительный путь к файлу (от каталога РД).
            Соответствует ``documents.file_path``.
        file_hash: Хеш файла (SHA-256). Соответствует
            ``documents.file_hash``.
        file_size: Размер файла в байтах. Соответствует
            ``documents.file_size`` и ``documents.cached_size``.
        last_modified: Дата последнего изменения файла в формате
            ISO 8601. Соответствует ``documents.last_modified`` и
            ``documents.cached_mtime``.
        page_count: Общее количество страниц в PDF. Соответствует
            ``documents.page_count``. Может быть больше
            ``len(pages)``, если часть страниц пуста и пропущена
            при ``SKIP_EMPTY_TEXT_PAGES=True``.
        indexed_at: Дата и время индексации в формате ISO 8601.
            Соответствует ``documents.indexed_at``.
        pages: Кортеж записей страниц. Отсортирован по возрастанию
            ``page_number``. Может быть пустым, если все страницы
            документа пусты.

    Примечание:
        ``pages`` — кортеж (``tuple``), а не список. Это обеспечивает
        иммутабельность (нельзя случайно модифицировать план после
        создания), hashability (план можно использовать как ключ
        словаря) и корректную pickle-сериализацию без модификаций.

    Примечание:
        Dataclass помечен ``frozen=True`` — экземпляры иммутабельны.
        Изменения плана (например, при переиндексации) создают новый
        экземпляр, а не мутируют существующий.

    Example:
        ::

            plan = DocumentIndexPlan(
                doc_id="abc-123",
                file_path="раздел_01/чертёж.pdf",
                file_hash="a3f2...",
                file_size=102400,
                last_modified="2024-01-15T10:30:00+00:00",
                page_count=5,
                indexed_at="2024-01-15T11:00:00+00:00",
                pages=(
                    PageRecord(page_number=0, text="...", normalized_text="..."),
                    PageRecord(page_number=2, text="...", normalized_text="..."),
                ),
            )
    """

    doc_id: str
    file_path: str
    file_hash: str
    file_size: int
    last_modified: str
    page_count: int
    indexed_at: str
    pages: tuple[PageRecord, ...]
