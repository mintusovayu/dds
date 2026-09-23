# ADR-005: DocumentIndexPlan

| Поле | Значение |
|---|---|
| Дата | 2025-01-15 |
| Автор | mintusovayu |
| Статус | accepted |
| Supersedes | — |
| Superseded-by | — |
| Связанные ADR | ADR-003 (Domain Text Normalization), ADR-004 (ProcessTaskRunner) |
| Фаза внедрения | 5 |

---

## Контекст

До Фазы 5 запись результатов индексации в БД выполнялась через
**SQL-строки, формируемые в application-слое**. Функция
`dds_core/application/query_builder.py::build_document_queries`
принимала параметры документа, открывала PDF через `ITextExtractor`,
обходила страницы и возвращала список кортежей
`(SQL-запрос, параметры)` для батчевой записи:

```python
queries.append(
    (
        "INSERT INTO text_index_fts "
        "(doc_id, page_number, text_content, normalized_text) "
        "VALUES (?, ?, ?, ?)",
        (doc_id, page_num, text, normalize_text(text)),
    )
)
```

`TextIndexer.prepare_document_queries` делегировал в
`build_document_queries`, а `TextIndexer.write_documents_batch`
передавал результат в `SQLiteAdapter.execute_write_batched_documents`.
В `ScanPipeline` воркер формирования запросов выполнялся через
`ProcessTaskRunner` (Фаза 4), возвращая список SQL-строк через IPC.

Это накопило три связанных архитектурных долга.

### Долг A. SQL-специфика в application-слое

SQL-строки `INSERT INTO documents ...`, `INSERT INTO text_index_fts ...`,
`DELETE FROM text_index_fts WHERE doc_id = ?` **знают**:

- имена таблиц (`documents`, `text_index_fts`);
- имена колонок (`doc_id`, `file_path`, `file_hash`, `page_number`,
  `text_content`, `normalized_text`, `cached_size`, `cached_mtime`);
- порядок и количество `?`-плейсхолдеров;
- семантику `INSERT OR REPLACE`.

Всё это — **специфика конкретного бэкенда БД** (SQLite FTS5).
Если заменить SQLite на PostgreSQL (упомянуто как целевое
направление в `ISearchBackend`), придётся переписать
`query_builder.py` и всё, что его использует. Application-слой
формально не должен знать о конкретном бэкенде — это
нарушение слоистости и инверсии зависимостей.

### Долг B. Worker в subprocess возвращает SQL-строки

`build_document_queries` вызывается из
`extract_document_queries` (Фаза 4, `subprocess_tasks/pdf_workers.py`)
в дочернем процессе `ProcessTaskRunner`. Результат — список
SQL-строк — сериализуется через `pickle` и передаётся родителю.

Проблема: **SQL-строки — не доменный тип**. Их формат и содержимое
зависят от конкретной схемы БД родителя. Worker в subprocess
вынужден «знать» о схеме `documents` и `text_index_fts`, чтобы
сформировать корректный SQL. Это связывает subprocess-слой со
схемой БД, которую он не должен видеть.

### Долг C. Три параллельных представления одного результата

В Фазе 4 в проекте одновременно сосуществовали:

1. `query_builder.build_document_queries` — возвращает SQL-строки
   (используется в production).
2. `PyMuPDFTextDocument.build_word_index` — возвращает `WordIndex`
   (используется для подсветки).
3. `DocumentInfo` / `Document` — доменные модели метаданных
   (используются для чтения).

Каждое представление формировалось независимо, что увеличивало
риск рассинхронизации (например, при добавлении нового столбца
в `documents` нужно было править `query_builder`, но не
`DocumentInfo`). Единая доменная модель устранила бы дублирование.

### Контекст среды

DDS работает в single-worker режиме uvicorn (см.
`docs/deployment.md`). Python 3.14. Linux. SQLite FTS5 как
единственный поисковый бэкенд на момент Фазы 5, но архитектура
`ISearchBackend` предполагает будущую замену (Elasticsearch и др.).

---

## Альтернативы

### Альтернатива 1: Оставить `query_builder` как есть

**Описание.** Не менять существующий API. `query_builder`
продолжает формировать SQL-строки в application-слое.

**Плюсы.**
- Никаких изменений в коде.
- Ноль рисков регресса.
- Существующие тесты не трогаются.

**Минусы.**
- Долг A сохраняется: application знает о конкретном бэкенде БД.
- Долг B сохраняется: worker в subprocess оперирует SQL-строками.
- Долг C сохраняется: три параллельных представления.
- Замена SQLite на PostgreSQL (упомянута в `ISearchBackend`)
  потребует правки application-кода.
- Противоречит контракту `application-isolation` (SQL — не доменное
  правило), хотя `import-linter` не ловит SQL-строки как импорты.

**Итог.** Отклонена. Долги системны и запланированы к устранению
в плане рефакторинга v9.

### Альтернатива 2: `DocumentIndexPlan` + `IIndexWriter` (принята)

**Описание.** Ввести доменную модель `DocumentIndexPlan`
(с вложенным `PageRecord`) и `IIndexWriter` Protocol.
Application-слой формирует план, infrastructure — конвертирует
в SQL и записывает.

- `build_index_plan` (application) → `DocumentIndexPlan | None`.
- `build_index_plan_in_subprocess` (subprocess_tasks) — worker,
  возвращает план через IPC.
- `SqliteIndexWriter` (infrastructure) — реализация
  `IIndexWriter`, конвертирует план в SQL-запросы SQLite FTS5.
- `TextIndexer` принимает `IIndexWriter` (запись) и `IDatabase`
  (чтение).

**Плюсы.**
- **Долг A устранён.** SQL-строки формируются только в
  infrastructure; application работает с доменной моделью.
- **Долг B устранён.** Worker возвращает доменный план
  (pickle-совместимый: `frozen=True`, `tuple`), не SQL.
- **Долг C устранён.** Единая модель метаданных документа
  (совместима с `documents`).
- Замена бэкенда БД возможна без правки application: достаточно
  реализовать новый `IIndexWriter`.
- `DocumentIndexPlan` — чистая структура, легко тестируется
  в изоляции (`test_index_plan_builder.py`).
- `SqliteIndexWriter` — единственная точка SQL для записи индекса;
  тестируется на реальной SQLite (`test_sqlite_index_writer.py`).

**Минусы.**
- Breaking change для кастомных `ITextIndexer` (если есть).
- Усложнение `TextIndexer.__init__` до 3 параметров
  (`text_extractor`, `index_writer`, `db`).
- Необходимость поддерживать два интерфейса (`IIndexWriter` для
  записи, `IDatabase` для чтения) до Фазы 8.
- Удаление `query_builder.py` и переименование методов.

**Итог.** Принята.

### Альтернатива 3: Callable-фабрика SQL в infrastructure

**Описание.** Оставить в application вызов функции формирования
SQL, но передавать её через DI как `Callable`. Application не
содержит SQL-литералов, но вызывает фабрику, реализованную в
infrastructure.

**Плюсы.**
- Меньше изменений: `query_builder.build_document_queries` →
  параметр конструктора.
- SQL-литералы физически в infrastructure.

**Минусы.**
- **Протечка через сигнатуру.** `TextIndexer` всё равно работает
  с `list[tuple[str, tuple]]` — типом, семантика которого
  «SQL-строки и параметры». Тип не доменный.
- **Worker в subprocess не может получить фабрику.** Pickle
  не сериализует callable из application без глобальной ссылки;
  в subprocess пришлось бы дублировать фабрику или передавать
  её через `module.attr`, что снова связывает subprocess с
  infrastructure.
- **Не устраняет долг C.** `DocumentInfo` остаётся отдельной
  моделью.
- Хуже тестируется: `DocumentIndexPlan` — простой dataclass,
  фабрика + список кортежей — сложнее для assert'ов.

**Итог.** Отклонена. Полумера, не устраняющая корень проблемы.

### Альтернатива 4: Запретить SQL в application через `import-linter`

**Описание.** Не менять код, но добавить архитектурный контракт
`no-sql-strings-in-application`: AST-анализ запрещает
строковые литералы, начинающиеся с `SELECT`/`INSERT`/`UPDATE`/
`DELETE` в модулях `dds_core.application`.

**Плюсы.**
- Формально фиксирует правило.
- Ноль изменений в production-коде.

**Минусы.**
- **Формальное решение без структурного.** SQL уже есть в
  `query_builder`; правило сделает его **ошибкой сборки**, но
  не устранит. Придётся либо удалить `query_builder`, либо
  добавить `ignore_imports` для него — что противоречит цели.
- **Инструмент `check_no_sql_in_contracts.py` уже существует**
  и защищает `dds_core/domain/interfaces.py` от SQL. Расширение
  на `application` — это тот же принцип, но он не отвечает на
  вопрос «а как же **писать**?».
- **Не решает долг B** (worker в subprocess всё равно возвращает
  SQL-строки, если только не менять API).
- Принудительный рефакторинг: после включения правила нужно
  всё равно придумать альтернативу — то есть вернуться к варианту 2.

**Итог.** Отклонена. Инструмент уже есть для контрактов; расширение
не решает задачу.

### Альтернатива 5: SQLAlchemy Core / ORM

**Описание.** Использовать SQLAlchemy Core или ORM для построения
SQL. Application-слой работает с таблицами/моделями SQLAlchemy,
infrastructure предоставляет connection.

**Плюсы.**
- Универсальный API для SQLite/PostgreSQL.
- Готовая поддержка транзакций, миграций (Alembic).

**Минусы.**
- **Огромная зависимость.** SQLAlchemy — ~50 MB + собственный
  стек. Проект принципиально использует stdlib + минимальные
  зависимости (см. `requirements.txt`).
- **FTS5 поддержка в SQLAlchemy ограничена.** Виртуальные таблицы
  FTS5 создаются только через raw SQL; `MATCH`, `bm25()`,
  `snippet()` не покрыты публичным API.
- **Не решает долг B.** Worker в subprocess всё равно возвращает
  данные через IPC; SQLAlchemy-объекты не pickle-сериализуемы.
- **Замена одного долга на другой.** SQL уходит из application,
  но появляется SQLAlchemy-специфика.

**Итог.** Отклонена. Неоправданная зависимость для задачи,
решаемой 300 строками собственного кода.

### Альтернатива 6: Возвращать «сырые» структуры (list[dict])

**Описание.** `build_document_queries` возвращает `list[dict[str, Any]]`
с «сырыми» данными (без SQL). Application передаёт их в
infrastructure, который сам формирует SQL.

**Плюсы.**
- Нет SQL в application.
- Pickle-совместимо (dict + str/int).

**Минусы.**
- **Нет статической типизации.** `dict[str, Any]` не проверяется
  mypy: опечатка в имени ключа (`"page_num"` vs `"page_number"`)
  обнаруживается только в runtime.
- **Нет самодокументируемости.** Структура dict'а не видна из типа;
  приходится читать docstring или implementation.
- **Нет инвариантов.** `page_count`, `pages` могут быть
  несогласованы; `dict` не гарантирует.
- **Проигрывает по тестируемости.** `DocumentIndexPlan` — явный
  dataclass; сравнение двух планов через `==` работает
  из коробки. Dict требует ручной проверки каждого поля.

**Итог.** Отклонена. Доменная модель — правильный инструмент
для структурированных данных. `list[dict]` — шаг назад по
типобезопасности.

---

## Решение

Применяется **Альтернатива 2**: `DocumentIndexPlan` в domain,
`IIndexWriter` Protocol, `SqliteIndexWriter` в infrastructure,
`build_index_plan` в application, `build_index_plan_in_subprocess`
в subprocess_tasks.

### Ключевые детали реализации

**Domain-модели** (`dds_core/domain/index_plan.py`):

- `PageRecord` — `@dataclass(frozen=True)`:
  `page_number: int`, `text: str`, `normalized_text: str`.
- `DocumentIndexPlan` — `@dataclass(frozen=True)`:
  `doc_id`, `file_path`, `file_hash`, `file_size`,
  `last_modified`, `page_count`, `indexed_at`,
  `pages: tuple[PageRecord, ...]`.

`pages` — `tuple`, а не `list`: иммутабельность (нельзя случайно
мутировать план после создания), hashability, корректная
pickle-сериализация без модификаций.

**Protocol** (`dds_core/domain/index_writer.py`):

```python
@runtime_checkable
class IIndexWriter(Protocol):
    def write_plan(self, plan: DocumentIndexPlan) -> None: ...
    def write_plans_batch(
        self, plans: list[DocumentIndexPlan], commit_interval: int = 200,
    ) -> tuple[int, int]: ...
    def remove_document(self, doc_id: str) -> None: ...
```

Protocol описывает **только write-контракт** индексации.
Read-операции (`get_document_by_hash`, `get_document_by_path`,
`get_document_metadata`) остаются в `IDatabase` — их рефакторинг
вне области Фазы 5 (см. «Известные ограничения»).

**Application** (`dds_core/application/index_plan_builder.py`):

- `build_index_plan(...) -> DocumentIndexPlan | None` —
  открывает PDF через `ITextExtractor`, обходит страницы,
  применяет `normalize_text`, пропускает пустые страницы при
  `SKIP_EMPTY_TEXT_PAGES=True`, формирует `DocumentIndexPlan`.
- Любая ошибка → `None` (pickle-совместимый сигнал).
- `indexed_at` проставляется здесь — фиксирует момент **начала**
  формирования плана, а не момент записи в БД.

**Subprocess_tasks** (`dds_core/subprocess_tasks/pdf_workers.py`):

- `build_index_plan_in_subprocess(...) -> DocumentIndexPlan | None` —
  worker для `ProcessTaskRunner`. Локально создаёт
  `PyMuPDFTextExtractor`, вызывает `build_index_plan`.
- Импортирует `application.index_plan_builder` и
  `infrastructure.pymupdf_text_extractor` — разрешено контрактами
  `subprocess-tasks-isolation` и `subprocess-tasks-shallow`.
- Заменяет удалённую `extract_document_queries`.

**Infrastructure** (`dds_core/infrastructure/sqlite_index_writer.py`):

- `SqliteIndexWriter` — реализация `IIndexWriter`.
- `_plan_to_queries(plan)` конвертирует план в список SQL-запросов
  (DELETE FTS, DELETE documents, INSERT OR REPLACE documents,
  INSERT FTS × N).
- `write_plan` делегирует в `IDatabase.execute_write_many`.
- `write_plans_batch` делегирует в
  `IDatabase.execute_write_batched_documents` (SAVEPOINT-изоляция
  уже реализована в `SQLiteAdapter`).
- `remove_document` — DELETE из двух таблиц, атомарно.

**TextIndexer** (`dds_core/application/indexer.py`):

- `__init__(text_extractor, index_writer, db)` — 3 параметра.
- Запись через `IIndexWriter`, чтение через `IDatabase`.
- `prepare_index_plan` делегирует в `build_index_plan`.
- `write_index_plans` делегирует в `IIndexWriter.write_plans_batch`
  с `commit_interval=config.SCAN_COMMIT_INTERVAL`.
- `remove_document` делегирует в `IIndexWriter.remove_document`.
- Удалены `prepare_document_queries`, `write_documents_batch`,
  `_remove_document_data`.

**ScanPipeline** (`dds_core/application/scan_pipeline.py`):

- Параметры `process_runner: IProcessTaskRunner` и
  `index_plan_worker: Callable[..., Any]` — **обязательные**.
- **Потоковый fallback удалён.** В production оба всегда
  передаются из composition root.
- `_extract_worker` формирует план через
  `process_runner.run(index_plan_worker, ...)`,
  накапливает буфер `list[DocumentIndexPlan]`, записывает через
  `indexer.write_index_plans`.

**Удалено:**

- `dds_core/application/query_builder.py` — целиком.
- `extract_document_queries` — заменена на
  `build_index_plan_in_subprocess`.
- Методы `TextIndexer.prepare_document_queries`,
  `TextIndexer.write_documents_batch`, `TextIndexer._remove_document_data`.

### Инварианты

- **Идемпотентность `write_plan`.** Повторная запись плана с
  тем же `doc_id` перезаписывает существующую запись
  (`INSERT OR REPLACE` + DELETE перед INSERT).
- **Атомарность `write_plan`.** План записывается целиком или не
  записывается вовсе (`execute_write_many` в одной транзакции).
- **Изоляция `write_plans_batch`.** Ошибка в одном плане не
  откатывает остальные (SAVEPOINT на каждый план).
- **Идемпотентность `remove_document`.** Удаление
  несуществующего `doc_id` не выбрасывает исключение.
- **Picklability плана.** `DocumentIndexPlan` и `PageRecord` —
  `frozen=True`, `pages` — `tuple`. Сериализуются через `pickle`
  без модификаций — критично для IPC через `ProcessTaskRunner`.
- **Консистентность метаданных плана.** `plan.pages` отсортирован
  по возрастанию `page_number`; `len(plan.pages) <= plan.page_count`.

---

## Последствия

### Положительные

- SQL-строки устранены из application-слоя: `query_builder.py`
  удалён, `TextIndexer` работает с доменной моделью
  `DocumentIndexPlan`.
- Worker в subprocess возвращает доменный план, не SQL.
  Subprocess-слой не знает о схеме БД.
- Единая доменная модель для записи: `DocumentIndexPlan` —
  источник истины для метаданных документа при индексации.
- Замена бэкенда БД возможна без правки application:
  достаточно реализовать новый `IIndexWriter`.
- Тестирование изолировано: `build_index_plan` тестируется на
  заглушках `ITextExtractor` (22 теста), `SqliteIndexWriter` —
  на реальной SQLite (17 тестов).
- Активированы 5 тестов `test_scan_pipeline_cancellation.py` и
  медленный бенчмарк `test_scan_full_1000_pdfs`.
- `SqliteIndexWriter` — единственная точка SQL для записи
  индекса; при добавлении нового столбца правится один файл.

### Отрицательные

- **Breaking change для кастомных реализаций** `ITextIndexer`:
  сигнатура `__init__` изменилась, методы переименованы.
  В текущем проекте кастомных реализаций нет, но это нужно
  учитывать при развитии.
- **Усложнение `TextIndexer.__init__`:** 3 параметра вместо 2.
  Обосновано разделением write/read-контрактов.
- **Два интерфейса для БД** (`IIndexWriter`, `IDatabase`) до
  Фазы 8. Это следствие инкрементального рефакторинга.
- **Удалён `query_builder.py`** — если внешний код его
  использовал, потребуется правка. В проекте потребителей
  не было.

### Нейтральные

- `dds_core/domain/interfaces.py`: контракт `ITextIndexer`
  обновлён (`prepare_index_plan`, `write_index_plans`).
- `dds_core/subprocess_tasks/pdf_workers.py`: имя модуля не
  изменилось, но функция заменена.
- `docs/DEPRECATIONS.yaml`: 4 записи Фазы 5 (см. ниже).
- `dds_web/lifespan.py`: создаёт `SqliteIndexWriter`, передаёт
  `build_index_plan_in_subprocess`.
- `tests/test_scan_pipeline_cancellation.py`: `_StubIndexer`
  обновлён под новый контракт.
- `tests/benchmarks/bench_slow.py`: `test_scan_full_1000_pdfs`
  активирован.

### Известные ограничения

- **Read-side SQL остаётся.** `IDatabase.execute(...)` в
  `TextIndexer.get_document_metadata` / `get_document_by_hash` /
  `get_document_by_path`, `MetadataFilterQueryBuilder.build`,
  `DocumentCache.load`, `SQLiteAdapter.execute` — все ещё
  содержат SQL-строки. Это осознанное решение: рефакторинг
  read-side отложен на **Фазу 8** (см. план v9). Причина — задача
  Фазы 5 — устранить SQL из **пути записи**; read-side менее
  критичен (нет доменной структуры, которую нужно защищать от
  SQL-специфики).
- **`DocumentCache` использует прямой SELECT.** Возвращает
  `tuple[str, str, int, str]`, что согласовано с
  `ITextIndexer.get_document_metadata`. При рефакторинге
  read-side в Фазе 8 можно ввести `DocumentMetadata` dataclass.
- **`indexed_at` проставляется в `build_index_plan`, а не в
  `SqliteIndexWriter`.** Обоснование: план формируется в
  subprocess, где `datetime.now(UTC)` доступен. Если бы
  `indexed_at` проставлялся в writer'е, потребовалось бы
  передавать timestamp отдельно или возвращать его из
  `write_plan`, что усложнило бы Protocol. При этом `indexed_at`
  фиксирует момент **начала** индексации, а не момент записи —
  это семантически корректно (индексирование начинается
  в subprocess).
- **`PageRecord` не хранит координаты.** Координаты слов нужны
  только для подсветки (`WordIndex`), не для индексации. Разные
  модели — правильно, потому что у них разное назначение.
- **FTS5-специфика не абстрагирована.** `SqliteIndexWriter`
  знает о колонках `text_content` и `normalized_text`. При
  замене SQLite на PostgreSQL FTS-специфику придётся переписать
  в новом `IIndexWriter`. Это ожидаемо: `IIndexWriter` —
  контракт **записи индекса**, а не **поискового движка**.
  Поиск абстрагирован отдельно через `ISearchBackend`.

---

## Ссылки

- **Реализация:**
  - `dds_core/domain/index_plan.py` — `PageRecord`,
    `DocumentIndexPlan`.
  - `dds_core/domain/index_writer.py` — `IIndexWriter`.
  - `dds_core/application/index_plan_builder.py` —
    `build_index_plan`.
  - `dds_core/infrastructure/sqlite_index_writer.py` —
    `SqliteIndexWriter`.
  - `dds_core/subprocess_tasks/pdf_workers.py` —
    `build_index_plan_in_subprocess`.
  - `dds_core/application/indexer.py` — `TextIndexer` с
    `IIndexWriter`.
  - `dds_core/application/scan_pipeline.py` — `index_plan_worker`
    вместо `pdf_worker`; потоковый fallback удалён.
  - `dds_core/application/scan_orchestrator.py` — передача
    `index_plan_worker` в `ScanPipeline`.
  - `dds_core/domain/interfaces.py` — обновлённый `ITextIndexer`.
  - `dds_web/lifespan.py` — создание `SqliteIndexWriter`,
    передача `build_index_plan_in_subprocess`.
- **Удалённые файлы:**
  - `dds_core/application/query_builder.py`.
- **Тесты:**
  - `tests/test_index_plan_builder.py` — 22 теста
    (успех, ошибки открытия/`page_count`/`get_page_text`,
    `SKIP_EMPTY_TEXT_PAGES`, нормализация, порядок, пустой
    PDF, инварианты, pickle).
  - `tests/test_sqlite_index_writer.py` — 17 тестов
    (`write_plan` успех/идемпотентность/перезапись,
    `write_plans_batch` пусто/успех/SAVEPOINT/commit_interval,
    `remove_document`, структура SQL, LSP, полный цикл).
  - `tests/test_scan_pipeline_cancellation.py` — 5 тестов
    (активированы в Фазе 5).
  - `tests/benchmarks/bench_slow.py::test_scan_full_1000_pdfs` —
    активирован.
- **Связанные ADR:**
  - ADR-003 (Domain Text Normalization) — паттерн «перенос
    правила в domain для устранения протечки».
  - ADR-004 (ProcessTaskRunner) — `index_plan_worker`
    выполняется в subprocess; ADR-005 меняет формат возврата.
- **Внешние материалы:**
  - Martin Fowler, «Inversion of Control Containers and the
    Dependency Injection pattern» (2004) — обоснование
    разделения write/read-контрактов.
  - Eric Evans, «Domain-Driven Design» (2003) — понятие
    «доменной модели плана» как структуры без SQL.

---
