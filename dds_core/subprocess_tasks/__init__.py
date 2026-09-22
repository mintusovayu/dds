"""
Пакет subprocess-задач DDS — composition root для worker-функций.

Модуль является **маркером пакета** (без исполняемого кода) и содержит
документацию архитектурного контракта, определяющего правила импорта
для всех модулей пакета.

Назначение
----------

Пакет ``dds_core.subprocess_tasks`` — узкий composition root для задач,
выполняемых в отдельных процессах через
:class:`~dds_core.infrastructure.process_task_runner.ProcessTaskRunner`.
Каждая задача — это функция уровня модуля (picklable), которая
выполняется в дочернем процессе, созданном через контекст
``forkserver``.

В отличие от остальных слоёв проекта, пакет легально импортирует
**и** ``dds_core.application``, **и** ``dds_core.infrastructure``:
worker-функции вынуждены создавать инфраструктурные зависимости
(например, ``PyMuPDFTextExtractor``) локально внутри дочернего
процесса, потому что родительский процесс не может передать их через
pickle.

Единственная точка доступа к пакету — ``dds_web.lifespan`` (главный
composition root приложения). Это зафиксировано архитектурным
контрактом ``subprocess-tasks-isolation`` и записью в
``exceptions.yaml``.

Состав пакета (Фаза 4)
----------------------

+-----------------------------+---------------------------------------+
| Модуль                      | Содержимое                            |
+=============================+=======================================+
| ``pdf_workers.py``          | Worker-функции для PyMuPDF:            |
|                             | ``extract_document_queries`` —         |
|                             | извлечение текста PDF и формирование   |
|                             | SQL-запросов индексирования.           |
+-----------------------------+---------------------------------------+

В Фазе 5 планируется добавление ``build_index_plan_in_subprocess``
(после введения ``DocumentIndexPlan``); в Фазе 6 — worker-функций
для рендера страниц и подсветки.

Архитектурные контракты
-----------------------

Пакет подчиняется двум контрактам ``import-linter`` (активируются
в Фазе 4):

**``subprocess-tasks-isolation`` (forbidden).**
Пакет ``dds_core.subprocess_tasks`` может импортироваться только
из ``dds_web.lifespan``. Запрещено импортировать его из
``dds_core.domain``, ``dds_core.application``, ``dds_core.infrastructure``
и остальных модулей ``dds_web``.

Обоснование: worker-функции требуют полного набора инфраструктурных
зависимостей и не должны быть достижимы из середины слоёв — иначе
легко скатиться к хаотичным импортам infrastructure из application.

**``subprocess-tasks-shallow`` (forbidden).**
Пакет **не должен** импортировать:

- ``dds_core.domain.events`` — stateful-события шины;
- ``dds_core.domain.interfaces`` — Protocol-интерфейсы;
- ``dds_core.domain.config`` — глобальная конфигурация;
- ``dds_core.infrastructure.event_bus``;
- ``dds_core.infrastructure.sqlite_adapter``;
- ``dds_core.infrastructure.process_task_runner``;
- ``dds_web`` — все модули presentation layer.

Обоснование: forkserver-контекст изолирует состояние основного
процесса. Передача ``event_bus`` или открытого соединения с БД в
worker привела бы к неявным ошибкам (pickle-несериализуемость) или
утечкам (дублирование соединений). Пакет должен оставаться простым:
принимает параметры, создаёт зависимости локально, возвращает
picklable-результат.

Разрешённые импорты:

- ``dds_core.domain.models`` — только type hints и dataclass-модели
  (например, ``WordIndex``, ``WordEntry``, ``DocumentIndexPlan``
  в Фазе 5);
- ``dds_core.application.query_builder`` — вызов
  ``build_document_queries`` (в Фазе 5 заменяется на
  ``build_index_plan``);
- ``dds_core.infrastructure.pymupdf_text_extractor`` — создание
  ``PyMuPDFTextExtractor`` внутри worker'а.

Принципы
--------

- **Маркер пакета.** ``__init__.py`` не содержит исполняемого кода
  и не реэкспортирует символы из подмодулей. Это устраняет
  каскадные импорты infrastructure при загрузке пакета и делает
  контракты ``import-linter`` точнее.
- **Явные импорты из подмодулей.** Потребители обращаются напрямую:
  ``from dds_core.subprocess_tasks.pdf_workers import extract_document_queries``.
- **Один процесс — одна задача.** ``ProcessTaskRunner`` создаёт
  дочерний процесс на каждый вызов ``run()``; worker-функции не
  должны полагаться на сохраняемое состояние.
- **Picklable.** Аргументы и результат worker-функции обязаны
  сериализоваться через ``pickle``. Всё, что несериализуемо
  (открытые файлы, соединения, event loop), должно создаваться
  локально внутри worker'а.

Ссылки
------

- ``docs/architecture-decisions/ADR-004-process-task-runner.md`` —
  обоснование выбора forkserver + process-per-task, альтернативы,
  контракты.
- ``docs/architecture-decisions/exceptions.yaml`` — контракты
  ``subprocess-tasks-isolation`` и ``subprocess-tasks-shallow``.
- ``dds_core/infrastructure/process_task_runner.py`` — основной
  компонент, вызывающий worker-функции.
"""

# Пакет не экспортирует символы из подмодулей. Потребители импортируют
# напрямую: from dds_core.subprocess_tasks.pdf_workers import ...
#
# Это осознанное решение: __init__.py не должен тянуть infrastructure
# при импорте пакета (нарушило бы subprocess-tasks-shallow) и не должен
# создавать точки входа, обходящие контракт subprocess-tasks-isolation.
