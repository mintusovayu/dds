"""
Worker-функции для PyMuPDF-операций в subprocess.

Модуль содержит picklable-функции уровня модуля, которые выполняются
в дочерних процессах через
:class:`~dds_core.infrastructure.process_task_runner.ProcessTaskRunner`.
Каждая функция создаёт необходимые инфраструктурные зависимости
локально — это единственный способ передать их в изолированный процесс,
поскольку открытые ресурсы (файловые дескрипторы, соединения с БД,
event loop) не сериализуются через ``pickle``.

Архитектурная роль: composition root пакета
-------------------------------------------

Пакет ``dds_core.subprocess_tasks`` — легальное исключение из общего
правила «application не импортирует infrastructure». Здесь разрешено
импортировать и ``application`` (для вызова бизнес-логики), и
``infrastructure`` (для создания экстрактора PyMuPDF). Это зафиксировано
контрактами ``subprocess-tasks-isolation`` и ``subprocess-tasks-shallow``
в ``docs/architecture-decisions/exceptions.yaml`` и обосновано в ADR-004.

Ключевое ограничение: импортировать ``dds_core.subprocess_tasks`` можно
**только** из ``dds_web.lifespan`` (главный composition root).
Из ``application`` или ``infrastructure`` — запрещено. Это гарантирует,
что worker-функции не «протекут» в середину слоёв.

Принципы
--------

- **Функции уровня модуля.** ``extract_document_queries`` должна быть
  определена на уровне модуля (не как вложенная функция), чтобы
  сериализоваться через ``pickle``. Локальные функции и лямбды
  не picklable.
- **Picklable-аргументы.** Все аргументы — примитивы (str, int) или
  стандартные структуры (list, dict). Открытые файлы, соединения,
  event loop, dataclass-со-ссылками не передаются.
- **Picklable-результат.** Возвращаемое значение (список кортежей
  ``(SQL, params)``) сериализуется через ``pickle`` без модификаций.
  ``None`` при ошибке также pickle-совместим.
- **Локальное создание зависимостей.** ``PyMuPDFTextExtractor``
  создаётся внутри функции, а не передаётся через аргумент.
- **Обработка ошибок — не пробрасывать.** Worker не должен поднимать
  исключение наружу, если это приведёт к падению дочернего процесса
  в неконтролируемом месте. Возврат ``None`` — сигнал ошибки,
  которую ``ScanPipeline`` обработает как «файл помечен ошибочным».

Отличие от предыдущей реализации
--------------------------------

Ранее функция ``extract_document_queries`` жила в
``dds_core/application/extract_worker.py``. Модуль импортировал
``PyMuPDFTextExtractor`` из ``infrastructure``, что нарушало
контракт ``application-isolation`` и требовало временного исключения
в ``exceptions.yaml``:

.. code-block:: yaml

    ignore_imports:
      - >-
          dds_core.application.extract_worker -> dds_core.infrastructure.pymupdf_text_extractor

В Фазе 4 функция перенесена в ``subprocess_tasks/pdf_workers.py``
(composition root), а ``application/extract_worker.py`` удалён.
Временное исключение из ``exceptions.yaml`` снято. Контракт
``application-isolation`` снова чистый.

Ссылки
------

- ``docs/architecture-decisions/ADR-004-process-task-runner.md`` —
  обоснование process-per-task и forkserver-контекста.
- ``docs/architecture-decisions/exceptions.yaml`` — контракты
  ``subprocess-tasks-isolation`` и ``subprocess-tasks-shallow``.
- ``dds_core/infrastructure/process_task_runner.py`` — вызывающий
  компонент.
- ``dds_core/application/query_builder.py`` — ``build_document_queries``,
  бизнес-логика извлечения текста и формирования SQL-запросов
  (в Фазе 5 будет заменена на ``build_index_plan``).
"""

from __future__ import annotations

from ..application.query_builder import build_document_queries
from ..infrastructure.pymupdf_text_extractor import PyMuPDFTextExtractor


def extract_document_queries(
    abs_file_path: str,
    doc_id: str,
    relative_path: str,
    file_hash: str,
    file_size: int,
    last_modified: str,
) -> list[tuple[str, tuple]] | None:
    """Извлекает текст документа в дочернем процессе.

    Функция уровня модуля — сериализуется через ``pickle`` и выполняется
    в изолированном процессе, созданном ``ProcessTaskRunner`` (контекст
    ``forkserver``). Локально создаёт ``PyMuPDFTextExtractor`` и вызывает
    ``build_document_queries`` для формирования SQL-запросов
    индексирования.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Создание экземпляра ``PyMuPDFTextExtractor``       |
    |    | локально в дочернем процессе.                      |
    +----+----------------------------------------------------+
    | 2  | Вызов ``build_document_queries`` с параметрами     |
    |    | документа.                                         |
    +----+----------------------------------------------------+
    | 3  | Возврат списка SQL-запросов.                       |
    +----+----------------------------------------------------+
    | 4  | При любом исключении — возврат ``None``.           |
    +----+----------------------------------------------------+

    Поведение при ошибках:

    - Открытие файла может упасть (``FileNotFoundError``,
      ``OSError``, ``RuntimeError`` от PyMuPDF).
    - Извлечение текста страницы может упасть (``RuntimeError``
      при ошибке PyMuPDF в stderr).
    - Формирование запросов может упасть (любая ошибка в
      ``build_document_queries``).

    Во всех случаях функция возвращает ``None`` — это pickle-совместимый
    сигнал ошибки. ``ScanPipeline`` интерпретирует его как «документ
    не удалось проиндексировать» и публикует событие
    ``FileProcessingFailed``.

    **Почему не пробрасывается исключение.** Если worker поднимет
    исключение, оно сериализуется и передаётся родителю через IPC.
    Это сработало бы, но добавляет сложность (обработка
    ``BaseException``, потеря traceback) и лишает ``ScanPipeline``
    единообразия: сейчас и ``extract_document_queries``, и
    ``TextIndexer.prepare_document_queries`` (thread-fallback)
    сигнализируют об ошибке одним и тем же способом — ``None``.

    Потокобезопасность:

    - Функция не хранит состояния между вызовами.
    - Каждый вызов получает собственный ``PyMuPDFTextExtractor``
      и, следовательно, собственный ``fitz.Document``.
    - Внутри одного дочернего процесса функция выполняется
      в единственном потоке.

    Args:
        abs_file_path: Абсолютный путь к PDF-файлу в файловой
            системе родительского процесса. В дочернем процессе
            (forkserver) путь доступен, поскольку рабочая
            директория и файловая система общие.
        doc_id: Идентификатор документа в БД. Используется в
            SQL-запросах индексирования.
        relative_path: Относительный путь файла (от каталога РД).
            Записывается в таблицу ``documents``.
        file_hash: Хеш файла (SHA-256). Записывается в
            ``documents.file_hash``.
        file_size: Размер файла в байтах. Записывается в
            ``documents.file_size`` и ``documents.cached_size``.
        last_modified: Дата изменения в формате ISO 8601.
            Записывается в ``documents.last_modified`` и
            ``documents.cached_mtime``.

    Returns:
        Список кортежей ``(SQL-запрос, параметры)`` для батчевой
        записи в БД, либо ``None`` при любой ошибке.
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
