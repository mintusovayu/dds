# ADR-006: Server-side Terms

| Поле | Значение |
|---|---|
| Дата | 2025-01-15 |
| Автор | mintusovayu |
| Статус | accepted |
| Supersedes | — |
| Superseded-by | — |
| Связанные ADR | ADR-003 (Domain Text Normalization), ADR-005 (DocumentIndexPlan) |
| Фаза внедрения | 6 |

---

## Контекст

Термины подсветки — фрагменты текста, на которых сработал поиск
FTS5. Они используются для двух задач:

1. **Подсветка совпадений на рендере страницы.** Клиент передаёт
   список терминов на эндпоинт ``POST /api/documents/{id}/pages/{n}/highlights``,
   сервер находит координаты bbox и возвращает прямоугольники.
2. **Подсказки при наведении на прямоугольник подсветки** —
   атрибут ``title`` показывает термин пользователю.

До Фазы 6 термины извлекались **на клиенте** в
``dds_web/static/js/search.js``. Модуль парсил сниппеты FTS5,
возвращённые эндпоинтом ``/api/search``, через регулярное выражение
по нейтральным маркерам ``[[DDS_HIGHLIGHT_START]]`` /
``[[DDS_HIGHLIGHT_END]]``:

```javascript
function extractTermsFromSnippet(snippet) {
    var pattern = new RegExp(
        escapeRegExp(SNIPPET_HIGHLIGHT_START) + "(.+?)"
        + escapeRegExp(SNIPPET_HIGHLIGHT_END), "g");
    // ... while (match = pattern.exec(snippet)) ...
}

function buildTermsByPage(result) {
    var map = {};
    for (var i = 0; i < result.pages.length; i++) {
        var page = result.pages[i];
        var terms = extractTermsFromSnippet(page.snippet || "");
        if (terms.length > 0) {
            map[page.page_number] = terms;
        }
    }
    return map;
}
```

Эта схема работала, но накопила четыре архитектурных долга.

### Долг A. Протечка FTS5-специфики в presentation layer

Маркеры ``[[DDS_HIGHLIGHT_START]]`` / ``[[DDS_HIGHLIGHT_END]]`` —
это **специфика FTS5** (аргументы функции ``snippet()``,
см. ``dds_core/infrastructure/fts5_search_backend.py``). Клиент
знал об их формате и был обязан повторять логику извлечения,
которую уже знал сервер.

При замене поискового движка (Elasticsearch, Meilisearch —
упомянуто как направление в ``ISearchBackend``) маркеры изменятся
(например, на HTML-теги ``<em>``). Пришлось бы править и сервер,
и клиент одновременно, рискуя рассинхронизировать формат.

### Долг B. Хрупкая логика извлечения на клиенте

Регулярное выражение с ``escapeRegExp``, ``while (match = pattern.exec(...))``,
``seen``-объект для дедупликации — заметный объём кода (~60 строк),
который не тестировался (в проекте нет JS-тестов). Ошибка в этом
коде привела бы к некорректной подсветке, которую сложно
диагностировать: клиентские ошибки не видны в логе сервера.

### Долг C. Дублирование логики

Сервер **уже знает** о формате маркеров: он их сам и генерирует
в ``FTS5SearchBackend.search`` (аргументы ``snippet()``).
Клиент повторял знание, добавляя точку рассинхронизации.

### Долг D. ``build_hash`` через контекст каждого ``TemplateResponse``

Отдельно — при добавлении cache-busting для статических ресурсов
возникла задача передавать ``build_hash`` во все шаблоны. Наивное
решение — передавать его в ``context={...}`` каждого вызова
``TemplateResponse`` (в ``pages.py`` таких вызовов 2, но при
развитии интерфейса станет больше). Это дублирование и
напоминание о забытом месте при добавлении нового эндпоинта.

### Контекст среды

DDS работает в single-worker режиме uvicorn. FTS5 —
единственный поисковый бэкенд на момент Фазы 6, но архитектура
``ISearchBackend`` предполагает замену. Python 3.14.

---

## Альтернативы

### Альтернатива 1: Оставить клиентскую реализацию как есть

**Описание.** Не переносить извлечение терминов на сервер. Оставить
``extractTermsFromSnippet`` / ``buildTermsByPage`` в ``search.js``.

**Плюсы.**
- Никаких изменений в коде.
- Ноль рисков регресса.
- Существующий smoke-тест ``test_document_highlight_visible``
  продолжает работать.

**Минусы.**
- Долги A, B, C сохраняются: протечка FTS5-специфики в клиент,
  хрупкая логика, дублирование.
- При смене поискового бэкенда — правка двух слоёв одновременно.
- Нет тестов на извлечение терминов (JS-тестов в проекте нет).
- Противоречит принципу «каждая функциональность реализована
  в одном месте» (см. ``docs/architecture.md``, «Единственная
  точка реализации»).

**Итог.** Отклонена. Долги системны и запланированы к устранению
в плане рефакторинга v9.

### Альтернатива 2: Расширить `PageHit.terms` (принята)

**Описание.** Добавить поле ``terms: tuple[str, ...]`` в domain-модель
``PageHit``. Сервер извлекает термины при формировании ``PageHit``
в ``FTS5SearchBackend.search`` через новую функцию
``extract_terms_from_snippet`` (application layer). Клиент получает
готовый список через ``PageHitResponse.terms`` и не парсит сниппеты.

**Плюсы.**
- **Долг A устранён.** Клиент не знает о маркерах FTS5.
- **Долг B устранён.** Извлечение терминов — чистая Python-функция,
  покрыта unit-тестами (``test_snippet_terms_extractor.py``,
  23 теста) и интеграционными (``test_fts5_search_backend.py``,
  11 тестов).
- **Долг C устранён.** Единая точка извлечения — сервер.
- Замена поискового бэкенда: клиент не меняется, формат терминов
  остаётся тем же. Достаточно обновить реализацию ``ISearchBackend``.
- Термины передаются в том же HTTP-ответе, что и результаты
  поиска — никаких дополнительных round-trip.
- Тип ``tuple[str, ...]`` (не ``list``): иммутабельность, hashability,
  pickle-совместимость.

**Минусы.**
- Термины передаются для **всех** страниц документа, даже если
  пользователь откроет только одну. Оверхед — несколько байт
  на страницу в JSON (~50–150 байт × N).
- ``PageHit`` становится тяжелее: domain-модель обрастает
  дополнительным полем.
- ``FTS5SearchBackend`` выполняет ``denormalize_text`` и
  ``extract_terms_from_snippet`` для каждой страницы — но это
  линейные операции на коротких строках (~200 символов).
- Breaking-change формально нет (поле с default ``()``), но
  OpenAPI-схема расширяется.

**Итог.** Принята.

### Альтернатива 3: Отдельный эндпоинт `POST /api/documents/pages/terms`

**Описание.** Не расширять ``PageHit``. Ввести новый эндпоинт
``POST /api/documents/pages/terms`` с телом
``{doc_ids: [...], page_numbers: [...]}``, возвращающий
``{doc_id: {page_number: [terms]}}``. Клиент вызывает его при
правом клике / открытии вкладки.

**Плюсы.**
- Термины запрашиваются по требованию — экономия трафика на
  редких сценариях.
- ``PageHit`` не меняется.
- Согласовано с существующим паттерном ``POST /api/documents/pages/batch``.

**Минусы.**
- **Дополнительный HTTP-запрос** для каждой вкладки документа.
  При 10 документах на страницу результатов — 10 запросов
  при последовательном открытии.
- **Дублирование логики формирования сниппетов.** Чтобы извлечь
  термины из сниппета, сервер должен повторно вызвать ``snippet()``
  FTS5 или хранить сниппеты. Первое — дорого (повторный MATCH),
  второе — усложнение БД.
- **Сложность клиента растёт:** вместо чтения поля из уже
  полученного ответа нужно делать отдельный fetch.
- Не решает Долг D (``build_hash``).
- Раздувание API: ещё один эндпоинт с пересекающейся семантикой.

**Итог.** Отклонена. Усложнение без выигрыша.

### Альтернатива 4: Экстрактор в domain

**Описание.** Поместить ``extract_terms_from_snippet`` в
``dds_core/domain/text_normalization.py`` или рядом с ним,
рядом с ``normalize_text``. Обоснование: маркеры — часть
доменного контракта нормализации/сниппетов.

**Плюсы.**
- Domain-слой содержит все правила обработки текста
  (нормализация + извлечение терминов).
- Согласовано с ADR-003 (перенос ``normalize_text`` в domain).

**Минусы.**
- **Маркеры — это FTS5-специфика, а не доменное правило.**
  ``normalize_text`` — правило о поиске в DDS (семантика,
  независимая от бэкенда). А ``extract_terms_from_snippet``
  зависит от конкретного формата аргументов ``snippet()`` FTS5.
- При смене поискового бэкенда (Elasticsearch с ``<em>``)
  доменное правило пришлось бы менять — нарушение принципа
  «domain не знает о конкретных реализациях».
- Поле ``PageHit.terms`` определено в domain, но **заполняется**
  в infrastructure — это соответствует паттерну:
  domain описывает структуру данных, infrastructure — способ
  её получить из конкретного источника.
- В ADR-003 перенос в domain был оправдан тем, что правило
  **семантическое** (кириллица ↔ латиница эквивалентны).
  Здесь правило **структурное** (вырезать фрагменты между
  маркерами) — application-уровневая задача.

**Итог.** Отклонена. Место в application layer.

### Альтернатива 5: Возвращать «сырые» маркеры, оставить парсинг клиенту

**Описание.** Не вводить ``PageHit.terms``. Оставить клиентский
парсинг, но стандартизировать формат маркеров через
env-конфигурацию, чтобы избежать рассинхронизации.

**Плюсы.**
- Минимальные изменения.
- Формально устраняет «дублирование знания о формате» — оно
  становится общим (передаётся через env).

**Минусы.**
- **Не устраняет корень проблемы.** Клиент всё равно парсит
  строки регуляркой.
- **Передача конфигурации на клиент** — это ещё один канал
  рассинхронизации (env на сервере vs. runtime-значение на клиенте).
- Не покрывается тестами — JS-логика остаётся.
- Сложно отлаживать: клиентские ошибки не видны на сервере.

**Итог.** Отклонена. Формальный workaround.

### Альтернатива 6: Отдельный эндпоинт + кэширование на клиенте

**Описание.** Комбинация 2 и 3: ``PageHit.terms`` **не** расширяем,
но добавляем эндпоинт ``/api/documents/pages/terms`` и кэшируем
результаты на клиенте (например, в ``Map``).

**Плюсы.**
- ``PageHit`` не меняется.
- Повторные открытия вкладок не делают повторных запросов.

**Минусы.**
- Всё, что минусы альтернативы 3, плюс:
- Кэш на клиенте — источник багов (инвалидация, race conditions).
- Клиент снова знает о терминах как о «внешнем ресурсе» — вместо
  того, чтобы получить их «бесплатно» вместе с результатами.

**Итог.** Отклонена. Сложнее без выигрыша.

---

## Решение

Применяется **Альтернатива 2**: расширение ``PageHit.terms``
в domain + новый модуль ``snippet_terms_extractor`` в application.

Дополнительно в том же ADR фиксируется решение по **``build_hash``
через ``templates.env.globals``** (Долг D) — как сопутствующее
улучшение cache-busting статических ресурсов.

### Ключевые детали реализации

**Domain-модель** (``dds_core/domain/models.py``):

```python
@dataclass
class PageHit:
    page_number: int
    snippet: str
    terms: tuple[str, ...] = ()
```

``terms`` — ``tuple[str, ...]``, не ``list``: иммутабельность,
hashability, pickle-совместимость. Значение по умолчанию ``()``
сохраняет обратную совместимость с существующими конструкторами
(например, ``PageHit(page_number=0, snippet="")`` в
``FTS5SearchBackend._search_documents_only``).

**Application-модуль**
(``dds_core/application/snippet_terms_extractor.py``):

```python
def extract_terms_from_snippet(snippet: str) -> tuple[str, ...]:
    if not snippet:
        return ()
    # Линейный обход через str.find по маркерам из config.
    # Дедупликация через dict.fromkeys — сохраняет порядок
    # первого появления.
    ...
```

Особенности:
- Без регулярок (``str.find``): линейная сложность,
  отсутствие escape-логики, отсутствие backtracking-рисков.
- Маркеры берутся из ``dds_core.domain.config``
  (``SNIPPET_HIGHLIGHT_START`` / ``SNIPPET_HIGHLIGHT_END``).
- Непарные ``START`` / ``END`` игнорируются.
- Пустые фрагменты между маркерами пропускаются.

**Infrastructure** (``dds_core/infrastructure/fts5_search_backend.py``):

```python
denormalized_snippet = denormalize_text(snippet_raw)
page_terms = extract_terms_from_snippet(denormalized_snippet)
pages_by_doc[doc_id].append(
    PageHit(
        page_number=page_number,
        snippet=denormalized_snippet,
        terms=page_terms,
    )
)
```

Порядок важен: денормализация **до** извлечения терминов —
термины должны совпадать с формой, отображаемой пользователю
в подсказках (атрибут ``title``).

**Web** (``dds_web/api.py``):

```python
class PageHitResponse(BaseModel):
    page_number: int
    snippet: str
    terms: list[str] = Field(default_factory=list)
```

Pydantic сериализует ``list[str]`` в JSON-массив. При конструировании
из domain — явное ``list(p.terms)`` для очевидного соответствия
схеме.

**Client** (``dds_web/static/js/search.js``):

Удалены ``extractTermsFromSnippet``, ``buildTermsByPage``,
``escapeRegExp``, module-level ``SNIPPET_HIGHLIGHT_START`` /
``SNIPPET_HIGHLIGHT_END``. В contextmenu-обработчике ``termsByPage``
формируется из ``page.terms``:

```javascript
var termsByPage = {};
if (currentResult && currentResult.pages) {
    for (var j = 0; j < currentResult.pages.length; j++) {
        var page = currentResult.pages[j];
        if (page.terms && page.terms.length > 0) {
            termsByPage[page.page_number] = page.terms;
        }
    }
}
```

Функция ``escapeAndHighlight`` **остаётся**: она использует локальные
``START_MARKER`` / ``END_MARKER`` для конвертации маркеров
в ``<b>``/``</b>`` при визуальном выделении совпадений в сниппете.
Это не извлечение терминов, а рендеринг сниппета.

**``build_hash`` через ``env.globals``** (``dds_web/pages.py``):

```python
def _compute_build_hash() -> str:
    """env DDS_BUILD_HASH → git HEAD (--short=8) → "dev"."""
    ...

templates.env.globals["build_hash"] = _compute_build_hash()
```

Значение доступно во всех шаблонах как ``{{ build_hash }}`` без
передачи в ``context={...}``. Приоритет источников: env-переменная
→ git → ``"dev"`` (fallback для не-git окружений).

В шаблонах: ``?v={{ build_hash }}`` для ``base.css`` и всех
``<script src="/static/js/*.js">``. Исключение —
``<link id="theme-stylesheet">``: динамический ``href``,
меняется ``theme_manager.js``; добавление ``?v=`` привело бы
к потере параметра при смене темы.

### Инварианты

- **``PageHit.terms`` — ``tuple``, не ``list``.** Согласовано
  с domain-моделью; проверяется тестом
  ``test_search_result_pages_have_tuple_terms``.
- **Порядок терминов — порядок первого появления.** Дедупликация
  через ``dict.fromkeys``.
- **Термины в денормализованной форме.** Согласованы с ``snippet``
  (тот же ``denormalize_text``).
- **Пустой запрос → ``terms == ()``.** Виртуальная страница
  не имеет сниппета; ``extract_terms_from_snippet("")`` возвращает
  ``()``.
- **Идемпотентность.** Повторный вызов ``extract_terms_from_snippet``
  на той же строке даёт тот же результат.
- **Отсутствие зависимости от FTS5 в application.** Модуль
  ``snippet_terms_extractor`` не импортирует ни SQLite, ни FTS5.
- **``build_hash`` доступен во всех шаблонах.** Регистрация один
  раз при импорте ``pages.py``.

---

## Последствия

### Положительные

- Термины подсветки формируются в **одном месте** (сервер),
  клиент не парсит сниппеты.
- Извлечение покрыто тестами: 23 unit-теста
  (``test_snippet_terms_extractor.py``) + 11 интеграционных
  (``test_fts5_search_backend.py``). Клиентская логика не
  тестировалась вообще.
- Замена поискового бэкенда (Elasticsearch, Meilisearch)
  не затрагивает клиент: формат ответа стабилен.
- **Удалено ~68 строк JS-кода** (три функции, две константы,
  раздел docstring). Дублирование логики устранено.
- ``build_hash`` регистрируется один раз в ``env.globals``:
  при добавлении новых эндпоинтов не нужно передавать его
  в ``context`` каждого ``TemplateResponse``.
- Cache-busting статических ассетов автоматически работает
  при изменении git-хеша сборки.

### Отрицательные

- **Оверхед передачи терминов.** Для 16 документов × ~2 страницы
  в среднем = 32 набора терминов × ~5 терминов × ~30 байт
  ≈ 5 КБ на страницу результатов. На практике незаметно
  на фоне сниппетов (те же 16 × 200 символов ≈ 3 КБ).
- **``FTS5SearchBackend`` выполняет дополнительную работу**
  на каждой странице: ``extract_terms_from_snippet``. Оверхед
  пренебрежим (O(len(snippet)) = O(200) × 32 страницы
  = ~6400 ``str.find``), но формально есть.
- **Domain-модель ``PageHit`` тяжелеет.** Одно поле — минимальная
  цена за устранение четырёх долгов.

### Нейтральные

- ``PageHitResponse.terms`` появляется в OpenAPI-схеме
  ``/api/search``. Клиенты, генерирующие код из OpenAPI,
  получат корректный тип ``array of string``.
- В ``dds_web/pages.py`` появляется функция ``_compute_build_hash``
  и регистрация ``env.globals``.
- В ``base.html`` и ``main.html`` — ``?v={{ build_hash }}``
  для CSS/JS.
- Новых записей в ``exceptions.yaml`` не требуется: импорт
  ``application → application`` (``fts5_search_backend →
  snippet_terms_extractor``) разрешён контрактом
  ``infrastructure-isolation`` (infrastructure → application
  уже легален через ``MetadataFilterQueryBuilder``).

### Известные ограничения

- **FTS5 ``snippet()`` при multi-term OR-запросе на многостраничном
  документе может вернуть сниппет не той страницы.** Это ограничение
  FTS5, выявленное при разработке Фазы 6. Оно **не относится
  к ADR-006** (не изменение поведения сервера в части ``terms``;
  сниппеты и раньше могли быть «сдвинуты»). В production поиск
  обычно идёт по однозначным запросам (пользователь ищет одно
  слово или фразу). В тестах
  (``test_search_terms_per_page_isolated``) этот сценарий
  обойдён через два однозначных запроса. Полное решение
  потребовало бы переработки ``FTS5SearchBackend`` — вне
  области Фазы 6.
- **``normalize_search_query`` не экранирует токены
  со спецсимволами FTS5.** Токены с ``-``, ``+``, ``*``
  интерпретируются FTS5 как операторы; ``EC-423-1`` парсится
  как ``"ec"`` + ``423`` + ``"1"`` с ошибкой
  ``no such column: 423``. Это предсуществующая особенность
  ``normalize_search_query`` (Фаза 3), не относящаяся к Фазе 6.
  Возможный фикс — оборачивать токены со спецсимволами
  в двойные кавычки — рекомендован для Фазы 8.
- **``terms`` передаются для всех страниц документа.** Даже если
  пользователь откроет только одну. Это осознанный выбор:
  разделение «термины для страницы, которую откроют» потребовало
  бы отдельного эндпоинта (см. альтернативу 3) с дополнительным
  round-trip.
- **``theme-stylesheet`` без ``?v=``.** Динамический ``href``
  не может безопасно нести query-параметр (JS перезаписывает
  href при смене темы без суффикса). Cache-busting тем —
  задача Фазы 7 (client cleanup).
- **``build_hash`` вызывает ``subprocess.run`` на импорте
  ``pages.py``.** С таймаутом 2 секунды и подавлением всех
  ошибок. Если git недоступен — fallback ``"dev"``. Накладные
  расходы — разовые, при загрузке модуля.

---

## Ссылки

- **Реализация:**
  - ``dds_core/application/snippet_terms_extractor.py`` —
    ``extract_terms_from_snippet``.
  - ``dds_core/domain/models.py`` — поле ``PageHit.terms``.
  - ``dds_core/infrastructure/fts5_search_backend.py`` —
    заполнение ``terms`` при формировании ``PageHit``.
  - ``dds_web/api.py`` — ``PageHitResponse.terms``.
  - ``dds_web/pages.py`` — ``_compute_build_hash``,
    регистрация в ``templates.env.globals``.
  - ``dds_web/templates/base.html``, ``main.html`` —
    ``?v={{ build_hash }}``.
  - ``dds_web/static/js/search.js`` — удаление
    ``extractTermsFromSnippet``, ``buildTermsByPage``,
    ``escapeRegExp``; чтение ``page.terms`` в
    contextmenu-обработчике.
- **Тесты:**
  - ``tests/test_snippet_terms_extractor.py`` — 23 unit-теста.
  - ``tests/test_fts5_search_backend.py`` — 11 интеграционных
    тестов (включая проверку ``tuple``-типа и дедупликации).
  - ``tests/smoke/test_smoke_playwright.py::test_document_highlight_visible`` —
    E2E-регресс-защита.
- **Связанные ADR:**
  - ADR-003 (Domain Text Normalization) — обоснование разделения:
    нормализация (семантика) — domain; извлечение терминов
    (структура) — application.
  - ADR-005 (DocumentIndexPlan) — устранение SQL-специфики из
    application. ADR-006 продолжает ту же линию:
    FTS5-специфика не протекает в presentation layer.
- **Внешние материалы:**
  - Документация SQLite FTS5, ``snippet()``:
    <https://www.sqlite.org/fts5.html#the_snippet_function>
  - Документация Jinja2, ``env.globals``:
    <https://jinja.palletsprojects.com/en/3.1.x/api/#jinja2.Environment.globals>
  - Martin Fowler, «Presentation Model» (2004) — принцип
    «presentation layer не знает о деталях бэкенда».

---
