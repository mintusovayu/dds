```markdown
# ADR-003: Domain Text Normalization

| Поле | Значение |
|---|---|
| Дата | 2025-01-15 |
| Автор | mintusovayu |
| Статус | accepted |
| Supersedes | — |
| Superseded-by | — |
| Связанные ADR | ADR-001 (Temporary stderr Lock), ADR-002 (Remove Service Locator) |
| Фаза внедрения | 3 |

---

## Контекст

Фаза 3 плана v9 (Domain cleanup) устраняет три независимых
архитектурных долга, накопившихся при рефакторингах v5–v8. Долги
связаны общей идеей: domain-слой должен содержать доменные правила
и структуры данных, а не делегировать их в application или
инфраструктуру.

### Долг A. Нормализация текста живёт в application, но нужна в infrastructure

Функции `normalize_text` / `denormalize_text` и словари
`CYRILLIC_TO_LATIN_MAP` / `LATIN_TO_CYRILLIC_MAP` находились в
`dds_core/application/text_normalizer.py`. Функция
`normalize_text` использовалась из infrastructure:

- `dds_core/infrastructure/pymupdf_text_extractor.py` —
  построение `WordIndex` при извлечении слов;
- `dds_core/infrastructure/fts5_search_backend.py` — нормализация
  сниппетов.

Это **нарушение направления зависимостей** (infrastructure →
application). В docstring модуля `pymupdf_text_extractor.py` оно
было зафиксировано как «осознанный компромисс», обоснованный
единством правил нормализации. Компромисс работал, но нарушал
принцип Clean Architecture и `import-linter` (исключение не
добавлялось, но код был «серым» по слоистости).

Правило нормализации — **доменное**, а не прикладное:

- оно определяет **семантику** поиска в DDS: какие символы
  считаются эквивалентными при сопоставлении запроса и текста
  документа;
- оно должно быть **единым контрактом** для трёх слоёв
  (infrastructure — индексация, application — сниппеты,
  presentation — отображение);
- изменение правила (например, добавление новых соответствий
  между кириллицей и латиницей) — изменение доменной логики.

### Долг B. Хрупкий инвариант параллельных списков в `build_word_index`

Модель `WordEntry` (`dds_core/domain/models.py`) не содержала
поля `block_no`. При построении `by_line` в
`PyMuPDFTextDocument.build_word_index` использовался **параллельный
список** `block_line_pairs`, синхронный по индексам с `entries`:

```python
entries: list[WordEntry] = []
block_line_pairs: list[tuple[int, int]] = []
for item in raw_words:
    ...
    entries.append(WordEntry(...))
    block_line_pairs.append((int(block_no), int(line_no)))

by_line: dict[tuple[int, int], list[WordEntry]] = {}
for entry, (block_no, line_no) in zip(entries, block_line_pairs, strict=False):
    ...
```

Обоснование в docstring: `WordEntry` не имеет `block_no`, потому
что при упрощении алгоритма поиска фраз (только внутри строки)
это поле сочли ненужным. Однако `by_line` требует ключ
`(block_no, line_no)` — PyMuPDF нумерует строки отдельно в каждом
блоке, начиная с 0.

Инвариант «индекс i в `entries` и `block_line_pairs` совпадают»
хрупок: любая фильтрация в цикле (например, добавление условия
пропуска) ломает соответствие. Docstring модуля содержит явное
предупреждение для будущих правок.

### Долг C. Смешение application и infrastructure в `normalize_search_query`

`dds_core/application/search_query_normalizer.py` выполнял **две**
функции одновременно:

1. **Токенизация** запроса с учётом синтаксиса FTS5 — структурный
   разбор на слова, фразы, операторы, скобки.
2. **Сборка MATCH-выражения** FTS5 — применение префикса колонки
   `normalized_text:` к словам и фразам, нормализация текста,
   форматирование кавычек.

Вторая задача — **инфраструктурная специфика** FTS5. Если
поисковый бэкенд сменится на Elasticsearch (упомянуто в
`ISearchBackend`), `normalize_search_query` придётся переписать
вместе с application-слоем. Это нарушает инверсию зависимостей:
application не должен знать о конкретном поисковом движке.

---

## Альтернативы

### Альтернатива 1: Оставить как есть

**Описание.** Не менять ни один из трёх долгов. Продолжать жить
с компромиссом (A), хрупким инвариантом (B) и смешением
слоёв (C).

**Плюсы.**
- Ноль изменений.
- Ноль рисков регресса.

**Минусы.**
- Компромисс A противоречит Clean Architecture и «серый» с точки
  зрения слоистости.
- Инвариант B уязвим: следующая же правка цикла (например,
  пропуск повреждённых слов) сломает соответствие.
- Смешение C блокирует замену поискового бэкенда без переписывания
  application-кода.
- Все три долга явно обозначены в docstrings как технический
  долг — сохранение их противоречит решению о рефакторинге v9.

**Итог.** Отклонена. Долги реальны и запланированы к устранению.

### Альтернатива 2: Полный перенос нормализации в domain + `WordEntry.block_no` + разделение токенизации/сборки (принята)

**Описание.** Три подзадачи:

- **A.** Перенести `normalize_text` / `denormalize_text` /
  словари в новый модуль `dds_core/domain/text_normalization.py`.
  Удалить `dds_core/application/text_normalizer.py`. Обновить
  импорты в infrastructure и application.
- **B.** Добавить `block_no: int` в `WordEntry`. В
  `build_word_index` использовать `entry.block_no` напрямую,
  удалить `block_line_pairs` и `zip(...)`.
- **C.** Разделить `search_query_normalizer` на два модуля:
  `dds_core/application/query_tokenizer.py` (структурный разбор
  в discriminated union токенов) и
  `dds_core/infrastructure/fts5/match_builder.py` (сборка
  MATCH-выражения FTS5 из токенов).

**Плюсы.**
- Правильное направление зависимостей: infrastructure → domain
  (вместо infrastructure → application).
- Domain содержит доменные правила (нормализация) и доменные
  структуры данных (`WordEntry.block_no`).
- Application-слой не знает о FTS5: discriminated union токенов —
  нейтральное представление, пригодное для любого поискового
  движка.
- Устранён хрупкий инвариант: `WordEntry` содержит все поля,
  необходимые для построения `by_line`.
- Тесты для новых модулей изолированы и не зависят от FTS5.

**Минусы.**
- Изменение импортов в ~5 файлах.
- Удаление двух модулей (`text_normalizer.py`,
  `search_query_normalizer.py`).
- Изменение публичной структуры domain — новые символы в
  `dds_core.domain.text_normalization`.
- Возможные расхождения: разработчики могли привыкнуть к старым
  путям импорта; незначительный период адаптации.

**Итог.** Принята.

### Альтернатива 3: Оставить `normalize_text` в application, разрешить импорт через `exceptions.yaml`

**Описание.** Не переносить `normalize_text` в domain, а
формально разрешить импорт infrastructure → application через
`import-linter` исключение (аналогично временному исключению для
`extract_worker`).

**Плюсы.**
- Минимальные изменения: одна запись в `exceptions.yaml`.
- Не требуется обновлять импорты.

**Минусы.**
- Увеличивает технический долг: `exceptions.yaml` — это список
  временных или перманентных исключений, каждое из которых
  требует rationale и фазы устранения. Добавление ещё одного
  исключения без плана удаления = рост долга.
- Компромисс уже был признан проблемой (docstring
  `pymupdf_text_extractor.py` явно указывает, что функция
  «чистая», что «дублирование привело бы к риску
  рассинхронизации» — но это не повод оставлять её в application).
- Правило нормализации — доменное по природе; его нахождение в
  application искажает семантику слоёв.

**Итог.** Отклонена. Формализация компромисса вместо его
устранения — плохой выбор.

### Альтернатива 4: Разделить `normalize_search_query` без переноса `normalize_text`

**Описание.** Выполнить только подзадачу C (разделить токенизатор
и сборщик), не трогая подзадачи A и B.

**Плюсы.**
- Меньше изменений.
- Подзадача C полезна сама по себе.

**Минусы.**
- Половинчатая мера: долг A (компромисс infrastructure →
  application) остаётся.
- Долг B (хрупкий инвариант) остаётся.
- Фаза 3 не достигает цели «Domain cleanup».

**Итог.** Отклонена. Разделение только C не решает исходную
задачу.

### Альтернатива 5: Поместить всю логику FTS5 в application

**Описание.** Оставить `normalize_search_query` в application, но
убрать из неё FTS5-специфику: сборка MATCH-выражения выполняется
в application как универсальная функция.

**Плюсы.**
- Один модуль вместо двух.

**Минусы.**
- Нарушает слоистость: префикс `normalized_text:`, синтаксис
  кавычек, порядок операторов — это FTS5-специфика. Её
  нахождение в application делает невозможной замену бэкенда
  без правки application.
- Противоречит принципу «application не знает о конкретном
  поисковом движке» (сформулирован в docstring
  `ISearchBackend`).

**Итог.** Отклонена. Слоистость важнее краткости.

### Альтернатива 6: Поместить всю логику токенизации в infrastructure

**Описание.** Перенести и токенизатор, и сборщик в
infrastructure/fts5, оставив application чистым от синтаксиса
поискового запроса.

**Плюсы.**
- Application не знает о синтаксисе запроса.

**Минусы.**
- Синтаксис поискового запроса (операторы AND/OR/NOT, кавычки,
  скобки) — часть **пользовательской семантики** поиска, а не
  FTS5-специфика. Другие поисковые движки тоже используют
  подобный синтаксис (Elasticsearch — `AND`/`OR`/`NOT`,
  кавычки для фраз).
- Разбор запроса — application-задача: он преобразует
  пользовательский ввод в структурированное представление,
  не зависящее от движка.
- Перенос в infrastructure заблокирует будущее
  переиспользование токенизатора (например, для подсветки
  терминов в клиенте).

**Итог.** Отклонена. Токенизация — application-уровневая задача.

---

## Решение

Применяется **Альтернатива 2**: три подзадачи A, B, C.

### A. Перенос нормализации в domain

- Создан модуль `dds_core/domain/text_normalization.py`.
  Содержит:
  - `CYRILLIC_TO_LATIN_MAP: dict[str, str]`;
  - `LATIN_TO_CYRILLIC_MAP: dict[str, str]`;
  - `normalize_text(text: str) -> str`;
  - `denormalize_text(text: str) -> str`.
  Код перенесён **без изменений** из
  `dds_core/application/text_normalizer.py`.
- Module docstring обновлён: подчёркивается, что нормализация —
  доменное правило, объясняется причина переноса.
- Удалён `dds_core/application/text_normalizer.py`.
- Обновлены импорты в:
  - `dds_core/infrastructure/pymupdf_text_extractor.py`;
  - `dds_core/application/highlights_service.py`;
  - `dds_core/infrastructure/fts5_search_backend.py`.
- В `pymupdf_text_extractor.py` удалён раздел модульного docstring
  «Архитектурный компромисс (infrastructure → application)».

### B. `WordEntry.block_no` и упрощение `build_word_index`

- В `dds_core/domain/models.py` в `WordEntry` добавлено поле
  `block_no: int`. Расположено между `line_no` и `word_no`.
- В `dds_core/infrastructure/pymupdf_text_extractor.py`:
  - из `build_word_index` удалён параллельный список
    `block_line_pairs`;
  - ключ `by_line` строится через
    `key = (entry.block_no, entry.line_no)`;
  - `zip(entries, block_line_pairs, strict=False)` заменён
    на `for entry in entries: ...`;
  - из docstring метода удалён раздел «Инвариант синхронности
    списков» и предупреждение для будущих правок.
- Обновлён docstring `WordEntry` в `models.py`: описано поле
  `block_no` и его роль в построении `by_line`.

### C. Discriminated union токенов FTS5

- Создан `dds_core/application/query_tokenizer.py`:
  - `WordToken(text: str)`, `PhraseToken(text: str)`,
    `OperatorToken(op: str)`, `ParenToken(char: str)` —
    `@dataclass(frozen=True)`;
  - алиас `QueryToken = WordToken | PhraseToken | OperatorToken | ParenToken`;
  - `tokenize_search_query(query: str) -> list[QueryToken]` —
    структурный разбор запроса без нормализации и без
    FTS5-специфики.
- Создан подпакет `dds_core/infrastructure/fts5/`:
  - `__init__.py` — маркер подпакета с описанием назначения;
  - `match_builder.py`:
    - `build_match_expression(tokens: list[QueryToken]) -> str` —
      сборка MATCH-выражения;
    - `normalize_search_query(query: str) -> str` — обёртка
      (`tokenize_search_query` + `build_match_expression`),
      сохраняющая привычный API.
- Удалён `dds_core/application/search_query_normalizer.py`.
- В `dds_core/infrastructure/fts5_search_backend.py`:
  - импорт `normalize_search_query` изменён на
    `from .fts5.match_builder import normalize_search_query`;
  - импорт `denormalize_text` изменён на
    `from ..domain.text_normalization import denormalize_text`;
  - обновлён module docstring в части описания нормализации.

### Ключевые детали

- **Правило направления зависимостей.** После Фазы 3:
  - domain не зависит ни от application, ни от infrastructure;
  - application зависит только от domain;
  - infrastructure зависит от application (для хелперов) и
    domain (для моделей и правил).
  Infrastructure → application остаётся легальным для хелперов
  вроде `MetadataFilterQueryBuilder` (зафиксировано в
  `exceptions.yaml` комментарием), но применение конкретных
  правил (например, `normalize_text`) перенесено в domain.
- **Discriminated union как контракт.** Любой поисковый бэкенд
  (FTS5 сейчас, Elasticsearch в будущем) работает с одним и тем
  же набором токенов `QueryToken`. Добавление нового типа токена
  (например, `WildcardToken`) требует явного обновления
  `isinstance`-веток в сборщике — это **осознанное** решение:
  молчаливое пропускание новых токенов привело бы к неполному
  MATCH-выражению.
- **`block_no` в `WordEntry`.** Поле используется только в
  `build_word_index` для группировки по `by_line`. Поиск
  одиночных слов и фраз продолжает работать так же, как и
  раньше. Проверяется тестами `test_highlights_transform_matrix`
  (хелпер `_make_entry` дополнен параметром `block_no: int = 0`).

### Инварианты

- **Единый контракт нормализации.** `normalize_text` —
  единственная точка применения правил (в domain). Ни
  application, ни infrastructure не дублируют правила.
- **Иммутабельность токенов.** `QueryToken` — `frozen=True`
  dataclass. Сборщик MATCH-выражения не может мутировать токены.
- **Идемпотентность `normalize_search_query`.**
  `normalize_search_query(q) == build_match_expression(tokenize_search_query(q))`
  для любого запроса.
- **Непрерывность `word_no` внутри строки.** Проверка смежности
  слов при поиске фраз (`_is_adjacent_word_numbers` в
  `HighlightsService`) сохранена без изменений — `WordEntry`
  по-прежнему содержит `word_no`.

---

## Последствия

### Положительные

- Устранён архитектурный компромисс infrastructure → application
  (долг A). Импорт `normalize_text` из infrastructure теперь
  идёт в domain — правильное направление.
- Устранён хрупкий инвариант параллельных списков (долг B).
  `build_word_index` стал проще и безопаснее: фильтрация в цикле
  не сломает соответствие.
- Устранено смешение application и infrastructure в
  `normalize_search_query` (долг C). Application не знает о FTS5;
  infrastructure содержит только сборщик.
- Единый контракт нормализации: правила изменения
  кириллица↔латиница находятся в одном доменном модуле.
- Расширяемость поиска: discriminated union токенов позволяет
  добавить новый бэкенд без изменения application-кода.
- `dds_modules/` не затронуты: внешние модули работают через
  `IModule` / `ISecondaryProcessor` и не используют внутренние
  хелперы (`normalize_text`, `normalize_search_query`).

### Отрицательные

- Обновление импортов в 5 файлах.
- Удаление двух модулей: разработчики, привыкшие к путям
  `dds_core.application.text_normalizer` и
  `dds_core.application.search_query_normalizer`, должны
  привыкнуть к новым путям.
- Тесты `test_highlights_transform_matrix.py` требуют обновления
  хелпера `_make_entry` — добавление параметра `block_no: int = 0`.

### Нейтральные

- Появились 3 новых модуля (`domain/text_normalization.py`,
  `application/query_tokenizer.py`,
  `infrastructure/fts5/match_builder.py`) и подпакет
  `infrastructure/fts5/`.
- В `DEPRECATIONS.yaml` остались записи Фазы 3 (5 штук) —
  аудит удалённых символов.
- Тестовое покрытие расширено: 3 новых файла
  (`test_text_normalization_domain.py`,
  `test_query_tokenizer.py`, `test_fts5_match_builder.py`).

### Известные ограничения

- **Токенизатор не валидирует синтаксис.** Парность скобок,
  сбалансированность кавычек, корректность операторов проверяет
  FTS5 на этапе `MATCH`. Ошибки синтаксиса приводят к
  `sqlite3.OperationalError`, который `fts5_search_backend`
  оборачивает в `ValueError` (HTTP 400).
- **Discriminated union требует явного обновления при добавлении
  токенов.** Если в токенизатор добавится новый тип (например,
  `WildcardToken`), сборщик должен быть обновлён для его
  обработки. Это осознанный компромисс: вместо «молчаливого
  пропуска» неизвестных токенов сборщик оставит их без
  преобразования — это заметно в тестах.
- **Правила нормализации остаются фиксированными.** Расширение
  таблицы соответствий (например, добавление ``ё → e``)
  возможно, но требует правки domain-модуля и обновления тестов
  `test_text_normalization_domain.py`. Это правильно: изменение
  семантики поиска — доменное решение, должно быть явным.
- **Схема БД не изменена.** Колонка `normalized_text` в
  `text_index_fts` по-прежнему заполняется на этапе индексации
  через `normalize_text`. Перенос модуля в domain не затрагивает
  схему; существующие индексы продолжают работать.

---

## Ссылки

- **Реализация:**
  - `dds_core/domain/text_normalization.py` (новый модуль);
  - `dds_core/domain/models.py` (`WordEntry.block_no`);
  - `dds_core/application/query_tokenizer.py` (новый модуль);
  - `dds_core/infrastructure/fts5/__init__.py` (новый подпакет);
  - `dds_core/infrastructure/fts5/match_builder.py` (новый модуль);
  - `dds_core/infrastructure/pymupdf_text_extractor.py`
    (обновлённые импорты, упрощённый `build_word_index`);
  - `dds_core/infrastructure/fts5_search_backend.py`
    (обновлённые импорты);
  - `dds_core/application/highlights_service.py`
    (обновлённый импорт).
- **Удалённые модули:**
  - `dds_core/application/text_normalizer.py`;
  - `dds_core/application/search_query_normalizer.py`.
- **Тесты:**
  - `tests/test_text_normalization_domain.py` (новый);
  - `tests/test_query_tokenizer.py` (новый);
  - `tests/test_fts5_match_builder.py` (новый);
  - `tests/test_highlights_transform_matrix.py` (обновлён
    хелпер `_make_entry`).
- **Связанные ADR:**
  - ADR-001 (Temporary stderr Lock) — не затрагивается.
  - ADR-002 (Remove Service Locator) — не затрагивается.
  - ADR-005 (DocumentIndexPlan, запланирован) — Phase 5
    продолжит уборку domain-слоя: план индексации заменит
    SQL-строки в application.
- **Внешние материалы:**
  - Martin Fowler, «Inversion of Control Containers and the
    Dependency Injection pattern» (2004) — обоснование того,
    что правила и структуры данных принадлежат слою, наиболее
    близкому к предметной области.
  - Eric Evans, «Domain-Driven Design» (2003) — понятие
    «доменного правила», применимое к нормализации текста в DDS.
```

---
