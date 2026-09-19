# `docs/architecture.md`

```markdown
## Обзор

Deep Doc Search (DDS) — расширяемая система полнотекстового поиска
и анализа рабочей документации (РД). Система индексирует PDF-документы,
извлекает текстовый слой и обеспечивает полнотекстовый поиск через
веб-интерфейс.

Основная функциональность ядра:
- Полнотекстовый поиск по документам РД.
- Расширяемая модульная архитектура для анализа документов.
- Веб-интерфейс для поиска и управления.
- **Фильтрация результатов по метаданным, извлечённым из имён файлов**
  (код объекта, код дисциплины, код типа документа, режим «только
  несоответствующие»). Фильтры представлены полями ввода с
  автодополнением, поддерживающими поиск по кодам, русским и
  английским псевдонимам.
- **Батчевое обновление метаданных документов** — все документы
  обновляются одной транзакцией (``execute_write_many``) с однократной
  загрузкой справочников, что устраняет N × COMMIT при больших каталогах.
- **Нечувствительный к раскладке клавиатуры поиск** — за счёт
  нормализации текста в дополнительной колонке FTS5.
- **Серверная группировка результатов поиска по документу** — одна
  запись в выдаче соответствует одному документу, все страницы
  которого с совпадениями агрегируются в массив ``pages``.
- **Предпросмотр документа с подсветкой совпадений** — растровый
  рендер страницы PDF (PNG) с наложением слоя подсветки
  для терминов, извлечённых из сниппетов FTS5.
- **Диагностика и коррекция системы координат подсветки** — при
  обнаружении аномальной системы координат (bottom-left с
  перепутанными осями) координаты подсветки автоматически
  трансформируются; пользователь может управлять коррекцией
  через чекбокс «Ротация координат».
- **Адаптивный режим просмотра** — при открытии страница
  автоматически вписывается по ширине; масштабирование
  (``Ctrl + колесо``) и панорамирование (перетаскивание правой
  кнопкой мыши) работают без изменения layout контейнера.
- **Два режима навигации по страницам** — по всем страницам
  документа или только по страницам с найденными совпадениями
  (переключается чекбоксом на вкладке документа).
- **Пакетная загрузка текста страниц** — эндпоинт
  ``POST /api/documents/pages/batch`` возвращает тексты страниц
  для нескольких документов одним HTTP-запросом; используется UI
  при пустом поисковом запросе (16 HTTP-запросов → 1).
- **LRU-кэш текста страниц** — ``SearchEngine.get_page_text`` кэширует
  результаты с автоинвалидацией по ``file_hash``.
- **Graceful shutdown** — при остановке пулы завершаются через
  ``shutdown(wait=True, cancel_futures=True)``; активные задачи
  дожидаются завершения до закрытия БД.
- **Управление паролями через env-переменные** — пароли задаются
  через ``DDS_USER_<NAME>_PASSWORD`` или пару
  ``_PASSWORD_HASH`` + ``_PASSWORD_SALT``; ``config.json`` исключён
  из репозитория.

Дополнительно внедрена **событийная модель** для логирования, мониторинга
и обновления интерфейса в реальном времени.

## Архитектурные принципы

### Инверсия зависимостей

Все компоненты зависят от абстракций (интерфейсов), а не от конкретных
реализаций. Конкретные реализации передаются через конструкторы
(dependency injection).

```text
┌─────────────────────────────────────────────────┐
│              presentation layer                 │
│         (dds_web/api.py, dds_web/pages.py,      │
│          dds_web/lifespan.py, dds_web/auth.py,  │
│          dds_web/auto_login.py,                 │
│          dds_web/security.py)                   │
│                                                 │
│    Зависит от application layer через           │
│    dependency injection                         │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│              application layer                  │
│    (search_engine, indexer, scan_orchestrator,  │
│     scan_pipeline, module_lifecycle,            │
│     module_loader, dependency_checker,          │
│     query_builder, document_cache,              │
│     extract_worker, progress_tracker,           │
│     timeout_guard, async_utils,                 │
│     file_name_parser,                           │
│     document_metadata_service,                  │
│     metadata_filter_query_builder,              │
│     reference_data_service,                     │
│     reference_data_initializer,                 │
│     text_normalizer,                            │
│     search_query_normalizer,                    │
│     highlights_service,                         │
│     coordinate_diagnostics,                     │
│     word_index_cache)                           │
│                                                 │
│    Зависит от domain/interfaces.py              │
│    Не знает о конкретных реализациях            │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│              infrastructure layer               │
│    (sqlite_adapter, connection_pool,            │
│     connection_factory, fts5_search_backend,    │
│     file_scanner, file_hasher,                  │
│     pymupdf_text_extractor,                     │
│     event_bus, logging_subscriber,              │
│     sqlite_reference_repository,                │
│     database)                                   │
│                                                 │
│    Реализует интерфейсы из domain/interfaces.py │
└─────────────────────────────────────────────────┘
```

### Единственная точка реализации

Каждая функциональность реализована в одном месте:
- Полнотекстовый поиск — `fts5_search_backend.py`.
- Хеширование файлов — `file_hasher.py`.
- Извлечение текста, рендер страницы и построение индекса слов —
  `pymupdf_text_extractor.py`.
- Сканирование каталога — `file_scanner.py`.
- Запись в БД — `sqlite_adapter.py`.
- Публикация событий — `event_bus.py`.
- Логирование через события — `logging_subscriber.py`.
- Безопасность веб-слоя — `dds_web/security.py`.
- Таймауты блокирующих операций — `timeout_guard.py`.
- Выполнение блокирующих операций в executor — `async_utils.py`.
- **Парсинг имени файла** — `dds_core/application/file_name_parser.py`.
- **Построение условий фильтрации** — `dds_core/application/metadata_filter_query_builder.py`.
- **Обновление метаданных документов** — `dds_core/application/document_metadata_service.py`.
- **Доступ к справочникам** — `dds_core/infrastructure/sqlite_reference_repository.py`.
- **Нормализация текста для поиска** — `dds_core/application/text_normalizer.py`.
- **Нормализация поискового запроса** — `dds_core/application/search_query_normalizer.py`.
- **Поиск совпадений для подсветки** — `dds_core/application/highlights_service.py`.
- **Диагностика и коррекция системы координат подсветки** —
  `dds_core/application/coordinate_diagnostics.py`.
- **Кэширование индексов слов** — `dds_core/application/word_index_cache.py`.
- **Кэширование текста страниц** — `SearchEngine.get_page_text` в
  `dds_core/application/search_engine.py` (LRU с ключом
  `(doc_id, page, file_hash)`).
- **Построение имён env-переменных для паролей** —
  `dds_web/lifespan.py::_env_var_name`.
- **Загрузка паролей из env** — `dds_web/lifespan.py::create_auth_service`.

### Событийная модель (Фаза 5)

Ядро и presentation layer взаимодействуют через шину событий.
Производители публикуют события, подписчики получают их асинхронно.

Основные компоненты:
- `Event` (базовый класс) и конкретные события в `dds_core/domain/events.py`.
- `AsyncEventBus` — асинхронная потокобезопасная шина.
- `LoggingSubscriber` — подписчик, преобразующий события в лог-записи.

### Архитектурные компромиссы

Ниже перечислены осознанно принятые компромиссы, которые могут
выглядеть как нарушения «идеальной» слоистой архитектуры, но
оправданы практическими соображениями.

#### `normalize_text`: infrastructure → application

Модуль `dds_core/infrastructure/pymupdf_text_extractor.py`
импортирует функцию `normalize_text` из
`dds_core/application/text_normalizer.py`. Формально это нарушает
направление зависимостей (infrastructure зависит от application).

Оправдание:
- `normalize_text` — чистая функция без побочных эффектов и
  внешних зависимостей.
- Её назначение — быть **единым контрактом нормализации** между
  инфраструктурой (индексация, построение `WordIndex`),
  application (поиск, сниппеты) и presentation (подсветка).
- Дублирование функции в домене или инфраструктуре привело бы
  к риску рассинхронизации правил нормализации.

#### `extract_worker`: application → infrastructure

Модуль `dds_core/application/extract_worker.py` импортирует
`PyMuPDFTextExtractor` из
`dds_core/infrastructure/pymupdf_text_extractor.py`. Это
осознанное нарушение слоистости, обусловленное требованием
сериализуемости для `ProcessPoolExecutor`: дочерний процесс не
может получить доступ к объектам основного процесса, поэтому
создаёт экстрактор самостоятельно.

Оправдание:
- Функция `extract_document_queries` — точка входа для дочернего
  процесса; она обязана создавать зависимости внутри процесса.
- Инверсия зависимостей здесь невозможна без усложнения
  (передача фабрики через pickle, что само по себе проблематично).
- Замена PDF-библиотеки потребует правки этого модуля и
  `pymupdf_text_extractor.py` — обе точки известны и локальны.

#### `window.DDSApp` — фасад над `AppState`

Модуль `dds_web/static/js/app.js` инкапсулирован в IIFE, поэтому
переменная `AppState` недоступна из других модулей напрямую.
Вместо расширения области видимости введён публичный фасад
`window.DDSApp` с методами доступа к per-tab состоянию
(`getDocumentRecord`, `updateDocumentRecord`, `getActiveDocumentRecord`,
`incrementLoadSeq`, `getLoadSeq`, `ensureRenderer`).

Оправдание:
- Сохраняет инкапсуляцию: внешние модули не манипулируют
  внутренними структурами `AppState` напрямую.
- Позволяет позже изменить внутреннее представление состояния
  без правки всех потребителей.

#### Единый счётчик `_seq` для координации async-операций

Каждая вкладка документа имеет счётчик `_seq` в
`AppState.openDocuments[i]`. Все асинхронные операции (запрос
PNG-рендера, запрос подсветки, загрузка текста страницы) проверяют
актуальность счётчика после каждого `Promise`.

Оправдание:
- Устраняет race condition при быстрой смене страниц или режимов
  просмотра.
- Предотвращает утечку blob URL (устаревший ответ не создаёт
  объект).
- Проще, чем отмена через `AbortController` (не требует
  поддержки в каждом fetch-вызове).

#### Публичные методы `refreshHighlights` и `applyTextFallbackState`

Ранее `dds_web/static/js/app.js` и `dds_web/static/js/document.js`
вызывали приватные методы друг друга
(`PageRenderer._fetchAndApplyHighlights`, `ViewModeManager._setActive`),
что нарушало инкапсуляцию. Введены публичные методы:

- `PageRenderer.refreshHighlights(pageNumber, terms)` —
  инкрементирует `_seq` через фасад `window.DDSApp` и
  делегирует в приватный `_fetchAndApplyHighlights`.
- `ViewModeManager.applyTextFallbackState(docId)` — применяет
  fallback-состояние UI (активна «Текст», «Рендер» заблокирована).

Оправдание:
- Единая точка контроля инкремента `_seq` — предотвращает
  двойной инкремент и race condition.
- Инкапсуляция деталей (внутренний метод остаётся приватным).
- Публичный API отражает реальные сценарии использования извне.

#### Отмена `ScanPipeline` через `asyncio.Event`

Механика кооперативной отмены построена на `asyncio.Event`
(`_cancel_event`), а не на булевом флаге. Воркеры ожидают задачу
и отправляют результаты через симметричные помощники
`_wait_task_or_cancel` / `_put_or_cancel`, использующие
`asyncio.wait` с приоритетом отмены.

Оправдание:
- Булев флаг требовал опроса в каждой итерации цикла, что
  приводило к race condition: воркер мог заблокироваться на
  `await queue.put()` и не увидеть флаг.
- `asyncio.Event` разблокирует и `get`, и `put` немедленно при
  установке отмены.
- Приоритет отмены при одновременной готовности обеих задач
  (`queue.get()` и `cancel_event.wait()`) предотвращает
  обработку задачи после отмены.

#### Сентинелы как уникальный тип `_QueueSentinel`

Маркер завершения очереди — экземпляр класса `_QueueSentinel`,
а не `None`. Это позволяет различить сентинел и сигнал
кооперативной отмены (который возвращается как `None`).

Оправдание:
- При использовании `None` в качестве сентинела оба сигнала
  совпали бы — воркер не смог бы отличить нормальное завершение
  от отмены.
- mypy видит `_QueueSentinel` как конкретный тип, а не абстрактный
  `object`, что ловит ошибки типизации.

#### Диагностика координат в `HighlightsService`, а не в
`build_word_index`

Диагностика системы координат и трансформация bbox выполняются
в момент поиска совпадений (`HighlightsService.search_highlights`),
а не при построении `WordIndex` (в инфраструктурном слое).

Оправдание:
- Кэш `WordIndexCache` работает по единому ключу
  `(doc_id, page_number, file_hash)` без флага трансформации:
  раздувание памяти неоправданно (трансформация дешёвая).
- `ITextDocument` и `build_word_index` остаются чистыми: они не
  знают о диагностике и работают только с PyMuPDF.
- Трансформируются только **найденные** слова (обычно единицы),
  а не все слова страницы — экономия CPU.

## Структура проекта

```text
dds/
├── dds_core/
│   ├── domain/
│   │   ├── config.py
│   │   ├── events.py
│   │   ├── interfaces.py
│   │   ├── models.py
│   │   └── reference_repository.py
│   ├── application/
│   │   ├── async_utils.py
│   │   ├── search_engine.py
│   │   ├── indexer.py
│   │   ├── scan_orchestrator.py
│   │   ├── scan_pipeline.py
│   │   ├── module_loader.py
│   │   ├── dependency_checker.py
│   │   ├── module_lifecycle.py
│   │   ├── query_builder.py
│   │   ├── document_cache.py
│   │   ├── extract_worker.py
│   │   ├── progress_tracker.py
│   │   ├── timeout_guard.py
│   │   ├── file_name_parser.py
│   │   ├── document_metadata_service.py
│   │   ├── metadata_filter_query_builder.py
│   │   ├── reference_data_service.py
│   │   ├── reference_data_initializer.py
│   │   ├── text_normalizer.py
│   │   ├── search_query_normalizer.py
│   │   ├── highlights_service.py
│   │   ├── coordinate_diagnostics.py
│   │   └── word_index_cache.py
│   └── infrastructure/
│       ├── sqlite_adapter.py
│       ├── connection_pool.py
│       ├── connection_factory.py
│       ├── database.py
│       ├── fts5_search_backend.py
│       ├── file_scanner.py
│       ├── file_hasher.py
│       ├── event_bus.py
│       ├── logging_subscriber.py
│       ├── pymupdf_text_extractor.py
│       └── sqlite_reference_repository.py
├── dds_web/
│   ├── api.py
│   ├── auth.py
│   ├── auto_login.py
│   ├── pages.py
│   ├── lifespan.py
│   ├── security.py
│   ├── static/
│   │   ├── css/
│   │   │   ├── base.css
│   │   │   ├── theme-dark.css
│   │   │   ├── theme-coffee.css
│   │   │   ├── theme-forest.css
│   │   │   ├── theme-nord.css
│   │   │   ├── theme-ocean.css
│   │   │   ├── theme-purple.css
│   │   │   └── theme-sunset.css
│   │   └── js/
│   │       ├── debug_utils.js
│   │       ├── ui_state_manager.js
│   │       ├── sse.js
│   │       ├── document.js
│   │       ├── autocomplete.js
│   │       ├── filter_coordinator.js
│   │       ├── search.js
│   │       ├── settings.js
│   │       ├── theme_manager.js
│   │       └── app.js
│   └── templates/
│       ├── base.html
│       ├── main.html
│       └── login.html
├── tests/
│   ├── test_auth_env_password.py
│   ├── test_document_metadata_batch.py
│   ├── test_scan_pipeline_cancellation.py
│   ├── ... (прочие тесты)
├── config.json
├── config.json.example
├── run.py
└── docs/
    ├── architecture.md
    ├── deployment.md
    └── user_guide.md
```

## Модуль безопасности веб-слоя

Модуль `dds_web/security.py` содержит централизованные функции
безопасности, используемые в веб-слое (редиректы, cookie).

## Модуль автологина

Модуль `dds_web/auto_login.py` реализует автологин по IP-адресу
через middleware `auto_login_middleware`. Парсинг и валидация
конфигурации выполняются в `lifespan.py::create_auth_service` и
`auto_login.py::parse_auto_login_config`.

## Жизненный цикл приложения

**Startup:**

1. Чтение конфигурации.
2. Настройка файлового логирования.
3. Создание и запуск шины событий и подписчика логирования.
4. Создание пулов потоков и процессов.
5. Создание компонентов через `create_components()`:
   - стандартные компоненты;
   - **репозитории справочников** (`SqliteReferenceRepository` для объектов, дисциплин, типов);
   - **`DocumentMetadataService`**;
   - **`ReferenceDataService`**;
   - **`ReferenceDataInitializer`** — загружает справочники из JSON при пустой базе.
6. **Проверка возможностей SQLite** (`DatabaseManager._check_sqlite_capabilities`)
   при создании схемы. Требуется **SQLite ≥3.9** (FTS5 доступен
   начиная с этой версии). Дополнительно выполняется интеграционный
   пробник FTS5 в in-memory БД.
7. `DocumentMetadataService` передаётся в `ScanOrchestrator`, `ReferenceDataService` — в `APIContext`.
8. Инициализация модулей.
9. **Создание сервиса аутентификации** (`create_auth_service`):
   пароли читаются из env-переменных `DDS_USER_<NAME>_PASSWORD`
   или `DDS_USER_<NAME>_PASSWORD_HASH` + `_PASSWORD_SALT`;
   fallback — поле `password` в `config.json`. При старте
   проверяются коллизии env-имён.
10. Парсинг и валидация `auth.auto_login`; WARNING в stdout для
    невалидных записей.
11. Запуск фоновой очистки сессий.
12. Сброс зависших записей сканирования.
13. **Создание `HighlightsService` и `WordIndexCache`** для
    предпросмотра документа.
14. Создание `APIContext`.
15. Публикация `ApplicationStarted`.

**Shutdown:**

1. Публикация `ApplicationStopping`.
2. Отмена фоновых задач (очистка сессий).
3. Отмена сканирования и ожидание завершения с таймаутом
   `OPERATION_TIMEOUTS["scan.cancel_grace"]` (по умолчанию 5 секунд).
   При превышении — принудительная отмена задачи.
4. Остановка модулей.
5. **Очистка `WordIndexCache`** (вызов `clear()`).
6. **Завершение пулов потоков и процессов** через
   `shutdown(wait=True, cancel_futures=True)`:
   - `wait=True` — дождаться завершения активных задач;
   - `cancel_futures=True` — отменить ещё не начатые.
   Это гарантирует, что фоновые потоки не обратятся к БД
   после её закрытия.
7. Закрытие БД.
8. Публикация `ApplicationStopped`.
9. Остановка подписчика логирования и шины событий.
10. Очистка глобальных контекстов (`set_context(None)`,
    `set_auth_service(None)`).

## Разделение пулов потоков и процессов

| Пул | Тип | Назначение | Размер |
|---|---|---|---|
| `api_executor` | ThreadPoolExecutor | Веб-запросы | `API_EXECUTOR_MAX_WORKERS` (4) |
| `scan_executor` | ThreadPoolExecutor | Сканирование, рендер PDF, построение индексов слов | `SCAN_EXECUTOR_MAX_WORKERS` (6) |
| `extract_executor` | ProcessPoolExecutor | Извлечение текста | `SCAN_EXTRACT_WORKERS` (6) |

**Важно:** эндпоинты `/render` и `/highlights` выполняют блокирующие
операции в `scan_executor`, а не в `api_executor`. Это предотвращает
конкуренцию с веб-запросами: рендер A0 @ 300 dpi может занимать
несколько секунд, и его размещение в `api_executor` блокировало бы
обработку других HTTP-запросов. Эндпоинт `/api/documents/pages/batch`
использует `api_executor` (чтение из БД, не CPU-интенсивное).

## Таймауты блокирующих операций

Реестр `OPERATION_TIMEOUTS` задаёт таймауты для критичных операций.
При срабатывании публикуется событие `OperationTimedOut`.

**Примечание о непрерываемости потоков:**

При срабатывании таймаута операция в `ThreadPoolExecutor` или
`ProcessPoolExecutor` **не прерывается**: поток/процесс завершится
самостоятельно. `asyncio.wait_for` только перестаёт ждать результат.
Это означает, что клиент получает ответ 504 сразу, но ресурс
(поток/процесс) освобождается позже — когда операция фактически
завершится. При систематических таймаутах пул может оказаться
занятым «зависшими» операциями; для диагностики служат
`get_pool_stats()` и события `operation.timed_out` в логе.

## Схема базы данных

### Таблица `documents`

| Столбец | Тип | Описание |
|---|---|---|
| doc_id | TEXT PRIMARY KEY | Уникальный идентификатор документа |
| file_path | TEXT NOT NULL | Относительный путь к файлу |
| file_hash | TEXT NOT NULL UNIQUE | Хеш файла |
| file_size | INTEGER | Размер файла в байтах |
| page_count | INTEGER | Количество страниц |
| indexed_at | TEXT | Дата индексирования |
| last_modified | TEXT | Дата изменения файла |
| cached_size | INTEGER | Размер файла при индексировании (для ленивого хеширования) |
| cached_mtime | TEXT | Дата изменения файла при индексировании (для ленивого хеширования) |
| object_code | TEXT | Код объекта из имени файла |
| discipline_code | TEXT | Код дисциплины из имени файла |
| document_type_code | TEXT | Код типа документа из имени файла |
| unmatched_flag | INTEGER DEFAULT 0 | Флаг несоответствия справочникам |

**Индексы:**
- `idx_documents_file_path`
- `idx_documents_object_code`
- `idx_documents_discipline_code`
- `idx_documents_document_type_code`
- `idx_documents_unmatched_flag`

(Индекс `idx_documents_file_hash` не создаётся: UNIQUE constraint
на колонке `file_hash` обеспечивает автоиндекс SQLite.)

### Таблицы справочников

Для каждой категории (объект, дисциплина, тип документа) создаются
две таблицы: основная таблица кодов и таблица псевдонимов с колонкой
языка.

#### Основная таблица (например, `object_reference`)

| Столбец | Тип | Описание |
|---|---|---|
| code | TEXT PRIMARY KEY | Канонический код |
| description | TEXT NOT NULL DEFAULT '' | Описание кода |

#### Таблица псевдонимов (например, `object_reference_alias`)

| Столбец | Тип | Описание |
|---|---|---|
| alias | TEXT PRIMARY KEY | Псевдоним |
| code | TEXT NOT NULL | Связанный код (внешний ключ) |
| lang | TEXT | Язык псевдонима: `en`, `ru` или `NULL` |

Внешний ключ: `FOREIGN KEY (code) REFERENCES object_reference(code) ON DELETE CASCADE`.

Индекс по `code` в таблице псевдонимов: `idx_object_reference_alias_code`.

Аналогично для `discipline_reference` и `document_type_reference`.

### Таблица `text_index_fts`

Полнотекстовый индекс через FTS5. Содержит четыре колонки:

| Колонка | Описание |
|---|---|
| doc_id | Идентификатор документа |
| page_number | Номер страницы (0-based) |
| text_content | Оригинальный текст страницы |
| normalized_text | Нормализованная версия текста для нечувствительного к раскладке поиска |

Нормализация выполняется при индексации с помощью модуля
`dds_core/application/text_normalizer.py`. Пользовательский запрос
нормализуется и дополняется префиксом колонки через
`dds_core/application/search_query_normalizer.py`.

### Таблица `module_registry`

Реестр модулей.

### Таблица `scan_state`

Состояние сканирования.

### Таблица `schema_version`

Версия схемы БД (текущая — **6**).

## Управление соединениями и потокобезопасность

Архитектура с пулом соединений (чтение) и выделенным соединением записи
осталась без изменений.

## Запуск сканирования и защита от гонок

Механизм `asyncio.Lock` и `_scan_task` сохранён.

## Прогресс сканирования

Живой прогресс из памяти конвейера, кэшированное состояние индексации.

## Поэтапное сканирование

### Фаза 1 (primary): индексирование текстового слоя

Без изменений: сканирование, ленивое хеширование, извлечение текста,
батчевая запись. При записи страницы в `text_index_fts` добавляется
нормализованная копия текста в колонку `normalized_text`.

**Отмена сканирования (скорректированный план):**
Механика кооперативной отмены построена на `asyncio.Event`
(`ScanPipeline._cancel_event`). Воркеры ожидают задачу через
`_wait_task_or_cancel` и отправляют результаты через
`_put_or_cancel`; оба метода используют `asyncio.wait` с
приоритетом отмены. При превышении порога `MAX_SCAN_ERRORS` или
таймауте каталога `_cancel_event` устанавливается автоматически.
Внешняя отмена через `task.cancel()` приводит к пробросу
`CancelledError` и публикации `ScanCancelled`.

### Фаза 2 (secondary): обновление метаданных и запуск модулей

В начале вторичного сканирования вызывается
`DocumentMetadataService.update_all_documents()`, который парсит имена
файлов и заполняет поля `object_code`, `discipline_code`,
`document_type_code`, `unmatched_flag`. **Все обновления выполняются
одной транзакцией** через `execute_write_many` (batch-write);
множества кодов справочников загружаются однократно. Далее выполняется
обработка модулями.

В процессе публикуются события `ScanSecondaryStarted`,
`ScanSecondaryProgressUpdated`, `ScanSecondaryCompleted`, которые
отображаются в статус-баре и разделе «Сканирование» панели настроек.

Повторный запуск обновления метаданных возможен через
`POST /api/scan/refresh-metadata`.

## Система модулей

Без изменений.

## Извлечение текста

Без изменений.

## Поисковый бэкенд

### FTS5

- Токенизатор `unicode61`.
- Ранжирование BM25.
- Фрагменты `snippet()` с нейтральными маркерами.
- Всегда выполняется JOIN с таблицей `documents` для получения
  `file_path` и применения фильтров.
- Фильтрация по метаданным добавляется через
  `MetadataFilterQueryBuilder`: коды объекта, дисциплины, типа документа,
  а также режим `unmatched_only`.
- Условия объединяются через `AND`. При `unmatched_only=True` все остальные
  фильтры игнорируются, добавляется только `d.unmatched_flag = 1`.

### Серверная группировка результатов поиска

Начиная с версии схемы 6 поиск возвращает **по одной записи на документ**
с массивом страниц:

```json
{
  "query": "...",
  "total": 42,
  "results": [
    {
      "doc_id": "...",
      "file_path": "раздел_01/чертёж_001.pdf",
      "relevance_score": -3.14,
      "pages": [
        {"page_number": 0, "snippet": "..."},
        {"page_number": 3, "snippet": "..."}
      ]
    }
  ]
}
```

**Семантика полей:**
- `total` — количество **уникальных документов**, соответствующих
  запросу и фильтрам.
- `limit` и `offset` в запросе также отсчитываются **по документам**.
- `pages[].page_number` — 0-based, соответствует
  `text_index_fts.page_number`.

**Двухзапросный подход.**

Серверная группировка реализована через **два последовательных
SQL-запроса** с Python-side агрегацией между ними.

**Query 1 — плоская выборка пар `(doc_id, rank)`:**

```sql
SELECT fts.doc_id AS doc_id,
       bm25(text_index_fts) AS rank
FROM text_index_fts fts
JOIN documents d ON fts.doc_id = d.doc_id
{filter_join_sql}
WHERE text_index_fts MATCH ?
  {filter_where_and}
```

Между Query 1 и Query 2 на Python выполняется:
- группировка по `doc_id` с вычислением `MIN(rank)` для каждого документа;
- сортировка документов по `(best_rank, doc_id)` — детерминированная
  пагинация;
- применение `limit`/`offset` по документам.

**Query 2 — страницы выбранных документов с сниппетами:**

```sql
SELECT fts.doc_id AS doc_id,
       d.file_path AS file_path,
       fts.page_number AS page_number,
       snippet(text_index_fts, 3, ?, ?, '...', ?) AS snippet
FROM text_index_fts fts
JOIN documents d ON fts.doc_id = d.doc_id
WHERE text_index_fts MATCH ?
  AND fts.doc_id IN ({placeholders})
ORDER BY fts.doc_id, fts.page_number
```

**Особенности SQL:**
- `MATCH` присутствует в **каждом** из двух запросов — это обязательное
  условие корректного вызова `bm25()` и `snippet()`.
- FTS5-функции `bm25()` и `snippet()` вызываются с **именем таблицы**
  `text_index_fts`, а не с алиасом `fts`.
- Агрегация `MIN(rank)` и пагинация выполняются на Python, а не в
  SQL.

**Research-шаг 3.1 плана рефакторинга v5.0 (результат: отрицательный).**

Проверялась возможность оптимизации SQL `LIMIT` в Query 1 через
подзапрос с `GROUP BY doc_id` и `MIN(bm25(...))`:

```sql
SELECT doc_id, MIN(rank) AS best_rank
FROM (
    SELECT fts.doc_id, bm25(text_index_fts) AS rank
    FROM text_index_fts fts
    JOIN documents d ON fts.doc_id = d.doc_id
    WHERE text_index_fts MATCH ?
)
GROUP BY doc_id
ORDER BY best_rank, doc_id
LIMIT ? OFFSET ?
```

Проверка на SQLite 3.40+ (целевая версия) дала ошибку
`unable to use function bm25 in the requested context`: FTS5
требует вызова `bm25()` в том же SELECT-блоке, где присутствует
`MATCH`, и не позволяет использовать функцию внутри подзапроса
с агрегацией. Оптимизация **не применяется**; двухзапросный
подход с Python-side агрегацией остаётся оптимальным.

**Требования к SQLite:**
- **SQLite ≥ 3.9** — с этой версии доступен FTS5.

**Ограничение по памяти:**
- **Query 1** возвращает плоский список пар `(doc_id, rank)` — по
  одной записи на каждую страницу-совпадение. При массовых
  совпадениях результат Query 1 может занимать десятки и сотни МБ
  памяти.
- **Query 2** возвращает страницы только для отобранных на
  предыдущем шаге документов (не более `limit` документов).

### Обработка пустого запроса

Если `query == ""`:
- Не используется FTS5 MATCH.
- Запрос строится напрямую по таблице `documents` с учётом всех
  фильтров.
- Используется `SELECT DISTINCT d.doc_id, d.file_path`.
- Каждый документ получает одну «виртуальную» страницу
  (`page_number=0`, пустой `snippet`).

### Нормализация текста для поиска

1. **При индексации** каждая страница сохраняется в двух колонках
   `text_index_fts`: `text_content` (оригинал) и `normalized_text`
   (нормализованная версия).
2. **При поиске** пользовательский запрос обрабатывается функцией
   `normalize_search_query()`, добавляющей префикс
   `normalized_text:` к каждому слову или фразе.
3. **Сниппеты** формируются из колонки `normalized_text` (индекс 3)
   и денормализуются на Python-стороне.

### Безопасный рендеринг сниппетов

Без изменений.

### Валидация идентификаторов

Без изменений.

### Комбинированный поиск

Поддерживается фильтрация по данным модулей (VIEW) и по метаданным.

### Подсчёт результатов

- Для непустого запроса: `SELECT COUNT(DISTINCT fts.doc_id)`.
- Для пустого запроса: `SELECT COUNT(DISTINCT d.doc_id)`.

## Веб-интерфейс

### Аутентификация и парольная политика

**Управление паролями через env (скорректированный план):**

Пароли загружаются из трёх источников в порядке приоритета:

1. `DDS_USER_<NAME>_PASSWORD` — открытый пароль из env.
2. `DDS_USER_<NAME>_PASSWORD_HASH` + `DDS_USER_<NAME>_PASSWORD_SALT` —
   готовый хеш и соль (PBKDF2-HMAC-SHA256, 64 hex-символа).
3. Поле `password` в `config.json` — только для локальной разработки.

`<NAME>` формируется функцией `_env_var_name`:
`username.upper()`, символы кроме `[A-Z0-9_]` → `_`. Коллизии
env-имён логируются WARNING в stdout.

Парольная политика (длина ≥ `PASSWORD_MIN_LENGTH`, чёрный список,
не совпадает с логином, не чисто цифровой) применяется к паролям
в открытом виде. Хеши, заданные через `_PASSWORD_HASH` + `_SALT`,
проверке не подвергаются (формат валидируется через regex
`^[0-9a-f]{64}$`).

`config.json` исключён из репозитория через `.gitignore`.

### Безопасность редиректов и cookie

Без изменений.

### Управление состояниями UI-элементов

`UIStateManager` используется для кнопок сканирования.
`FilterCoordinator` управляет полями автодополнения.
`ThemeManager` управляет выбором темы оформления.
`ViewModeManager` управляет режимом просмотра документа
(рендер ↔ текст).

### Просмотр документа (рендер и подсветка)

Просмотр документа на вкладке реализован как **растровый рендер
страницы PDF** с наложением слоя подсветки совпадений. Это
соответствует назначению DDS как инструмента поиска.

#### Два независимых эндпоинта

- **`GET /api/documents/{doc_id}/pages/{page_number}/render?dpi=300`** —
  PNG-рендер страницы.
  - Заголовки ответа: `ETag: "<file_hash>-<page>-<dpi>"`,
    `Cache-Control: private, max-age=86400`.
  - DPI ограничен диапазоном `[72, 300]`.
  - При превышении `MAX_RENDER_PIXELS` DPI автоматически
    понижается пропорционально.

- **`POST /api/documents/{doc_id}/pages/{page_number}/highlights`** —
  поиск прямоугольников совпадений.
  - Тело: `{"terms": [...], "apply_transform": null}`.
  - Ответ: `{"highlights": [...], "applied_transform": false,
    "transform_confidence": "none"}`.

- **`POST /api/documents/pages/batch`** — пакетная загрузка текста
  страницы для нескольких документов.
  - Тело: `{"doc_ids": [...], "page_number": 0}` (до 20 doc_ids).
  - Ответ: `{"pages": {doc_id: text}, "errors": {doc_id: message}}`.
  - Отсутствующие документы и страницы вне диапазона попадают
    в `errors`.
  - Используется UI при пустом поисковом запросе: 16 HTTP-запросов
    заменяются одним.

#### Публичный API `PageRenderer`

`PageRenderer` (создаётся через `createPageRenderer(docId)`) предоставляет
публичные методы:

- `renderPage(page, options, seq)` — первичный рендер страницы.
- `refreshHighlights(page, terms)` — пересчёт подсветки. Инкрементирует
  `_seq` через `window.DDSApp.incrementLoadSeq` и делегирует в
  приватный `_fetchAndApplyHighlights`. Единственная публичная точка
  пересчёта подсветки извне (используется `app.js` при переключении
  чекбокса «Ротация координат»).
- `setZoom(value)` / `resetZoom()` — управление zoom.
- `releaseBlob()` — освобождение blob URL без запуска нового рендера.
- `cleanup()` — освобождение ресурсов при закрытии вкладки.
- `getZoom()` / `getTransformForPage(page)` / `setTransformForPage(page, value)` —
  доступ к состоянию для внешних модулей.

#### Публичный API `ViewModeManager`

- `getDefault()` / `getMode(docId)` / `setMode(docId, mode, opts)` —
  управление режимом.
- `installSwitcher(panelElement, docId)` — создание переключателя.
- `applyTextFallbackState(docId)` — применение fallback-состояния UI:
  активна кнопка «Текст», кнопка «Рендер» заблокирована. Вызывается
  из `PageRenderer._fallbackToText`.

#### Серверный поиск совпадений

Подсветка реализована через **нормализованный индекс слов страницы**
(`WordIndex`), а не через `page.search_for()`. Это решает проблему,
когда сниппет FTS5 денормализован и не совпадает с оригинальным
текстом PDF.

Поток:

1. При первом запросе к странице сервер строит `WordIndex`:
   - `page.get_text("words")` извлекает все слова с координатами;
   - для каждого слова формируется `WordEntry` с оригинальной и
     **нормализованной** формами;
   - строятся два представления: `by_normalized` (для одиночных
     слов) и `by_line` (для фраз, по ключу `(block_no, line_no)`).
2. `WordIndex` кэшируется в `WordIndexCache` по ключу
   `(doc_id, page_number, file_hash)`. Автоинвалидация при
   переиндексации — через `file_hash`.
3. `HighlightsService.search_highlights(index, terms, apply_transform)`
   выполняет поиск терминов в нормализованном пространстве.

#### Диагностика и коррекция системы координат подсветки

Проблема: в некоторых PDF-документах система координат отличается
от стандартной top-left. Модуль
`dds_core/application/coordinate_diagnostics.py` предоставляет
функции `diagnose_word_index` и `transform_bbox`.

**Диагностика.** Два независимых признака: OOB (доля слов за
границами страницы) и Dominance (доля «вертикальных» слов).
Комбинирование: оба → `"high"`; один → `"medium"`; ни один →
`"none"`.

**Коррекция.** `transform_bbox` преобразует координаты из bottom-left
в top-left. Порядок координат сохраняется; guard-ы предотвращают
«схлопывание» прямоугольника.

**Управление.** `search_highlights` принимает `apply_transform`:
`None` — авто-диагностика; `True` — принудительно; `False` —
не применять.

**Событие аудита.** При уверенности `"high"` или `"medium"` эндпоинт
`/highlights` публикует `CoordinateSystemAnomalyDetected` уровня
WARNING.

**Клиентский интерфейс.** Чекбокс «Ротация координат»:
изначально `disabled`; после первого ответа `/highlights`
активируется и устанавливается по `applied_transform` из ответа;
при переключении пользователем вызывается публичный
`renderer.refreshHighlights(page, terms)`.

#### Нормализованные координаты

Координаты подсветки нормализуются делением на `WordIndex.page_width`
и `WordIndex.page_height`.

#### Источник терминов для подсветки

Термины извлекаются на клиенте из сниппетов FTS5, обрамлённых
маркерами `[[DDS_HIGHLIGHT_START]]` и `[[DDS_HIGHLIGHT_END]]`.
Клиент (`search.js`) формирует карту `termsByPage` (номер страницы →
список терминов) и передаёт её через `window.DDSApp.openDocumentTab`.

#### Визуализация подсветки

Прямоугольники подсветки отображаются как элементы
`.page-render-highlight` внутри `.page-render-overlay` поверх
PNG-изображения страницы. Цвет — **бирюзовый**.

#### Fit-width при открытии страницы

При первом рендере страницы zoom автоматически устанавливается
так, чтобы изображение полностью помещалось **по ширине**
видимой области контейнера (fit-width). Значение ограничено
диапазоном `[0.25, 1.0]`.

Размеры `<img>` задаются явными inline-стилями `style.width` и
`style.height` в пикселях (не через `transform: scale`).

#### Масштабирование

- **`Ctrl + колесо`** — изменение zoom в диапазоне `[0.25, 4.0]`.
- **`Ctrl + 0`** — сброс zoom: fit-width или `1.0`.

#### Панорамирование правой кнопкой мыши

Перетаскивание правой кнопкой мыши по рендеру перемещает страницу.
Обработчики `mousedown`/`mousemove`/`mouseup` и `contextmenu`
устанавливаются в `document.js`.

#### Fallback на текст

При получении от `/render` ответа с кодом 500 или 504 клиент
переключает вкладку в текстовый режим через
`ViewModeManager.applyTextFallbackState(docId)`.

#### Переключатель режима

Каждая вкладка документа имеет переключатель «Рендер / Текст»
(`.page-view-switch`).

#### Навигация по страницам

Навигация по страницам реализована через пагинацию
(`.pagination` внутри `.doc-nav`) и управляется чекбоксом
**«Навигация по всем страницам»**.

#### Управление ресурсами

Каждая вкладка владеет собственным `PageRenderer`
(фабрика `createPageRenderer(docId)`), который хранит blob URL
текущего рендера и карту per-page флагов трансформации координат
(`_transformByPage`). При смене страницы, переходе в текстовый
режим или закрытии вкладки blob URL освобождается через
`URL.revokeObjectURL`.

#### Взаимодействие presentation layer с сервером

Три независимых эндпоинта: `GET /render`, `POST /highlights`,
`POST /api/documents/pages/batch`.

Схема взаимодействия при открытии вкладки:

```
search.js:
  правый клик → termsByPage из сниппетов
  window.DDSApp.openDocumentTab(docId, fileName, page, {termsByPage})

app.js::TabManager.openDocumentTab:
  создание panel и PageRenderer
  DocumentLoader.loadDocument(..., options, renderer)

document.js::DocumentLoader.loadDocument:
  fetch метаданных → page_count
  if viewMode === "render":
    renderer.renderPage(page, options, seq)
  else:
    renderer.releaseBlob()
    DocumentLoader._loadPageText(docId, page, container, seq)
  DocumentRenderer.renderPageNavigation(...)

renderer.renderPage:
  _updateTransformCheckboxState(docId, page, options)
  fetch /render → PNG (в браузерном кэше по ETag)
  создание <img>, применение fit-width по load
  if options.termsByPage[page]:
    fetch /highlights с apply_transform (или без него)
    → bbox-ы, applied_transform, transform_confidence
    → _syncTransformCheckbox(docId, page, seq, applied, confidence)
    → overlay

app.js::transformCheckbox.change:
  renderer.setTransformForPage(page, checked)
  if terms: renderer.refreshHighlights(page, terms)
    → incrementLoadSeq → _fetchAndApplyHighlights

search.js::_loadFirstPagesBatch (при пустом запросе):
  POST /api/documents/pages/batch с doc_ids
  → pages{id: text}, errors{id: message}
```

### API Endpoints

| Метод | Путь | Описание |
|---|---|---|
| GET | /api/search | Полнотекстовый поиск с серверной группировкой. Параметры: `q`, `object_code`, `discipline_code`, `document_type_code`, `unmatched_only`, `limit` (документы), `offset` (по документам). |
| GET | /api/documents/{id} | Метаданные документа |
| GET | /api/documents/{id}/pages/{page} | Текст страницы |
| POST | /api/documents/pages/batch | **Пакетная загрузка текста страницы для нескольких документов** |
| GET | /api/documents/{id}/pages/{page}/render | PNG-рендер страницы (с ETag и Cache-Control) |
| POST | /api/documents/{id}/pages/{page}/highlights | Прямоугольники подсветки совпадений |
| GET | /api/documents/{id}/download | Скачивание исходного PDF |
| DELETE | /api/documents/{id} | Удаление документа (админ) |
| GET | /api/scan/status | Статус сканирования |
| POST | /api/scan/start | Запуск сканирования |
| POST | /api/scan/cancel | Отмена сканирования |
| GET | /api/scan/progress | SSE прогресс (включая вторичное сканирование) |
| GET | /api/index/status | Состояние индексации |
| GET | /api/filters/metadata | Получение справочников с разделением языков |
| POST | /api/scan/refresh-metadata | Повторное обновление метаданных документов (админ) |
| GET | /api/modules | Список модулей |
| GET | /api/modules/{name} | Статус модуля |
| GET/POST | /api/settings | Настройки (админ) |
| GET | /api/diagnostics | Диагностика (админ); включает `word_index_cache.{size, max_size, hits, misses}` |
| GET | /api/themes | Список доступных тем оформления |
| POST | /api/auth/change-password | Смена пароля |

### Страницы

| Путь | Описание |
|---|---|
| / | Главная страница (поиск) |
| /login | Вход |

### Прогресс сканирования через SSE

Без изменений.

### Условная публикация HTTP-событий

Без изменений.

### Группировка результатов поиска в UI

Таблица результатов содержит одну строку на документ:

| Столбец | Описание |
|---|---|
| Документ | Имя файла с расширением (ссылка для скачивания) |
| Страницы | Количество страниц документа (N шт.) |
| Фрагмент | Сниппет страницы с наименьшим номером |

При клике на кнопку раскрытия под родительской строкой появляются
дочерние строки с номерами страниц и сниппетами.

Для пустого запроса (режим «показать все») в столбце «Страницы»
отображается прочерк «—»; первые страницы документов загружаются
одним batch-запросом.

## Событийная модель

Иерархия событий пополнена событиями `ScanSecondaryStarted`,
`ScanSecondaryProgressUpdated`, `ScanSecondaryCompleted`,
`CoordinateSystemAnomalyDetected`, `AutoLoginPerformed`.

Событие `AutoLoginPerformed` (`auth.auto_login`) публикуется
middleware `auto_login_middleware` при успешном создании сессии
по IP-адресу. Уровень INFO.

Событие `CoordinateSystemAnomalyDetected` (`coord.anomaly_detected`)
публикуется на уровне WARNING при обнаружении аномальной системы
координат страницы.

## Зависимости

### Обязательные

Без изменений.

### Для тестов

Без изменений.

### Опциональные (для модулей)

Без изменений.

## Константы

- `DEFAULT_REFERENCES_JSON_PATH` — путь к эталонному JSON справочников.
- `_MIN_SQLITE_VERSION = (3, 9, 0)` — минимальная версия SQLite,
  при которой доступен FTS5.
- `MAX_RENDER_PIXELS = 20_000_000` — максимальное количество
  пикселей в PNG-рендере страницы.
- `WORD_INDEX_CACHE_SIZE = 500` — максимальное количество страниц
  в LRU-кэше индексов слов.
- `PAGE_TEXT_CACHE_SIZE = 500` — максимальное количество страниц
  в LRU-кэше текста страниц (`SearchEngine.get_page_text`).
  Ключ: `(doc_id, page_number, file_hash)`. Автоинвалидация при
  переиндексации через `file_hash`.
- `OPERATION_TIMEOUTS["render.page"] = 30.0` — таймаут рендера
  страницы.
- `OPERATION_TIMEOUTS["render.highlights"] = 30.0` — таймаут
  построения индекса и поиска подсветки.
- `OPERATION_TIMEOUTS["scan.cancel_grace"] = 5.0` — таймаут
  ожидания завершения сканирования при shutdown. Используется
  в `dds_web/lifespan.py::SHUTDOWN_SCAN_TIMEOUT_SECONDS`.
- `COORD_DIAGNOSTICS_ENABLED = True` — глобальный переключатель
  авто-диагностики системы координат.
- `COORD_TEXT_OOB_THRESHOLD = 0.05` — порог доли слов за границами
  страницы.
- `COORD_TEXT_DOMINANCE_THRESHOLD = 0.7` — порог доли «вертикальных»
  слов.
- `COORD_ASPECT_T = 2.0` — порог отношения сторон.

## Сводка по файлу

| Раздел | Содержание |
|---|---|
| Обзор | Добавлены пункты о batch-write метаданных, batch endpoint, LRU-кэше текста, graceful shutdown, env-паролях |
| Архитектурные принципы | Добавлен компромисс `extract_worker`, `refreshHighlights`/`applyTextFallbackState`, отмена через `asyncio.Event`, `_QueueSentinel` |
| Структура проекта | Добавлены `async_utils.py`, `auto_login.py`, `test_auth_env_password.py`, `test_document_metadata_batch.py`, `test_scan_pipeline_cancellation.py`, `theme-coffee.css` |
| Жизненный цикл | Startup: env-пароли; Shutdown: `wait=True, cancel_futures=True` |
| Таймауты | Добавлено примечание о непрерываемости потоков; `SHUTDOWN_SCAN_TIMEOUT_SECONDS` через `OPERATION_TIMEOUTS["scan.cancel_grace"]` |
| Схема БД | Убран `idx_documents_file_hash` (дубликат UNIQUE) |
| Поэтапное сканирование | Фаза 1: отмена через `asyncio.Event`; Фаза 2: batch-write |
| Поисковый бэкенд | Добавлен research-шаг 3.1 (LIMIT не применим) |
| Веб-интерфейс → Аутентификация | Управление паролями через env; `config.json` исключён |
| Веб-интерфейс → Просмотр документа | Публичные API `refreshHighlights`/`applyTextFallbackState`; эндпоинт `/api/documents/pages/batch` |
| API Endpoints | Добавлен `POST /api/documents/pages/batch` |
| Событийная модель | Добавлено `AutoLoginPerformed` |
| Константы | Добавлены `PAGE_TEXT_CACHE_SIZE`, `scan.cancel_grace` |
```

---
