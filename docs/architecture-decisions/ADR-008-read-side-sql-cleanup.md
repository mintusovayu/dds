# ADR-008: Read-side SQL Cleanup

| Поле | Значение |
|---|---|
| Дата | 2025-01-25 |
| Автор | mintusovayu |
| Статус | accepted |
| Supersedes | — |
| Superseded-by | — |
| Связанные ADR | ADR-005 (DocumentIndexPlan), ADR-003 (Domain Text Normalization) |
| Фаза внедрения | 8 |

---

## Контекст

Фаза 5 (ADR-005) вывела SQL-строки **write-side** из
application-слоя: `query_builder.build_document_queries` был
заменён доменной моделью `DocumentIndexPlan`, а формирование
SQL перенесено в `SqliteIndexWriter` (infrastructure). Это
устранило протечку деталей схемы БД в application для операций
записи.

Однако **read-side** SQL остался в application-слое. Три модуля
продолжали выполнять SQL-запросы к таблицам `documents` и
`text_index_fts` напрямую через `IDatabase.execute(...)`:

- `TextIndexer.get_document_by_hash`:
  `SELECT doc_id FROM documents WHERE file_hash = ?`.
- `TextIndexer.get_document_by_path`:
  `SELECT doc_id FROM documents WHERE file_path = ?`.
- `TextIndexer.get_document_metadata`:
  `SELECT doc_id, file_hash, cached_size, cached_mtime
  FROM documents WHERE file_path = ?`.
- `SearchEngine.get_document_info`:
  `SELECT doc_id, file_path, file_hash, file_size, page_count,
  indexed_at, last_modified FROM documents WHERE doc_id = ?`.
- `SearchEngine.get_page_text`:
  `SELECT text_content FROM text_index_fts
  WHERE doc_id = ? AND page_number = ?`.
- `DocumentCache.load`:
  `SELECT doc_id, file_path, file_hash, cached_size, cached_mtime
  FROM documents`.

**Проблема.** Эти SQL-строки **знают**:

- имена таблиц (`documents`, `text_index_fts`);
- имена колонок (`doc_id`, `file_path`, `file_hash`, `cached_size`,
  `cached_mtime`, `page_count`, `indexed_at`, `last_modified`,
  `text_content`, `page_number`);
- порядок и количество `?`-плейсхолдеров.

Всё это — **специфика конкретного бэкенда БД** (SQLite).
Application-слой формально не должен знать о схеме БД: это
нарушение слоистости и инверсии зависимостей, идентичное по
классу тому, что ADR-005 устранил для write-side.

**Признаки долга в коде.**

1. **Протечка схемы.** `TextIndexer`, `SearchEngine` и
   `DocumentCache` знают точные имена колонок; при добавлении
   нового поля в `documents` правки требуются в нескольких
   application-модулях.
2. **Нельзя заменить бэкенд БД.** При переходе на PostgreSQL
   придётся переписать все read-методы application-слоя,
   несмотря на наличие абстракции `IDatabase`.
3. **Дублирование между модулями.** Запрос
   `SELECT doc_id, file_hash, cached_size, cached_mtime
   FROM documents WHERE file_path = ?` присутствует и в
   `TextIndexer.get_document_metadata`, и (по смыслу) в
   `DocumentCache`. Нет единой точки read-side SQL.
4. **Диспропорция с write-side.** ADR-005 создал `IIndexWriter`
   для записи, но для чтения аналогичной абстракции не было:
   application-слой читал через общий `IDatabase.execute`,
   доступный и infrastructure-слою.

**Контекст среды.** DDS работает в single-worker режиме
uvicorn (см. `docs/deployment.md`). Python 3.14. Linux. SQLite
FTS5 — единственный поисковый бэкенд на момент Фазы 8, но
архитектура `ISearchBackend` предполагает замену, и симметрия
write-side/read-side важна для будущей миграции.

---

## Альтернативы

### Альтернатива 1: Оставить read-side SQL в application

**Описание.** Не менять существующую архитектуру. `TextIndexer`,
`SearchEngine` и `DocumentCache` продолжают выполнять SQL
напрямую через `IDatabase.execute(...)`.

**Плюсы.**
- Никаких изменений в коде.
- Ноль рисков регресса.
- `IDatabase.execute` уже является абстракцией — формально
  application зависит от интерфейса, а не от `SQLiteAdapter`.

**Минусы.**
- Протечка схемы БД сохраняется: SQL-строки с именами колонок
  продолжают жить в application.
- Дисбаланс с write-side: ADR-005 закрыл write, read остаётся.
- Замена бэкенда БД потребует правок во всех read-методах.
- Дублирование read-запросов между модулями.
- Не соответствует принципу «application не знает о конкретном
  бэкенде», сформулированному в ADR-005.

**Итог.** Отклонена. Долг реален и запланирован к устранению
в плане рефакторинга v9.

### Альтернатива 2: `IDocumentRepository` + `DocumentMetadata` (принята)

**Описание.** Ввести доменный Protocol `IDocumentRepository`,
доменную модель `DocumentMetadata` (frozen dataclass) и
реализацию `SqliteDocumentRepository` в infrastructure.
Application-слой работает с Protocol, не зная о схеме БД.

Методы Protocol:

- `get_by_hash(file_hash) -> str | None`
- `get_by_path(file_path) -> str | None`
- `get_metadata(file_path) -> DocumentMetadata | None`
- `get_by_id(doc_id) -> Document | None`
- `load_all() -> dict[str, DocumentMetadata]`
- `get_page_text(doc_id, page_number) -> str`

**Плюсы.**
- Симметрия с ADR-005: write-side — `IIndexWriter`, read-side —
  `IDocumentRepository`. Оба интерфейса в domain.
- Замена бэкенда БД возможна без правок application: достаточно
  реализовать новый `IDocumentRepository`.
- Типизация: `DocumentMetadata` — frozen dataclass с именованными
  полями; устранён риск перепутать порядок элементов в кортеже.
- Единая точка read-side SQL: вся схема сосредоточена в
  `SqliteDocumentRepository`.
- Тестируется в изоляции: `SqliteDocumentRepository` — 16 тестов
  на реальной SQLite.
- Соответствует принципу «application работает через абстракции
  domain» (контракт `application-isolation`).

**Минусы.**
- Breaking change для `TextIndexer.__init__`, `SearchEngine.__init__`,
  `DocumentCache.__init__` (параметр `db: IDatabase` → `document_repository:
  IDocumentRepository`).
- Дополнительный Protocol в domain.
- `MetadataFilterQueryBuilder` остаётся в application — не
  покрыт этим ADR (см. «Известные ограничения»).
- Tuple-сигнатура `ITextIndexer.get_document_metadata` сохранена
  (для минимизации blast radius) — двойное представление
  метаданных: `DocumentMetadata` в репозитории и tuple в
  Protocol.

**Итог.** Принята.

### Альтернатива 3: Расширить `IIndexWriter` до read+write

**Описание.** Не вводить новый Protocol, а добавить read-методы
в существующий `IIndexWriter`.

**Плюсы.**
- Один интерфейс для всех операций с БД.
- Меньше сущностей в domain.

**Минусы.**
- **Смешение ответственностей.** `IIndexWriter` изначально
  спроектирован как **write-контракт** (ADR-005). Добавление
  read-методов размывает смысл названия и назначения.
- **Разные потребители.** `IIndexWriter` внедряется в
  `TextIndexer` для записи планов; read-side нужен также в
  `SearchEngine` и `DocumentCache`. Один интерфейс будет
  внедряться шире, чем нужно.
- **Нарушение Interface Segregation Principle.** Класс-реализация
  обязан реализовать все методы, даже если используется только
  часть.
- При замене бэкенда БД смешение write и read усложняет
  миграцию: например, можно представить read из PostgreSQL
  и write в SQLite (для перехода), но объединённый интерфейс
  это затруднит.

**Итог.** Отклонена. Write- и read-контракты должны быть
разделены по ISP и по аналогии с ADR-005.

### Альтернатива 4: Перенести read-side SQL в domain

**Описание.** Поместить SQL-строки в domain-модуль (например,
`dds_core/domain/queries.py`), application импортирует их.

**Плюсы.**
- SQL выведен из application (формально).
- Единая точка для всех SQL-строк.

**Минусы.**
- **Смешение слоёв в другом направлении.** Domain не должен
  знать о схеме БД (см. ADR-003 — тот же принцип для нормализации
  текста). SQL — специфика конкретного бэкенда, а не доменное
  правило.
- **Противоречит ADR-005.** ADR-005 явно вывел SQL из domain в
  infrastructure через `SqliteIndexWriter`. Перенос read-side
  SQL в domain — шаг назад.
- Домен станет зависимым от SQLite-специфики (`SELECT`,
  `WHERE`, имена таблиц), что нарушает `domain-isolation`.

**Итог.** Отклонена. Противоречит слоистой архитектуре DDS.

### Альтернатива 5: `list[dict]` вместо `DocumentMetadata`

**Описание.** Protocol возвращает `list[dict[str, Any]]` или
`dict[str, Any]` — без доменной модели.

**Плюсы.**
- Нет новой доменной модели.
- Быстро реализуется.
- Pickle-совместимо.

**Минусы.**
- **Потеря типизации.** `dict[str, Any]` не проверяется mypy;
  опечатка в ключе (`"page_num"` вместо `"page_number"`)
  обнаруживается только в runtime.
- **Нет самодокументируемости.** Структура dict не видна из
  типа; приходится читать docstring или implementation.
- **Нет инвариантов.** `frozen=True` невозможен; мутация
  возможна по ошибке.
- **Проигрыш по тестируемости.** `DocumentMetadata` — явный
  dataclass, сравнение двух метаданных через `==` работает из
  коробки; dict требует ручной проверки каждого поля.

**Итог.** Отклонена. Доменная модель — правильный инструмент
для структурированных данных.

### Альтернатива 6: Динамическая фабрика SQL через `Callable`

**Описание.** Передавать SQL-строки как `Callable` в конструктор
application-класса. Application не содержит SQL-литералов, но
вызывает фабрику, реализованную в infrastructure.

**Плюсы.**
- Меньше изменений в сигнатурах классов (один параметр вместо
  нового Protocol).
- SQL-литералы физически в infrastructure.

**Минусы.**
- **Протечка через тип.** Application работает с
  `list[tuple[str, tuple]]` — типом, семантика которого
  «SQL-строки и параметры». Тип не доменный.
- **Pickle-несовместимость.** Фабрика из infrastructure не
  сериализуется через `pickle` для передачи в subprocess; в
  `ScanPipeline` пришлось бы хранить глобальную ссылку или
  дериватив, что снова связывает application с infrastructure.
- **Слабая типизация.** `Callable` не даёт контрактных гарантий:
  любой callable с подходящей сигнатурой пройдёт, даже если
  возвращает не тот формат.
- **Не устраняет дисбаланс с write-side.** `IIndexWriter` —
  явный Protocol, а `Callable` — нет; архитектура становится
  несимметричной.

**Итог.** Отклонена. Доменный Protocol — более явный и
типобезопасный контракт.

---

## Решение

Применяется **Альтернатива 2**: доменный Protocol
`IDocumentRepository`, доменная модель `DocumentMetadata`,
реализация `SqliteDocumentRepository` в infrastructure.

### Ключевые детали реализации

**Домен — `DocumentMetadata`** (`dds_core/domain/models.py`):

```python
@dataclass(frozen=True)
class DocumentMetadata:
    doc_id: str
    file_hash: str
    cached_size: int
    cached_mtime: str
```

`frozen=True` обеспечивает иммутабельность (hashability,
pickle-совместимость, безопасность при передаче между слоями).

**Домен — `IDocumentRepository`** (`dds_core/domain/interfaces.py`):

```python
@runtime_checkable
class IDocumentRepository(Protocol):
    def get_by_hash(self, file_hash: str) -> str | None: ...
    def get_by_path(self, file_path: str) -> str | None: ...
    def get_metadata(self, file_path: str) -> DocumentMetadata | None: ...
    def get_by_id(self, doc_id: str) -> Document | None: ...
    def load_all(self) -> dict[str, DocumentMetadata]: ...
    def get_page_text(self, doc_id: str, page_number: int) -> str: ...
```

`@runtime_checkable` позволяет проверять реализацию через
`isinstance()` (используется в тестах на LSP).

**Infrastructure — `SqliteDocumentRepository`** (новый файл
`dds_core/infrastructure/sqlite_document_repository.py`):

Единственная точка SQL-запросов к таблицам `documents` и
`text_index_fts` для read-операций. Использует `IDatabase.execute`
(не `sqlite3.Connection` напрямую) — это сохраняет единый пул
соединений и адаптер событий БД (`DatabaseQuerySlow`,
`DatabaseError`).

**Application — обновления:**

- `TextIndexer.__init__(text_extractor, index_writer, document_repository)`.
  Параметр `db: IDatabase` заменён на `document_repository:
  IDocumentRepository`. Методы `get_document_by_hash`,
  `get_document_by_path`, `get_document_metadata` делегируют в
  репозиторий.
- `SearchEngine.__init__(backend, document_repository)`.
  Методы `get_document_info` и `get_page_text` делегируют в
  репозиторий; логика LRU-кэша сохранена.
- `DocumentCache.__init__(document_repository)`. Метод `load`
  использует `IDocumentRepository.load_all()`; публичные
  `get_by_path`, `get_doc_id_by_hash`, `register_hash` — без
  изменений.

**Composition root — `dds_web/lifespan.py`:**

`create_components` создаёт `SqliteDocumentRepository(db)` и
передаёт его в `TextIndexer`, `SearchEngine`, `DocumentCache`.
`document_repository` добавлен в возвращаемый словарь
`components`.

### Инварианты

- **Идемпотентность.** Все методы `IDocumentRepository` не
  изменяют состояние БД. `get_*` возвращают данные или `None` /
  пустую строку; повторный вызов даёт тот же результат.
- **Иммутабельность `DocumentMetadata`.** `frozen=True`:
  экземпляры нельзя мутировать. Обеспечивает безопасность
  передачи через слои и pickle-сериализацию.
- **Сохранение tuple-сигнатуры `ITextIndexer.get_document_metadata`.**
  Метод по-прежнему возвращает
  `tuple[str, str, int, str] | None`. Причина: сигнатура
  зафиксирована в Protocol и используется в
  `ScanPipeline._hash_single_file`. Замена на `DocumentMetadata`
  отложена — минимизация blast radius.
- **Единая точка SQL.** Все read-запросы к `documents` и
  `text_index_fts` сосредоточены в `SqliteDocumentRepository`.
  `MetadataFilterQueryBuilder` — исключение (см. ограничения).
- **LSP-совместимость.** `SqliteDocumentRepository` —
  структурный подтип `IDocumentRepository`. Проверяется тестом
  `test_repository_is_lsp_compliant`.

---

## Последствия

### Положительные

- **Application-слой без SQL на read.** `TextIndexer`,
  `SearchEngine` и `DocumentCache` больше не содержат SQL-строк
  и не знают имён колонок.
- **Симметрия архитектуры.** Write-side — `IIndexWriter`
  (ADR-005), read-side — `IDocumentRepository` (ADR-008).
- **Замена бэкенда БД без правок application.** Достаточно
  реализовать новый `IDocumentRepository`.
- **Типизация.** `DocumentMetadata` — frozen dataclass с
  именованными полями; mypy ловит опечатки в именах полей.
- **Изоляция тестирования.** `SqliteDocumentRepository` — 16
  тестов на реальной SQLite (`tests/test_sqlite_document_repository.py`).
- **Единая точка read-запросов.** При добавлении нового столбца
  в `documents` правится только `SqliteDocumentRepository`.
- **Согласованность с контрактом `application-isolation`.**
  Application зависит только от Protocol из domain.

### Отрицательные

- **Breaking change для трёх конструкторов.**
  `TextIndexer.__init__`, `SearchEngine.__init__`,
  `DocumentCache.__init__` изменили третий параметр. В текущем
  проекте прямых потребителей, кроме `lifespan.py`, нет;
  кастомные реализации в `dds_modules/` не затронуты (модули
  работают через `IModule` / `ISecondaryProcessor`).
- **Tuple-сигнатура `ITextIndexer.get_document_metadata`
  сохранена.** Двойное представление метаданных
  (`DocumentMetadata` в репозитории, tuple в Protocol). Это
  осознанный компромисс для минимизации изменений в
  `ScanPipeline`.
- **Дополнительный слой абстракции.** `DocumentCache.load`
  выполняет `DocumentMetadata → tuple` конверсию. Стоимость O(N)
  при загрузке, пренебрежимо мала по сравнению с самим
  SELECT-запросом.
- **Дисбаланс: `MetadataFilterQueryBuilder` остаётся в
  application.** См. «Известные ограничения».

### Нейтральные

- **Новый файл:** `dds_core/infrastructure/sqlite_document_repository.py`.
- **Новый Protocol:** `IDocumentRepository` в
  `dds_core/domain/interfaces.py`.
- **Новая доменная модель:** `DocumentMetadata` в
  `dds_core/domain/models.py`.
- **`docs/DEPRECATIONS.yaml`:** 3 записи Фазы 8
  (`direct_removal`).
- **`docs/architecture.md`:** обновлены разделы «Слоистая
  архитектура», «Единственная точка реализации», «Список
  ADR», «Известные ограничения».
- **Тестов добавлено:** 16 (`tests/test_sqlite_document_repository.py`).
- **Общее число тестов:** 366 → 382 (380 non-smoke + 17 smoke).
- **Mypy:** 80 → 83 source files.

### Известные ограничения

- **`MetadataFilterQueryBuilder` остаётся в application.**
  Класс формирует SQL-фрагменты WHERE-условий
  (`d.object_code = ?`, `d.unmatched_flag = 1`) и используется
  только из infrastructure (`FTS5SearchBackend`). Формально он
  содержит SQL-литералы, но:

  - это **shared helper**, а не реализация интерфейса domain;
  - его потребитель — infrastructure, а не application;
  - перенос в infrastructure возможен, но не входит в объём
    Фазы 8 (низкий приоритет, работает корректно).

  См. ADR-005, где `MetadataFilterQueryBuilder` упомянут как
  допустимое исключение `infrastructure → application` в
  контракте `infrastructure-isolation`.

- **Tuple-сигнатура `ITextIndexer.get_document_metadata`.**
  Замена на `DocumentMetadata | None` отложена. Потребует
  правок в Protocol, `TextIndexer`, `ScanPipeline._hash_single_file`,
  `DocumentCache` (согласованность формата). Задача будущей
  фазы (например, Фаза 9+).

- **`DocumentCache` хранит tuple, не `DocumentMetadata`.**
  Согласовано с `ScanPipeline._hash_single_file`, который
  ожидает 4-элементный кортеж от `get_by_path`. Преобразование
  выполняется один раз при `load()` — минимизирует изменения
  в конвейере.

- **`SqliteDocumentRepository` использует `IDatabase.execute`,
  не `sqlite3.Connection`.** Это осознанное решение:
  сохраняет адаптер событий БД (`DatabaseQuerySlow`,
  `DatabaseError`) и единый пул соединений. Прямой `sqlite3`
  был бы быстрее, но потерял бы наблюдаемость и потокобезопасность
  пула.

- **`load_all()` не фильтрует данные.** Метод возвращает все
  записи `documents`, даже если часть из них не нужна. Для
  большого каталога (10 000+ документов) это может занимать
  несколько МБ памяти. В текущей архитектуре это приемлемо:
  `DocumentCache` используется только в фазе хеширования,
  когда все документы необходимы.

---

## Ссылки

- **Реализация:**
  - `dds_core/domain/models.py` — `DocumentMetadata`.
  - `dds_core/domain/interfaces.py` — `IDocumentRepository`.
  - `dds_core/infrastructure/sqlite_document_repository.py` —
    `SqliteDocumentRepository`.
  - `dds_core/application/indexer.py` — `TextIndexer` с
    `IDocumentRepository`.
  - `dds_core/application/search_engine.py` — `SearchEngine` с
    `IDocumentRepository`.
  - `dds_core/application/document_cache.py` — `DocumentCache` с
    `IDocumentRepository`.
  - `dds_web/lifespan.py` — создание `SqliteDocumentRepository`,
    передача в application-компоненты.
  - `tests/benchmarks/bench_fast.py` — обновлённая fixture
    `search_engine`.
  - `tests/benchmarks/bench_slow.py` — обновлённый `scan_once`.
- **Тесты:**
  - `tests/test_sqlite_document_repository.py` — 16 тестов
    (все методы Protocol, `frozen`-проверка, LSP-проверка).
- **Изменённая документация:**
  - `docs/DEPRECATIONS.yaml` — 3 записи `direct_removal`.
  - `docs/architecture-decisions/README.md` — ADR-008 в индексе.
  - `docs/architecture-decisions/current_phase.txt` — `8`.
  - `docs/architecture.md` — обновлены разделы про слои,
    точки реализации, ADR.
- **Связанные ADR:**
  - ADR-005 (DocumentIndexPlan) — аналог для write-side;
    паттерн «перенос SQL из application в infrastructure».
  - ADR-003 (Domain Text Normalization) — принцип «доменное
    правило не зависит от схемы БД».
- **Внешние материалы:**
  - Martin Fowler, «Inversion of Control Containers and the
    Dependency Injection pattern» (2004) — обоснование
    разделения write- и read-контрактов.
  - Robert C. Martin, «Clean Architecture» (2017) — принцип
    «application не знает о схеме БД».
  - Robert C. Martin, «The Interface Segregation Principle» —
    обоснование отдельного Protocol для read-side (вместо
    расширения `IIndexWriter`).

---
