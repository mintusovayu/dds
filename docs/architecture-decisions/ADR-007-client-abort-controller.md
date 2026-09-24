# ADR-007: Client cleanup

| Поле | Значение |
|---|---|
| Дата | 2025-01-15 |
| Автор | mintusovayu |
| Статус | accepted |
| Supersedes | — |
| Superseded-by | — |
| Связанные ADR | ADR-004 (ProcessTaskRunner), ADR-006 (Server-side Terms) |
| Фаза внедрения | 7 |

---

## Контекст

Фаза 7 (Client cleanup) устраняет четыре независимых клиентских
долга, накопившихся в ходе рефакторингов v5–v9. Долги затрагивают
только JavaScript и HTML presentation layer; Python-код (FastAPI,
application, infrastructure, subprocess_tasks) не меняется.

### Долг A. Защита от race condition через `_seq` вместо `AbortController`

До Фазы 7 защита от гонок при быстрой смене страниц/режимов
обеспечивалась счётчиком `AppState.openDocuments[i]._seq`:

```javascript
// document.js::DocumentLoader.loadDocument
var seq = window.DDSApp.incrementLoadSeq(docId);

fetch("/api/documents/" + docId)
    .then(function (r) {
        if (seq !== window.DDSApp.getLoadSeq(docId)) return null;
        return r.json();
    })
    .then(function (doc) {
        if (seq !== window.DDSApp.getLoadSeq(docId)) return;
        // ... обработка
    });
```

Схема работала корректно, но имела три недостатка:

1. **Проверка дублировалась в каждом `.then`/`.catch`.** В
   `document.js` — 12+ мест с `seq !== getLoadSeq(docId)`. При
   добавлении нового `fetch` нужно не забыть проверку.

2. **Устаревший fetch продолжает выполняться.** `abort()` не
   вызывается — сеть и CPU расходуются на запрос, результат
   которого будет отброшен. На медленных соединениях это заметно.

3. **Нет единой точки отмены.** Смена страницы, смена режима,
   закрытие вкладки — три разных сценария, каждый из которых
   вынужден вручную инкрементировать `_seq`.

`AbortController` — стандартный Web API (Chrome 66+, Firefox 57+,
Safari 12.1+, Edge 16+), даёт единый механизм отмены: один
`abort()` прерывает все pending `fetch`, связанные с контроллером.

### Долг B. `innerHTML`/`createElement` для строк таблицы результатов

`SearchRenderer.renderTable` строил строки таблицы через
`document.createElement("tr")` + `tdDoc.innerHTML = "..."`. Проблемы:

- `escapeHtml` вызывался вручную для каждой текстовой вставки —
  легко забыть в новом поле (риск XSS).
- HTML-строки парсились заново при каждом `innerHTML =`.
- Разметка строки разбросана по JS, а не декларативна.

`<template>` — стандарт HTML5 (Chrome 26+, Firefox 22+, Safari
8+). Декларативная разметка в HTML, клонирование через
`content.cloneNode(true)`, автоматическое экранирование через
`textContent`.

### Долг C. Монолитный `SearchRenderer.renderResults`

`renderResults` (~40 строк) смешивал шесть ответственностей:
сохранение состояния, обновление статус-бара, визуализацию
фильтров, управление контейнерами, рендеринг таблицы, пагинация.
Нарушение SRP.

### Долг D. `theme-stylesheet` без cache-busting

Фаза 6 (ADR-006) ввела cache-busting для статических ресурсов
(`base.css`, все `js`-скрипты) через `?v={{ build_hash }}`.
Исключение — `<link id="theme-stylesheet">`: его `href`
динамически меняется `theme_manager.js::applyTheme` при смене
темы, и добавление `?v=` требовало бы механизма, сохраняющего
параметр при смене темы.

Без cache-busting браузер может использовать старую версию CSS
темы из кеша — визуально тема не переключается.

### Контекст среды

DDS работает в single-worker режиме uvicorn (см.
`docs/deployment.md`). Python 3.14. Linux. Все 4 долга —
исключительно в presentation layer (JS/HTML). Python-тесты
(365 на момент начала Фазы 7) не покрывают JS-код; единственная
защита от регрессии — smoke Playwright.

---

## Альтернативы

### Долг A: `_seq` → `AbortController`

#### Альтернатива A1: Оставить `_seq`

**Плюсы.** Никаких изменений.

**Минусы.** Все три недостатка `_seq` сохраняются. Устаревшие
fetch продолжают выполняться. Проверки дублируются. Новые
`fetch` требуют ручного добавления проверок.

**Итог.** Отклонена. Долг реален, устранение запланировано.

#### Альтернатива A2: `AbortController` параллельно с `_seq` (шаг 4a), затем удаление `_seq` (шаг 4b)

**Описание.** Двухшаговый переход. Сначала `AbortController`
добавляется **параллельно** с `_seq`: оба механизма работают
одновременно. После подтверждения стабильности smoke-тестами —
`_seq` полностью удаляется.

**Плюсы.**
- **Снижение риска регресса.** Если `AbortController` не покроет
  какой-то сценарий, `_seq` страхует.
- **Поэтапная верификация.** На каждом шаге можно прогнать
  smoke и убедиться, что поведение не изменилось.
- **Стандартный паттерн.** Аналогичен подходу в ADR-004 (сначала
  добавить `ProcessTaskRunner` рядом с `ProcessPoolExecutor`,
  затем удалить старый).
- **Прозрачность для code review.** Промежуточные коммиты легко
  проверяются (4a — только добавления; 4b — только удаления).

**Минусы.**
- Два коммита вместо одного.
- Временное дублирование логики (14 дней между шагами в
  реальном проекте).

**Итог.** Принята.

#### Альтернатива A3: Заменить `_seq` на `AbortController` одним шагом

**Плюсы.** Один коммит.

**Минусы.**
- Большой diff (add + remove) смешивает две логики в одном
  review.
- При регрессе сложно понять, что именно сломалось — добавление
  или удаление.
- Нет страховки на время между коммитами.

**Итог.** Отклонена. Риск регресса выше, чем удобство.

---

### Долг B: `innerHTML` → `<template>`

#### Альтернатива B1: Оставить `createElement`

**Плюсы.** Ноль изменений.

**Минусы.** Хрупкость `escapeHtml` сохраняется. Смешение разметки
и логики сохраняется. Кэш парсера браузера не используется.

**Итог.** Отклонена. Долг реален.

#### Альтернатива B2: `<template>` с обёрткой `<table><tbody>` в `tmpl-search-doc-row`

**Описание.** Разметка строки документа и дочерней строки
страницы выносится в HTML5-`<template>`. Для `<tr>` содержимое
обёрнуто в `<table><tbody>` — иначе HTML5-парсер в контексте
`<div>` игнорирует `<tr>` (in body insertion mode, §13.2.6.4.7).

**Плюсы.**
- **Декларативная разметка.** Строка таблицы видна в HTML, а не
  собирается в JS.
- **Автоматическое экранирование.** Заполнение через
  `textContent` безопасно без ручного `escapeHtml`.
- **Кэш парсера.** `<template>` парсится один раз при загрузке
  страницы.
- **Устранение хрупкости.** `escapeHtml` остаётся только в
  `escapeAndHighlight` для сниппетов (там он внутри).

**Минусы.**
- **Обёртка `<table><tbody>` в шаблоне.** Требуется обходное
  решение для HTML5-ограничения. При клонировании извлекается
  только `<tr>` через `querySelector("tr.search-doc-row")`.
- **Условная логика остаётся в JS.** `<template>` не поддерживает
  условия; для `hasMultiplePages` — JS-ветки (`.remove()` или
  `style.display = ""`).
- **Два шаблона.** `tmpl-search-doc-row` и `tmpl-search-page-row`
  — две новые сущности в HTML.

**Итог.** Принята.

#### Альтернатива B3: Вынести HTML-строки в константы

**Плюсы.** Минимальные правки.

**Минусы.** Не решает корень — `escapeHtml` остаётся вручную,
разметка остаётся в JS. Кэш парсера не используется.

**Итог.** Отклонена.

#### Альтернатива B4: `<template>` без обёртки

**Описание.** Использовать `<template>` без `<table><tbody>`.

**Плюсы.** Чище разметка шаблона.

**Минусы.** **Не работает.** HTML5-парсер игнорирует `<tr>` в
контексте `<div>` (in body insertion mode). `template.content`
оказывается пустым. Клонирование даёт пустой fragment. Этот
вариант был реализован первым (Шаг 8 Фазы 7) и вызвал падение
3 smoke-тестов.

**Итог.** Отклонена эмпирически (см. раздел «Известные
ограничения»).

---

### Долг C: монолитный `renderResults`

#### Альтернатива C1: Оставить монолит

**Минусы.** SRP нарушен. Читаемость страдает.

**Итог.** Отклонена.

#### Альтернатива C2: Разбить на 4 хелпера + оркестратор

**Описание.** Выделить `renderSearchStatus`, `renderResultsTable`,
`renderPaginationControls`, `renderEmptyState`. Оркестратор
`renderResults` вызывает хелперы в фиксированном порядке.

**Плюсы.**
- SRP: каждая функция делает одну вещь.
- Оркестратор читается как «оглавление» (~15 строк).
- Легче тестировать (хотя JS-тестов в проекте нет).
- Порядок DOM-операций сохранён идентично монолиту.

**Минусы.**
- 4 новых module-private функции.
- `renderResultsTable` — тривиальная обёртка над
  `SearchRenderer.renderTable`.

**Итог.** Принята.

#### Альтернатива C3: Инлайн-комментарии без разбиения

**Минусы.** Не решает SRP. Комментарии не заменяют функции.

**Итог.** Отклонена.

---

### Долг D: cache-busting тем

#### Альтернатива D1: Оставить без `?v=`

**Минусы.** Браузер может использовать старую версию CSS темы
из кеша. Тема визуально не переключается.

**Итог.** Отклонена.

#### Альтернатива D2: `window.DDSApp.buildHash` + `?v=` в `applyTheme`

**Описание.** `build_hash` передаётся из `main.html` через
`data-build-hash` на `#main-init-data`. `app.js::AppInit` читает
его и сохраняет в `window.DDSApp.buildHash`. `theme_manager.js::
applyTheme` добавляет `?v=<buildHash>` к `href` темы.

**Плюсы.**
- Единый источник `build_hash` для всех cache-busting целей.
- `window.DDSApp` — уже существующий фасад; не нужна новая
  глобальная переменная.
- Fallback без `?v=` при отсутствии `window.DDSApp` (ранняя
  инициализация, тесты).

**Минусы.**
- Inline-скрипт в `base.html` (ранняя установка темы) **не
  добавляет** `?v=` — на этом этапе `window.DDSApp` ещё не
  создан. Это осознанное ограничение: первичная загрузка темы
  кэшируется отдельно.
- Новый атрибут `data-build-hash` на `#main-init-data`.

**Итог.** Принята.

#### Альтернатива D3: Inline-скрипт в `base.html`

**Описание.** Добавить `<script>window.__DDS_BUILD_HASH__ = "..."</script>`
в `base.html`. `applyTheme` читает оттуда.

**Плюсы.** Не требует `window.DDSApp`.

**Минусы.**
- Новая глобальная переменная `__DDS_BUILD_HASH__`.
- Дублирование: `build_hash` уже доступен через
  `templates.env.globals` (ADR-006).
- Смешение двух источников: `DDSApp.buildHash` для JS, `env.globals`
  для шаблонов.

**Итог.** Отклонена.

#### Альтернатива D4: `?v=` в inline-скрипте

**Описание.** Inline-скрипт в `base.html` устанавливает `href`
темы с `?v={{ build_hash }}`.

**Плюсы.** Cache-busting работает и при первой загрузке.

**Минусы.** Inline-скрипт не может читать `build_hash` без
`Jinja2`-подстановки в атрибут скрипта. Это уже сделано в
`main.html` для `<script src>`, но для inline-скрипта пришлось
бы добавить `data-build-hash` в `base.html`, что противоречит
цели «единый источник». Кроме того, `applyTheme` при смене темы
всё равно должен добавлять `?v=` из JS.

**Итог.** Отклонена. Осложняет без существенного выигрыша.

---

## Решение

Применяется четыре подзадачи:

1. **AbortController** (двухшаговый переход, шаги 4a и 4b).
2. **`<template>` с обёрткой `<table><tbody>`** для
   `tmpl-search-doc-row`; без обёртки для `tmpl-search-page-row`.
3. **Разбиение `renderResults`** на 4 module-private хелпера
   и оркестратор.
4. **Cache-busting тем** через `window.DDSApp.buildHash`.

### Ключевые детали реализации

#### AbortController (двухшаговый переход)

**Шаг 4a — параллельно с `_seq`.**

- `app.js::TabManager.openDocumentTab` добавляет
  `_abortController: new AbortController()` в запись вкладки.
- `window.DDSApp.getAbortController(docId)` и
  `window.DDSApp.resetAbortController(docId)` — публичные методы.
- `closeDocumentTab` вызывает `rec._abortController.abort()`
  перед `renderer.cleanup()`.
- `document.js::loadDocument` вызывает `resetAbortController`,
  передаёт `signal` во все `fetch`. Все `.then`/`.catch`
  проверяют `signal.aborted` **в дополнение** к `_seq`-проверкам.
- `AbortError` в `.catch` — тихий return (не ошибка).

**Шаг 4b — удаление `_seq`.**

- Удалены `window.DDSApp.incrementLoadSeq` и `getLoadSeq`.
- Удалено поле `_seq` из записи вкладки.
- Удалены все проверки `seq !== window.DDSApp.getLoadSeq(docId)`
  в `document.js`.
- Удалены параметры `seq` из методов
  `renderPage`, `_fetchAndApplyHighlights`, `_fallbackToText`,
  `_loadPageText`, `_syncTransformCheckbox`.
- Единственный guard — `signal.aborted`.

**Особый случай `refreshHighlights`.**

Публичный метод `PageRenderer.refreshHighlights` (пересчёт
подсветки при переключении чекбокса «Ротация координат»)
**не передаёт `signal`** в `_fetchAndApplyHighlights`.

Обоснование: пересчёт подсветки не должен прерывать активный
PNG-рендер. Если бы `refreshHighlights` вызывал
`resetAbortController`, текущий рендер страницы отменился бы,
что визуально нежелательно (пользователь увидел бы скелетон
вместо уже загруженного PNG).

Защита от применения устаревшего ответа к overlay сменившейся
страницы — проверка `rec.page !== pageNumber` в
`_fetchAndApplyHighlights` перед `_applyHighlights`. Эта проверка
выполняется **всегда**, независимо от наличия `signal`.

#### `<template>` для строк таблицы

**`tmpl-search-doc-row`** — с обёрткой `<table><tbody>`:

```html
<template id="tmpl-search-doc-row">
    <table>
        <tbody>
            <tr class="search-doc-row" data-expanded="false"
                aria-expanded="false">
                <!-- ... ячейки ... -->
            </tr>
        </tbody>
    </table>
</template>
```

При клонировании из fragment'а извлекается **только** `<tr>`:

```javascript
var wrapper = docRowTmpl.content.cloneNode(true);
var tr = wrapper.querySelector("tr.search-doc-row");
// ... заполнение tr
tbody.appendChild(tr);
```

**`tmpl-search-page-row`** — без обёртки (`<div>` разрешён
в «in body» mode):

```html
<template id="tmpl-search-page-row">
    <div class="search-page-row">
        <span class="search-page-number"></span>
        <div class="snippet"></div>
    </div>
</template>
```

#### Разбиение `renderResults`

Четыре module-private хелпера **до** `var SearchRenderer = {...}`:

```javascript
function renderSearchStatus(data, actualQuery, appliedFilters) { ... }
function renderResultsTable(results, isQueryEmpty) { ... }
function renderPaginationControls(total, currentPage) { ... }
function renderEmptyState() { ... }
```

Оркестратор `SearchRenderer.renderResults` сохраняет **идентичный**
порядок DOM-операций:

1. `SearchState.lastResults = data.results || []`.
2. `renderSearchStatus(...)`.
3. `container.classList.remove("hidden")`.
4. Если `total === 0` — `renderEmptyState()` + early return.
5. `noResults.add("hidden")`, `wrapper.remove("hidden")`.
6. `renderResultsTable(...)`.
7. `renderPaginationControls(...)`.

#### Cache-busting тем

- `main.html`: `#main-init-data` получает атрибут
  `data-build-hash="{{ build_hash }}"`.
- `app.js::AppInit`: `_buildHash = initData.dataset.buildHash || ""`;
  `window.DDSApp.buildHash = _buildHash`.
- `theme_manager.js::applyTheme`: `var suffix = hash ? ("?v=" + hash) : "";`
  `link.href = "/static/css/theme-" + safeName + ".css" + suffix;`.
- Fallback без `?v=` при отсутствии `window.DDSApp`.

### Инварианты

- **`AbortController` per-tab.** Каждая вкладка имеет собственный
  контроллер, хранящийся в `AppState.openDocuments[i]._abortController`.
- **`resetAbortController` вызывается только в `loadDocument`.**
  `refreshHighlights` использует `getAbortController` **без**
  сброса.
- **`AbortError` — не ошибка.** Не показывается пользователю,
  не логируется как `console.error`. Допустимо
  `console.warn`.
- **`rec.page !== pageNumber` в `_fetchAndApplyHighlights`** —
  единственная защита от применения устаревшего ответа подсветки
  для случая `refreshHighlights` (без `signal`).
- **`<template>` для `<tr>` требует обёртки `<table><tbody>`.**
  Это HTML5-ограничение, не наше решение.
- **Порядок DOM-операций в `renderResults` сохранён.**
  Наблюдаемое поведение не изменилось.
- **Inline-скрипт в `base.html` не добавляет `?v=`.**
  Cache-busting тем вступает в силу после `AppInit`.
- **`escapeAndHighlight` сохранён.** Он конвертирует маркеры
  подсветки в `<b>`/`</b>` для визуального выделения совпадений
  в сниппете. Это не извлечение терминов.

---

## Последствия

### Положительные

- **Устаревшие fetch прерываются.** Смена страницы/режима
  вызывает `abort()` — pending fetch отменяются мгновенно.
  Экономия сети и CPU.
- **Единый механизм отмены.** Один `signal` вместо 12+ проверок
  `seq !== getLoadSeq(docId)`. Новые `fetch` автоматически
  защищены.
- **Декларативная разметка строк таблицы.** `<template>` в HTML,
  а не строки в JS. `textContent` автоматически экранирует —
  нельзя забыть `escapeHtml`.
- **SRP в `renderResults`.** Оркестратор + 4 хелпера вместо
  монолита.
- **Cache-busting тем работает после `AppInit`.** Смена темы
  подтягивает свежую CSS после пересборки.
- **Расширение smoke-тестов.** Два новых теста
  (`test_theme_switch_adds_cache_busting`,
  `test_rapid_page_switch_no_errors`) защищают новые механизмы.
- **Удалён мёртвый код.** `_seq`, `incrementLoadSeq`,
  `getLoadSeq` — удалены полностью.

### Отрицательные

- **Два коммита при двухшаговом переходе** (шаг 4a + шаг 4b).
  В промежуточном состоянии оба механизма работают параллельно.
- **`XMLHttpRequest` в `downloadDocument` не отменяется.**
  `signal` работает только для `fetch`. Скачивание файла
  продолжается даже после закрытия вкладки. Приемлемо:
  скачивание — короткая операция.
- **Обёртка `<table><tbody>` в шаблоне.** Читателю нужно
  понимать HTML5-ограничение. Комментарий в HTML и JSDoc
  поясняют причину.
- **`refreshHighlights` без `signal`.** Единственная защита —
  `rec.page !== pageNumber`. Это менее надёжно, чем `abort()`,
  но оправдано: пересчёт подсветки не должен прерывать
  PNG-рендер.
- **Inline-скрипт в `base.html` не имеет cache-busting.**
  Первичная загрузка темы кэшируется отдельно. Cache-busting
  вступает в силу только при смене темы пользователем.

### Нейтральные

- `main.html`: добавлен атрибут `data-build-hash` на
  `#main-init-data` и два `<template>`.
- `app.js`: добавлено поле `_abortController` в запись вкладки;
  добавлены `getAbortController`, `resetAbortController`;
  добавлен `window.DDSApp.buildHash`; удалены `incrementLoadSeq`,
  `getLoadSeq`, поле `_seq`.
- `document.js`: все `fetch` получают `signal`; удалены все
  `_seq`-проверки и параметры `seq`.
- `search.js`: `renderResults` разбит; `renderTable` использует
  `<template>`.
- `theme_manager.js`: `applyTheme` добавляет `?v=<buildHash>`.
- `test_smoke_playwright.py`: 15 → 17 тестов.

### Известные ограничения

- **HTML5-ограничение `<template>` для `<tr>`.** Парсер в контексте
  `<div>` игнорирует `<tr>`. Обёртка `<table><tbody>` — обходное
  решение. Если HTML5-спецификация изменится (маловероятно),
  обёртку можно будет убрать. Пока — обязательна.

- **`XMLHttpRequest` не поддерживает `signal`.** Скачивание
  файла не отменяется. Обходной путь — заменить на `fetch` с
  `response.blob()` + `createObjectURL`, но это меняет
  существующий паттерн (XHR даёт встроенный `onprogress` с
  `lengthComputable`, `fetch` — не даёт). Отложено.

- **`refreshHighlights` без `signal`.** Защита от race —
  `rec.page !== pageNumber`. Менее надёжно, чем `abort()`, но
  оправдано (пересчёт подсветки не должен прерывать рендер).
  Альтернатива — отдельный `AbortController` для подсветки —
  усложнила бы модель.

- **Cache-busting тем не работает при первой загрузке.**
  Inline-скрипт в `base.html` не имеет доступа к `build_hash`
  без изменения структуры `base.html`. Осознанное решение.

- **Inline-скрипт в `base.html` может кэшироваться браузером
  при обновлении.** Это отдельная проблема, не связанная с
  Фазой 7. При необходимости решается в будущих фазах.

- **Смоук-тест `test_rapid_page_switch_no_errors` использует
  переключение режимов, а не страниц.** `seeded_document` —
  одна страница; пагинация недоступна. Переключение режимов
  даёт тот же эффект race condition (смена fetch, отмена
  pending). Расширение сценария — при появлении многостраничного
  smoke-документа.

- **Расширенный `test_document_highlight_visible`.** Явная
  проверка `highlight_count > 0` — регресс-защита. Если
  сервер вернёт пустой `highlights`, тест упадёт. Это ожидаемое
  поведение.

---

## Ссылки

- **Реализация:**
  - `dds_web/templates/main.html` — атрибут `data-build-hash`,
    шаблоны `tmpl-search-doc-row` (с обёрткой `<table><tbody>`)
    и `tmpl-search-page-row`.
  - `dds_web/static/js/app.js` — `_abortController` в записи
    вкладки, методы `getAbortController` / `resetAbortController`,
    `window.DDSApp.buildHash`, `abort()` в `closeDocumentTab`;
    удалены `_seq`, `incrementLoadSeq`, `getLoadSeq`.
  - `dds_web/static/js/document.js` — `signal` во всех `fetch`,
    `AbortError`-handling, удалены все `_seq`-проверки;
    проверка `rec.page !== pageNumber` в
    `_fetchAndApplyHighlights`.
  - `dds_web/static/js/search.js` — разбиение `renderResults`
    на 4 хелпера, `<template>` в `renderTable`.
  - `dds_web/static/js/theme_manager.js` — `?v=<buildHash>` в
    `applyTheme`.
- **Тесты:**
  - `tests/smoke/test_smoke_playwright.py` —
    `test_theme_switch_adds_cache_busting`,
    `test_rapid_page_switch_no_errors`,
    расширенный `test_document_highlight_visible`.
- **Связанные ADR:**
  - ADR-004 (ProcessTaskRunner) — паттерн двухшагового перехода
    при риске регресса.
  - ADR-006 (Server-side Terms) — паттерн устранения хрупкости
    presentation layer; источник `build_hash` через
    `templates.env.globals`.
- **Внешние материалы:**
  - HTML5 Specification, §13.2.6.4.7 «The "in body" insertion
    mode» — обоснование обёртки `<table><tbody>`:
    <https://html.spec.whatwg.org/multipage/parsing.html#parsing-main-inbody>
  - MDN Web Docs, `AbortController`:
    <https://developer.mozilla.org/en-US/docs/Web/API/AbortController>
  - MDN Web Docs, `<template>`:
    <https://developer.mozilla.org/en-US/docs/Web/HTML/Element/template>
  - WHATWG Fetch Standard, `signal`:
    <https://fetch.spec.whatwg.org/#request-abort-signal>

---
