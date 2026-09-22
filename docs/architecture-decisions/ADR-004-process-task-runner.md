# ADR-004: ProcessTaskRunner

| Поле | Значение |
|---|---|
| Дата | 2025-01-15 |
| Автор | mintusovayu |
| Статус | accepted |
| Supersedes | — |
| Superseded-by | — |
| Связанные ADR | ADR-001 (Temporary stderr Lock), ADR-005 (DocumentIndexPlan, запланирован) |
| Фаза внедрения | 4 |

---

## Контекст

До Фазы 4 извлечение текста PDF выполнялось через
`concurrent.futures.ProcessPoolExecutor` (см. `dds_web/lifespan.py::lifespan`
startup — создание `extract_executor`). Пул переиспользовал N=6
процессов между задачами. Это работало, но накопило три класса
проблем:

### Проблема A. Сегфолты PyMuPDF отравляют пул

PyMuPDF — C-расширение с известными случаями SIGSEGV на повреждённых
или нестандартных PDF (CAD-экспорты, PDF с битыми xref-таблицами,
документы с нестандартной системой координат). В `ProcessPoolExecutor`
сегфолт одного воркера **ломает весь пул**: все оставшиеся задачи
падают с `BrokenProcessPool`, пул становится непригодным до
пересоздания. До Фазы 4 в коде `scan_pipeline.py` был специальный
`except BrokenProcessPool:` — обработка этой ситуации через fallback
на потоковый путь. Это лечило симптом, а не причину.

### Проблема B. Наследование состояния через `fork`

На Linux `ProcessPoolExecutor` по умолчанию использует контекст
`fork`. Дочерний процесс получает полную копию памяти родителя:
открытые файловые дескрипторы, захваченные `threading.Lock`,
состояние event loop. Это создаёт несколько классов ошибок:

- Дочерний процесс может случайно использовать fd, открытый
  родителем для БД (SQLite WAL-файл) — с непредсказуемыми
  последствиями.
- Захваченные родителем блокировки внутри дочернего процесса
  оказываются «занятыми навсегда» — попытка взять их приводит
  к deadlock.
- Любая ссылка на `event_bus` (содержит очередь `asyncio` и
  ссылку на event loop) в дочернем процессе бесполезна, но
  занимает память и вводит в заблуждение.

### Проблема C. Race condition в `_capture_stderr` (см. ADR-001)

PyMuPDF пишет диагностику напрямую в файловый дескриптор 2
(fd 2) — **процессный** ресурс, не потоковый. `_capture_stderr`
подменяет fd 2 через `os.dup2`, чтобы перехватить сообщения.
ADR-001 добавил `threading.Lock` — временную меру,
сериализующую `_capture_stderr` в рамках процесса.

В Фазе 4 встала задача: **устранить корневую причину проблемы A**
(сегфолты) и **подготовить основу для решения проблемы B** (изоляция
состояния). Проблема C — отдельная: она касается не извлечения
текста, а операций рендера/подсветки в веб-слое (см. раздел
«Известные ограничения»).

### Контекст среды

DDS работает в single-worker режиме uvicorn (см.
`docs/deployment.md`). Python 3.14. Linux как целевая ОС. PyMuPDF
не публикует type stubs и является обязательной runtime-зависимостью.

---

## Альтернативы

### Альтернатива 1: Оставить `ProcessPoolExecutor` как есть

**Описание.** Не вводить новый компонент. Оставить текущий пул.

**Плюсы.**
- Никаких изменений в коде.
- Минимальный оверхед на переиспользовании процессов.

**Минусы.**
- Проблема A сохраняется: любой SIGSEGV в PyMuPDF ломает пул.
- Проблема B сохраняется: `fork` наследует состояние родителя,
  включая fd и блокировки.
- Fallback на потоки через `except BrokenProcessPool` — симптоматическое
  лечение.

**Итог.** Отклонена. Проблемы системные и регулярно воспроизводятся
на реальных данных.

### Альтернатива 2: Process-per-task на `forkserver` (принята)

**Описание.** Создавать отдельный процесс на каждую задачу. Контекст
`forkserver` (Linux) или `spawn` (macOS/Windows). IPC через
`multiprocessing.Queue`. Ограничение параллелизма через
`asyncio.Semaphore`.

**Плюсы.**
- **Проблема A решена.** Падение одного процесса не влияет на
  другие: каждый subprocess изолирован. Сегфолт PyMuPDF → умирает
  только текущая задача.
- **Проблема B решена.** `forkserver` не наследует состояние
  родителя: стартует из «чистого» процесса, содержащего только
  stdlib и модуль, указанный для forkserver. Открытые fd, блокировки,
  event loop — не переносятся.
- **Простота.** Одна функция `run(func, *args, timeout, kind)`;
  никакой возни с пулами и их пересозданием.
- **Автоматический graceful shutdown** с сигналами SIGTERM → SIGKILL
  при превышении shutdown_timeout.
- **Таймауты** встроены через `asyncio.wait_for` + контроль
  `process.is_alive()`.
- **Раздельные пулы** для interactive- и batch-задач через два
  семафора — резервирование слотов для будущих batch-сценариев.

**Минусы.**
- **Оверхед fork на задачу.** ~50–200 мс на forkserver (первый
  запуск ~200–500 мс, последующие ~30–50 мс). Для 1000 файлов:
  ~30–50 с суммарно. Сопоставимо с извлечением текста из типичного
  PDF (100–300 мс).
- **Ограничение параллелизма** через семафор, а не через размер
  пула. При N=6 семафор и N=6 пул дают одинаковую пропускную
  способность.
- **Сложность реализации** — собственная обработка IPC, таймаутов,
  сигналов. Но локализована в одном модуле (~500 строк).

**Итог.** Принята.

### Альтернатива 3: `multiprocessing.Pool` с `maxtasksperchild=1`

**Описание.** Использовать `multiprocessing.Pool(processes=N,
maxtasksperchild=1)`. Пул пересоздаёт воркер после каждой задачи.

**Плюсы.**
- Переиспользование API `Pool` (map/apply).
- `maxtasksperchild=1` даёт изоляцию между задачами на уровне
  воркеров.

**Минусы.**
- `Pool` использует `fork` по умолчанию — проблема B сохраняется
  (наследование состояния родителя). Явное указание `forkserver`
  возможно, но нестандартно.
- `maxtasksperchild=1` пересоздаёт воркер, но `Pool` стартует
  весь пул целиком: первый запуск дороже, чем process-per-task.
- **Синхронный API.** `Pool.apply_async` не интегрируется с
  `asyncio` напрямую. Потребовалось бы оборачивать в
  `loop.run_in_executor` с блокирующим `AsyncResult.get()`. Это
  сводит на нет преимущества process-per-task: слот executor'а
  занят, пока `Pool` ждёт задачу.
- **Таймауты** реализуются через `AsyncResult.get(timeout=...)`
  в отдельном потоке — снова утечка слотов при срабатывании.

**Итог.** Отклонена. Несовместимость с asyncio-моделью сводит
на нет выгоды.

### Альтернатива 4: `ProcessPoolExecutor` с явным `forkserver`

**Описание.** Оставить `ProcessPoolExecutor`, но передать ему
`mp_context=multiprocessing.get_context("forkserver")`.

**Плюсы.**
- Минимальные изменения (одна строка в `lifespan.py`).
- `forkserver` решает проблему B.

**Минусы.**
- **Проблема A сохраняется.** `ProcessPoolExecutor` по-прежнему
  переиспользует воркеры. Сегфолт одного воркера → `BrokenProcessPool`
  → вся очередь падает.
- `maxtasksperchild` не поддерживается `ProcessPoolExecutor`
  (есть только в `multiprocessing.Pool`).

**Итог.** Отклонена. Не решает главную проблему.

### Альтернатива 5: `fork` вместо `forkserver`

**Описание.** Использовать `forkserver`-обёртку, но с контекстом
`fork`. Каждая задача — отдельный `fork`-процесс.

**Плюсы.**
- Быстрее `forkserver` (нет дополнительного процесса-сервера).
- Простая реализация.

**Минусы.**
- **Проблема B сохраняется.** `fork` наследует состояние родителя
  (fd, блокировки, event loop). Каждый дочерний процесс — копия
  родителя на момент `fork`.
- На Python 3.14 `fork` внутри активного `asyncio` event loop
  вызывает `DeprecationWarning` и в будущих версиях может быть
  запрещён.

**Итог.** Отклонена. `forkserver` решает проблему B без значимой
потери производительности.

### Альтернатива 6: Отказ от процессов, только потоки

**Описание.** Извлекать текст в `ThreadPoolExecutor` (том же
`scan_executor`). Никаких subprocess.

**Плюсы.**
- Простая реализация.
- Нет IPC, нет pickle-сериализации.

**Минусы.**
- **GIL.** PyMuPDF — C-расширение, но парсинг PDF включает
  Python-код. Под GIL параллелизм ограничен.
- **Сегфолты.** SIGSEGV в PyMuPDF убивает весь uvicorn-процесс.
  Это **хуже**, чем падение пула: умирает всё приложение.
- **`_stderr_lock`** остаётся единственной защитой от race за fd 2,
  что делает throughput извлечения сериализованным.

**Итог.** Отклонена. Сегфолты PyMuPDF — реальный сценарий, изоляция
в процессах принципиально важна.

---

## Решение

Применяется **Альтернатива 2**: `ProcessTaskRunner` с process-per-task
на контексте `forkserver` (Linux) / `spawn` (macOS/Windows).

### Ключевые детали реализации

**Расположение.**

- `dds_core/infrastructure/process_task_runner.py` — компонент
  `ProcessTaskRunner`, реализующий `IProcessTaskRunner` (Protocol
  в `dds_core/domain/interfaces.py`).
- `dds_core/subprocess_tasks/__init__.py` — маркер пакета-композиции.
- `dds_core/subprocess_tasks/pdf_workers.py` — worker-функция
  `extract_document_queries` (перенесена из
  `dds_core/application/extract_worker.py`, файл удалён).

**Инверсия зависимостей.**

- `ScanPipeline` (application) зависит от `IProcessTaskRunner`
  (domain), а не от `ProcessTaskRunner` (infrastructure).
- Конкретная реализация создаётся в `dds_web/lifespan.py` и
  передаётся через DI.
- Worker-функция (`pdf_worker`) передаётся как `Callable` — без
  импорта `subprocess_tasks` из application. Это сохраняет
  контракт `subprocess-tasks-isolation`.

**Worker.**

- `extract_document_queries` — модульная (picklable) функция.
- Локально создаёт `PyMuPDFTextExtractor` (composition root,
  где импорт infrastructure разрешён).
- Вызывает `build_document_queries` из
  `dds_core.application.query_builder` (application-логика).
- При любой ошибке возвращает `None` — pickle-совместимый
  сигнал ошибки.

**IPC.**

- `multiprocessing.Queue` для результата. Воркер отправляет
  кортеж `("ok", pickle.dumps(result))` или
  `("error", pickle.dumps(exception))`.
- Родитель ждёт результат через
  `loop.run_in_executor(None, _blocking_get, queue, process, timeout)`.
- `_blocking_get` опрашивает `queue.get(timeout=0.1)` в цикле,
  проверяя `process.is_alive()`. Это позволяет обнаружить смерть
  worker'а **немедленно**, а не ждать полного timeout.

**Таймауты.**

- `asyncio.wait_for` ограничивает общее ожидание.
- При срабатывании: `process.terminate()` (SIGTERM), grace=1.0 с,
  затем `process.kill()` (SIGKILL) при необходимости.
- После этого поднимается `TimeoutError` для вызывающего кода.

**Ограничение параллелизма.**

- Два `asyncio.Semaphore`: `interactive` (6 слотов) и `batch` (4).
- Параметр `kind: Literal["interactive", "batch"]` выбирает
  семафор.
- В Фазе 4 `ScanPipeline` использует только `interactive`.

**Обход ограничения Python 3.14 (forkserver + running loop).**

В Python 3.14 forkserver при первом `process.start()` использует
внутренний `asyncio.Runner`, который падает с
`RuntimeError: Runner.run() cannot be called from a running event
loop`, если вызван из активного event loop (включая случай
`asyncio.to_thread` — `ContextVar _RunningLoop` копируется).

Решение — обёртка `_start_in_fresh_context`:

```python
contextvars.Context().run(process.start)
```

Пустой `Context` изолирует `_RunningLoop`. `asyncio.to_thread`
переносит вызов в отдельный поток (не блокирует event loop),
`Context().run` — в изолированный контекст.

**Graceful shutdown.**

- `await process_runner.close(shutdown_timeout)` в shutdown
  lifespan.
- Активные процессы: SIGTERM → grace → SIGKILL.
- Вызывается **до** закрытия БД, чтобы subprocess'ы не обращались
  к закрытому соединению.

**Контракты `import-linter`.**

- `subprocess-tasks-isolation` — `subprocess_tasks` доступен
  только из `dds_web.lifespan`.
- `subprocess-tasks-shallow` — `subprocess_tasks` не импортирует
  stateful-модули infrastructure (`event_bus`, `sqlite_adapter`,
  `process_task_runner`) и presentation layer (`dds_web`).
  Доменные типы/константы разрешены (не stateful, попадают
  транзитивно через application/infrastructure).

**Временное исключение `application-isolation` удалено.**

В фазах 0–3 в `exceptions.yaml` было временное исключение
`dds_core.application.extract_worker → dds_core.infrastructure.pymupdf_text_extractor`
(phase_removed_by: 5). В Фазе 4 файл `extract_worker.py` удалён,
исключение снято. `application-isolation` снова чистый.

### Инварианты

- **Pickle-сериализуемость.** `func` и `*args` обязаны быть
  pickle-сериализуемыми. Проверяется раньше `process.start()`
  через `pickle.dumps` — понятное исключение вместо падения
  forkserver.
- **Изоляция состояния.** `forkserver` не наследует fd, блокировки,
  event loop родителя.
- **Таймаут не оставляет процессов.** После `TimeoutError` процесс
  гарантированно убит (SIGKILL как последняя мера).
- **`close()` идемпотентен.** Повторный вызов — no-op.
- **Race condition в `_capture_stderr` (см. ADR-001) не решается
  этим ADR.** См. раздел «Известные ограничения».

---

## Последствия

### Положительные

- Сегфолты PyMuPDF **не отравляют** параллельную обработку:
  падение одного worker'а не влияет на другие файлы.
- Наследование состояния родителя исключено через `forkserver`.
- Заменён симптоматический `except BrokenProcessPool` на
  архитектурное решение.
- Интерфейс `IProcessTaskRunner` в domain обеспечивает инверсию
  зависимостей: `ScanPipeline` не знает о реализации.
- Worker-функция и composition root вынесены в
  `dds_core/subprocess_tasks/` — единственная точка, где разрешён
  импорт infrastructure в application-логику (через контракт 4).
- **Временное исключение `application-isolation` удалено** —
  архитектурный контракт application layer снова чистый.
- `PID` каждого worker'а изолирован, диагностика через `stderr`
  (перехватывается через `_capture_stderr` в самом worker'е) не
  пересекается между задачами.

### Отрицательные

- **Оверхед fork на задачу.** На 1000 PDF ≈ 30–50 с суммарно
  (при 6 параллельных слотах). Сопоставимо с извлечением текста,
  но заметно на больших каталогах.
- **Сложность реализации.** ~500 строк в `ProcessTaskRunner` —
  обработка IPC, таймаутов, сигналов, `is_alive`, `pickle`.
- **Усложнённый graceful shutdown.** Нужно явно ждать
  `process_runner.close()` с таймаутом; при зависании — SIGKILL.
- **`forkserver` специфичен для Python 3.14.** Пришлось
  добавить обёртку `_start_in_fresh_context` из-за внутреннего
  `asyncio.Runner`. Это ослабляет переносимость решения между
  версиями Python.
- **Отладка сложнее.** Traceback в subprocess'е не пробрасывается
  автоматически; ошибки передаются через pickle и теряют часть
  контекста (локальные переменные, стек вызовов).

### Нейтральные

- `ProcessPoolExecutor` и `extract_worker.py` удалены.
- В `dds_web/lifespan.py` появился `ProcessTaskRunner`,
  удалён `extract_executor`.
- В `dds_core/domain/interfaces.py` добавлен `IProcessTaskRunner`.
- В `dds_core/domain/config.py` добавлены константы
  `PROCESS_RUNNER_*`.
- Активны контракты 4, 5 в `.importlinter` (были закомментированы).
- Запись в `DEPRECATIONS.yaml` о `extract_worker.extract_document_queries`
  (`direct_removal`, `removed_in_phase: 4`).

### Известные ограничения

- **ADR-001 не заменяется этим ADR.** `_stderr_lock` в
  `pymupdf_text_extractor.py` **остаётся**. Он защищает
  параллельные вызовы из `scan_executor` (потоки), которые
  используют `/render` и `/highlights` (см. `dds_web/api.py`).
  Эти операции по-прежнему выполняются в потоках, а не в
  subprocess'ах; race за fd 2 сохраняется, lock его сериализует.
  Удаление lock'а — задача будущей фазы (когда render/highlights
  переедут в `ProcessTaskRunner`). ADR-001 сохраняет статус
  `accepted`. См. примечание в ADR-001.
- **Сериализация доменных типов не запрещена.** Контракт
  `subprocess-tasks-shallow` запрещает только stateful-модули
  infrastructure (`event_bus`, `sqlite_adapter`, `process_task_runner`)
  и presentation layer. Доменные `models`, `interfaces`, `events`,
  `config` разрешены: они не несут состояния и попадают в worker
  транзитивно через application-логику (`query_builder`).
- **`ProcessTaskRunner.run` использует семафор, а не пул.**
  При N=6 семафор и N=6 пул дают одинаковую пропускную способность,
  но семафор работает per-task: меньше «пустого» времени в слотах.
- **Worker не имеет доступа к БД.** `sqlite_adapter` запрещён
  контрактом 5. Worker получает всё нужное через аргументы и
  возвращает SQL-запросы (`list[tuple[str, tuple]]`), которые
  родитель записывает в БД через `SQLiteAdapter.execute_write_batched_documents`.
  В Фазе 5 это изменится: worker вернёт `DocumentIndexPlan`,
  родитель запишет через `IIndexWriter`.
- **Обёртка `_start_in_fresh_context` — временная.** Она
  компенсирует поведение forkserver в Python 3.14. Если
  Python 3.15+ изменит поведение, обёртку можно убрать. См.
  комментарий в `process_task_runner.py`.

---

## Ссылки

- **Реализация:**
  - `dds_core/infrastructure/process_task_runner.py` — компонент
    `ProcessTaskRunner`.
  - `dds_core/subprocess_tasks/__init__.py` — маркер пакета.
  - `dds_core/subprocess_tasks/pdf_workers.py` — worker-функция.
  - `dds_core/domain/interfaces.py` — Protocol `IProcessTaskRunner`.
  - `dds_core/domain/config.py` — константы `PROCESS_RUNNER_*`.
  - `dds_web/lifespan.py` — создание и закрытие runner'а.
- **Изменённые файлы:**
  - `dds_core/application/scan_pipeline.py` — использование
    `process_runner.run(pdf_worker, ...)`.
  - `dds_core/application/scan_orchestrator.py` — передача
    `process_runner`/`pdf_worker` в `ScanPipeline`.
  - `docs/architecture-decisions/exceptions.yaml` — контракты 4, 5.
- **Удалённые файлы:**
  - `dds_core/application/extract_worker.py`.
- **Тесты:**
  - `tests/test_process_task_runner.py` — 25 базовых тестов.
  - `tests/test_process_runner_timeout.py` — 7 тестов таймаутов.
  - `tests/test_scan_pipeline_cancellation.py` — 5 тестов отмены
    (активируются в Фазе 5).
- **Связанные ADR:**
  - ADR-001 (Temporary stderr Lock) — остаётся `accepted`;
    дополнен примечанием о судьбе lock'а в свете Фазы 4.
  - ADR-005 (DocumentIndexPlan, запланирован) — заменит
    `extract_document_queries` на `build_index_plan_in_subprocess`.
- **Внешние материалы:**
  - Документация `multiprocessing.get_context`:
    <https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods>
  - Документация `multiprocessing.process.BaseProcess`:
    <https://docs.python.org/3/library/multiprocessing.html#multiprocessing.process.BaseProcess>
  - PEP 741 (Python 3.14): forkserver и `asyncio.Runner` — обсуждение
    взаимодействия.
  - Документация PyMuPDF о SIGSEGV на повреждённых PDF:
    <https://pymupdf.readthedocs.io/en/latest/app3.html>
