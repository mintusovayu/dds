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

- **Функции уровня модуля.** ``build_index_plan_in_subprocess`` должна
  быть определена на уровне модуля (не как вложенная функция), чтобы
  сериализоваться через ``pickle``. Локальные функции и лямбды
  не picklable.
- **Picklable-аргументы.** Все аргументы — примитивы (str, int) или
  стандартные структуры (list, dict). Открытые файлы, соединения,
  event loop, dataclass-со-ссылками не передаются.
- **Picklable-результат.** Возвращаемое значение
  (``DocumentIndexPlan | None``) сериализуется через ``pickle`` без
  модификаций — ``DocumentIndexPlan`` и ``PageRecord`` помечены
  ``frozen=True``, ``pages`` — ``tuple``. ``None`` при ошибке
  также pickle-совместим.
- **Локальное создание зависимостей.** ``PyMuPDFTextExtractor``
  создаётся внутри функции, а не передаётся через аргумент.
- **Обработка ошибок — не пробрасывать.** Worker не должен поднимать
  исключение наружу, если это приведёт к падению дочернего процесса
  в неконтролируемом месте. Возврат ``None`` — сигнал ошибки,
  которую ``ScanPipeline`` обработает как «файл помечен ошибочным».

Отличие от предыдущей реализации
--------------------------------

До Фазы 5 функция ``extract_document_queries`` возвращала список
SQL-запросов (``list[tuple[str, tuple]]``), сформированных функцией
``dds_core.application.query_builder.build_document_queries``. SQL
формировался в application-слое, что нарушало слоистость: SQL —
специфика конкретного бэкенда БД (SQLite FTS5).

В Фазе 5 (ADR-005, DocumentIndexPlan) worker переименован и
возвращает доменную модель ``DocumentIndexPlan``. SQL-строки
формирует ``SqliteIndexWriter`` в infrastructure-слое.

Дополнительно (историческая справка): до Фазы 4 функция жила в
``dds_core/application/extract_worker.py`` и импортировала
``PyMuPDFTextExtractor`` из infrastructure, нарушая контракт
``application-isolation``. Перенос в ``subprocess_tasks`` (Фаза 4)
устранил это нарушение.

Ссылки
------

- ``docs/architecture-decisions/ADR-004-process-task-runner.md`` —
  обоснование process-per-task и forkserver-контекста.
- ``docs/architecture-decisions/ADR-005-document-index-plan.md`` —
  обоснование перехода к доменной модели плана индексации.
- ``docs/architecture-decisions/exceptions.yaml`` — контракты
  ``subprocess-tasks-isolation`` и ``subprocess-tasks-shallow``.
- ``dds_core/infrastructure/process_task_runner.py`` — вызывающий
  компонент.
- ``dds_core/application/index_plan_builder.py`` — ``build_index_plan``,
  бизнес-логика формирования плана.
- ``dds_core/infrastructure/sqlite_index_writer.py`` — запись плана
  в БД (в родительском процессе).
"""

from __future__ import annotations

from ..application.index_plan_builder import build_index_plan
from ..domain.index_plan import DocumentIndexPlan
from ..infrastructure.pymupdf_text_extractor import PyMuPDFTextExtractor


def build_index_plan_in_subprocess(
    doc_id: str,
    file_path: str,
    file_hash: str,
    file_size: int,
    last_modified: str,
    abs_file_path: str,
) -> DocumentIndexPlan | None:
    """Строит план индексации документа в дочернем процессе.

    Функция уровня модуля — сериализуется через ``pickle`` и выполняется
    в изолированном процессе, созданном ``ProcessTaskRunner`` (контекст
    ``forkserver``). Локально создаёт ``PyMuPDFTextExtractor`` и вызывает
    :func:`~dds_core.application.index_plan_builder.build_index_plan`
    для формирования :class:`DocumentIndexPlan`.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Создание экземпляра ``PyMuPDFTextExtractor``       |
    |    | локально в дочернем процессе.                      |
    +----+----------------------------------------------------+
    | 2  | Вызов ``build_index_plan`` с параметрами документа.|
    +----+----------------------------------------------------+
    | 3  | Возврат ``DocumentIndexPlan``.                     |
    +----+----------------------------------------------------+
    | 4  | При любом исключении — возврат ``None``.           |
    +----+----------------------------------------------------+

    Поведение при ошибках:

    - Открытие файла может упасть (``FileNotFoundError``,
      ``OSError``, ``RuntimeError`` от PyMuPDF).
    - Извлечение текста страницы может упасть (``RuntimeError``
      при ошибке PyMuPDF в stderr).
    - ``build_index_plan`` возвращает ``None`` вместо проброса —
      это pickle-совместимый сигнал ошибки.

    Во всех случаях функция возвращает ``None``. ``ScanPipeline``
    интерпретирует ``None`` как «документ не удалось проиндексировать»
    и публикует событие ``FileProcessingFailed``.

    **Почему не пробрасывается исключение.** Если worker поднимет
    исключение, оно сериализуется и передаётся родителю через IPC.
    Это сработало бы, но добавляет сложность (обработка
    ``BaseException``, потеря traceback) и лишает ``ScanPipeline``
    единообразия: сейчас и ``build_index_plan_in_subprocess``, и
    ``build_index_plan`` (вызывается из ``TextIndexer.prepare_index_plan``)
    сигнализируют об ошибке одним и тем же способом — ``None``.

    Потокобезопасность:

    - Функция не хранит состояния между вызовами.
    - Каждый вызов получает собственный ``PyMuPDFTextExtractor``
      и, следовательно, собственный ``fitz.Document``.
    - Внутри одного дочернего процесса функция выполняется
      в единственном потоке.

    Picklability:

    - Функция определена на уровне модуля — pickle-совместима.
    - Аргументы — примитивы (str, int).
    - Возвращаемое значение — ``DocumentIndexPlan`` (frozen dataclass
      с tuple-полями) или ``None``. Оба варианта pickle-совместимы.

    Args:
        doc_id: Идентификатор документа в БД. Используется в
            полях ``DocumentIndexPlan.doc_id``.
        file_path: Относительный путь файла (от каталога РД).
            Записывается в ``DocumentIndexPlan.file_path``.
        file_hash: Хеш файла (SHA-256). Записывается в
            ``DocumentIndexPlan.file_hash``.
        file_size: Размер файла в байтах. Записывается в
            ``DocumentIndexPlan.file_size``.
        last_modified: Дата изменения в формате ISO 8601.
            Записывается в ``DocumentIndexPlan.last_modified``.
        abs_file_path: Абсолютный путь к PDF-файлу. В дочернем
            процессе (forkserver) путь доступен, поскольку
            рабочая директория и файловая система общие.

    Returns:
        :class:`DocumentIndexPlan` с метаданными и страницами
        документа, либо ``None`` при любой ошибке.
    """
    try:
        extractor = PyMuPDFTextExtractor()
        return build_index_plan(
            doc_id=doc_id,
            file_path=file_path,
            file_hash=file_hash,
            file_size=file_size,
            last_modified=last_modified,
            abs_file_path=abs_file_path,
            text_extractor=extractor,
        )
    except Exception:
        return None
