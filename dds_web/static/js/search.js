/**
 * Логика полнотекстового поиска веб-приложения DDS.
 *
 * Модуль реализует клиентскую сторону поиска: выполнение запросов
 * к REST API, рендеринг сгруппированных результатов в таблицу
 * и пагинацию по документам.
 *
 * Серверная группировка результатов:
 * API ``/api/search`` возвращает по одной записи на документ
 * с вложенным массивом страниц ``pages``. Каждый элемент
 * ``pages`` содержит ``page_number`` (0-based), ``snippet`` и
 * ``terms`` — уникальные термины подсветки, извлечённые сервером
 * (см. ADR-006). Поле ``total`` — количество уникальных документов.
 *
 * Рендеринг свёрнутой строки документа:
 * - Столбец «Документ» — имя файла с расширением (ссылка).
 *   Полный путь — в подсказке (``title``).
 * - Столбец «Страницы» — количество страниц документа, на которых
 *   найдены совпадения, в формате ``N шт.`` (например, ``13 шт.``).
 *   Для документа с одной страницей — ``1 шт.`` (кнопка раскрытия
 *   не создаётся).
 *   При пустом запросе (режим «показать все») документ содержит
 *   одну виртуальную страницу ``page_number=0``, которая не имеет
 *   смысла для пользователя. В этом случае в столбце «Страницы»
 *   отображается прочерк «—».
 * - Столбец «Фрагмент» — сниппет страницы с наименьшим номером
 *   (``pages[0].snippet``). При раскрытии группы сниппет
 *   родительской строки скрывается через ``visibility: hidden``
 *   (сохраняет layout таблицы).
 *
 * Рендеринг раскрытой группы:
 * - Дополнительная строка ``<tr class="search-children-row">``
 *   с ``<td colspan="3">`` содержит контейнер
 *   ``.search-children-container``.
 * - Внутри контейнера — по одной строке ``.search-page-row``
 *   для каждой страницы: номер (``Стр. N``) и сниппет.
 * - Анимация раскрытия/сворачивания — через ``max-height``
 *   и ``opacity``.
 *
 * Пакетная загрузка первых страниц при пустом запросе
 * (скорректированный план, шаг 3.3):
 *
 * Ранее при пустом поисковом запросе (режим «показать все»)
 * каждый документ вызывал ``GET /api/documents/{id}/pages/0``
 * отдельно — до 16 HTTP-запросов на одну страницу результатов.
 * Теперь используется единый эндпоинт
 * ``POST /api/documents/pages/batch`` с телом
 * ``{doc_ids: [...], page_number: 0}``, возвращающий
 * ``{pages: {doc_id: текст}, errors: {doc_id: сообщение}}``.
 * Один HTTP-запрос вместо N.
 *
 * Отображение текста:
 * - Успешно загруженный текст устанавливается через
 *   ``textContent`` (защита от XSS).
 * - Для документов из ``errors`` отображается сообщение об
 *   ошибке (например, «Документ не найден», «Таймаут загрузки»).
 * - При сетевой ошибке — «Ошибка загрузки» для всех документов.
 *
 * Взаимодействие с пользователем:
 * - **Левый клик** по имени файла в столбце «Документ» запускает
 *   скачивание PDF-файла (``downloadDocument``).
 * - **Правый клик** по имени файла открывает вкладку свойств
 *   документа (метаданные + текст страницы с навигацией) через
 *   ``window.DDSApp.openDocumentTab``. Системное контекстное меню
 *   подавляется (``preventDefault``). Вкладка открывается на
 *   странице с наименьшим номером (``pages[0].page_number``),
 *   то есть на странице, чей сниппет показан в столбце
 *   «Фрагмент».
 * - Клик в любом другом месте строки документа (если у документа
 *   более одной страницы) раскрывает или сворачивает группу
 *   страниц. Обработка клика делегирована на ``tbody``.
 * - Раскрытая строка документа помечается классом ``active``
 *   и подсвечивается фоном (см. ``base.css``).
 * - У документа с одной страницей строка не кликабельна для
 *   раскрытия (раскрывать нечего), но клики (левый и правый)
 *   по имени файла работают.
 *
 * Передача терминов для подсветки в вкладку документа
 * (Фаза 6, ADR-006):
 *
 * При правом клике по имени файла модуль формирует карту
 * ``termsByPage`` (номер страницы → список терминов) из поля
 * ``result.pages[i].terms``, подготовленного сервером, и передаёт
 * её в ``window.DDSApp.openDocumentTab`` через параметр ``options``.
 *
 * До Фазы 6 термины извлекались на клиенте из сниппетов FTS5
 * через регулярные выражения по маркерам
 * ``[[DDS_HIGHLIGHT_START]]`` / ``[[DDS_HIGHLIGHT_END]]``.
 * Теперь сервер — единственная точка контроля формата терминов
 * (модуль ``dds_core/application/snippet_terms_extractor.py``),
 * а клиент получает готовый список. Это устраняет протечку
 * FTS5-специфики в presentation layer и делает логику подсветки
 * устойчивой к изменению формата маркеров.
 *
 * Разбиение ``renderResults`` (Фаза 7, ADR-007):
 *
 * Ранее ``SearchRenderer.renderResults`` (~40 строк) смешивал
 * пять независимых ответственностей: сохранение состояния,
 * обновление статус-бара, визуализацию фильтров, управление
 * контейнерами, рендеринг таблицы и пагинации.
 *
 * В Фазе 7 функция разбита на оркестратор и 4 module-private
 * хелпера:
 *
 * +----------------------------------+----------------------------------+
 * | Компонент                        | Ответственность                  |
 * +==================================+==================================+
 * | ``renderSearchStatus``           | Обновление статус-бара и         |
 * |                                  | визуализации фильтров.           |
 * +----------------------------------+----------------------------------+
 * | ``renderResultsTable``           | Делегирование в                  |
 * |                                  | ``SearchRenderer.renderTable``.  |
 * +----------------------------------+----------------------------------+
 * | ``renderPaginationControls``     | Вычисление ``totalPages`` и      |
 * |                                  | делегирование в                  |
 * |                                  | ``SearchRenderer.renderPagination``.|
 * +----------------------------------+----------------------------------+
 * | ``renderEmptyState``             | Показ сообщения «Ничего не       |
 * |                                  | найдено», скрытие таблицы.       |
 * +----------------------------------+----------------------------------+
 * | ``SearchRenderer.renderResults`` | Оркестратор: вызывает хелперы    |
 * |                                  | в фиксированном порядке.         |
 * +----------------------------------+----------------------------------+
 *
 * Оркестратор сохраняет **идентичный** порядок DOM-операций, что
 * и монолитная версия. Наблюдаемое поведение не меняется.
 *
 * Использование ``<template>`` (Фаза 7, ADR-007):
 *
 * ``SearchRenderer.renderTable`` строит строки таблицы через
 * клонирование HTML5-``<template>``-элементов, объявленных в
 * ``main.html``:
 *
 * +----------------------------------+----------------------------------+
 * | Шаблон                           | Назначение                       |
 * +==================================+==================================+
 * | ``tmpl-search-doc-row``          | Строка документа в таблице       |
 * |                                  | результатов.                     |
 * +----------------------------------+----------------------------------+
 * | ``tmpl-search-page-row``         | Дочерняя строка страницы         |
 * |                                  | (внутри раскрытой группы).       |
 * +----------------------------------+----------------------------------+
 *
 * Преимущества перед ``createElement`` + ``innerHTML``:
 * - разметка в HTML, а не в JS;
 * - ``textContent`` вместо ``escapeHtml`` — автоматическое
 *   экранирование для ``fileName``, ``pagesListText``, номеров
 *   страниц;
 * - ``innerHTML`` остаётся только для сниппетов, где
 *   ``escapeAndHighlight`` возвращает безопасную разметку с
 *   ``<b>`` (результат ``escapeHtml`` + замена маркеров).
 *
 * Оборачивание ``<tr>`` в ``<table><tbody>``:
 *
 *   Шаблон ``tmpl-search-doc-row`` содержит ``<tr>``, обёрнутый в
 *   ``<table><tbody>``. Это необходимо: ``<tr>`` — table-элемент,
 *   и HTML5-парсер игнорирует его в контексте ``<div>`` (in body
 *   insertion mode). Обёртка переводит парсер в «in table body»
 *   mode, где ``<tr>`` разрешён. При клонировании из fragment'а
 *   извлекается **только** ``<tr>`` через
 *   ``querySelector("tr.search-doc-row")``; обёртка
 *   ``<table><tbody>`` не попадает в целевой ``<tbody>``.
 *
 * Ограничение ``<template>``: он не поддерживает условную логику.
 * Ветвления (``hasMultiplePages``) выполняются в JS: показать или
 * удалить кнопку раскрытия через ``style.display`` / ``remove()``.
 *
 * Пасхалка («большой белый самолет»):
 * Если в поле поиска введена фраза «большой белый самолет»,
 * а в поле объекта — слово «домой», при отправке формы поиска
 * запускается визуальная анимация пролетающего слева направо
 * самолёта.
 *
 * Порядок документов:
 * Порядок документов задаётся сервером (по ``best_rank``, затем
 * по ``doc_id``) и не должен изменяться на клиенте.
 *
 * Архитектура модуля:
 *
 * +----------------------------------+----------------------------------+
 * | Компонент                        | Ответственность                  |
 * +==================================+==================================+
 * | ``SearchState``                  | Управление состоянием поиска:    |
 * |                                  | текущий запрос, страница,        |
 * |                                  | последние результаты.            |
 * +----------------------------------+----------------------------------+
 * | ``SearchRenderer``               | Рендеринг сгруппированных        |
 * |                                  | результатов, пагинация,          |
 * |                                  | раскрытие групп страниц,         |
 * |                                  | пакетная загрузка первых страниц.|
 * +----------------------------------+----------------------------------+
 * | ``renderSearchStatus``           | Хелпер: статус-бар + фильтры.    |
 * +----------------------------------+----------------------------------+
 * | ``renderResultsTable``           | Хелпер: делегат в renderTable.   |
 * +----------------------------------+----------------------------------+
 * | ``renderPaginationControls``     | Хелпер: вычисление totalPages +  |
 * |                                  | делегат в renderPagination.      |
 * +----------------------------------+----------------------------------+
 * | ``renderEmptyState``             | Хелпер: показ «Ничего не         |
 * |                                  | найдено».                        |
 * +----------------------------------+----------------------------------+
 * | ``SearchAPI``                    | Взаимодействие с REST API        |
 * |                                  | ``/api/search`` через Fetch.     |
 * +----------------------------------+----------------------------------+
 *
 * Принципы:
 * - Модуль не содержит бизнес-логики — только отображение
 *   и координация запросов.
 * - Данные загружаются через REST API.
 * - Обработка ошибок на уровне пользовательского интерфейса.
 * - Рендеринг выполняется через DOM API без внешних библиотек.
 * - Таблица результатов НЕ содержит столбец «Релевантность».
 * - Кнопка раскрытия доступна с клавиатуры (``<button>``),
 *   имеет ``aria-expanded`` и ``aria-controls``.
 * - Термины подсветки приходят с сервера (``pages[i].terms``),
 *   а не извлекаются из сниппетов на клиенте (см. ADR-006).
 * - ``renderResults`` — оркестратор; хелперы не имеют побочных
 *   эффектов вне своей зоны ответственности.
 * - Строки таблицы строятся из ``<template>`` (``main.html``);
 *   ``textContent`` используется для всех текстовых данных.
 *
 * Зависимости:
 * - ``document.js`` — предоставляет ``DocumentUtils``.
 * - ``filter_coordinator.js`` — предоставляет ``FilterCoordinator``.
 * - ``app.js`` — предоставляет глобальный объект ``DDSApp``.
 * - ``main.html`` — DOM-структура с идентификаторами элементов
 *   и ``<template>``-элементы (``tmpl-search-doc-row``,
 *   ``tmpl-search-page-row``).
 * - ``base.css`` — стили ``.snippet``, ``.table``, ``.pagination``,
 *   ``.search-pages-cell``, ``.search-expand-btn``,
 *   ``.search-expand-icon``, ``.search-children-row``,
 *   ``.search-children-container``, ``.search-page-row``,
 *   ``.search-page-number``, ``.download-progress``,
 *   ``.dds-airplane-easter-egg``.
 */
(function () {
    "use strict";

    /* ==============================================================
       1. Константы и утилиты
       ============================================================== */

    /**
     * Максимальное количество символов, сохраняемых в идентификаторе.
     */
    var MAX_SANITIZED_ID_LENGTH = 64;

    /**
     * Фраза-триггер для пасхалки в поле поиска.
     */
    var EASTER_EGG_SEARCH_PHRASE = "большой белый самолет";

    /**
     * Слово-триггер для пасхалки в поле объекта.
     */
    var EASTER_EGG_OBJECT_VALUE = "домой";

    /**
     * Длительность анимации пролёта самолёта в миллисекундах.
     */
    var EASTER_EGG_ANIMATION_MS = 4000;

    /**
     * Приводит строку к безопасному HTML id.
     *
     * @param {string} s — исходная строка (например, ``doc_id``).
     * @returns {string} — безопасный идентификатор.
     */
    function sanitizeId(s) {
        var result = String(s).replace(/[^a-zA-Z0-9_-]/g, "_");
        return result.length > MAX_SANITIZED_ID_LENGTH
            ? result.substring(0, MAX_SANITIZED_ID_LENGTH)
            : result;
    }

    /**
     * Нормализует значение поля: приводит к нижнему регистру,
     * обрезает пробелы по краям, схлопывает множественные пробелы
     * внутри строки в один.
     *
     * @param {string} value — исходное значение поля.
     * @returns {string} — нормализованное значение.
     */
    function normalizeFieldValue(value) {
        if (!value) return "";
        return String(value)
            .toLowerCase()
            .replace(/\s+/g, " ")
            .trim();
    }

    /* ==============================================================
       2. Импорт внешних модулей
       ============================================================== */

    var DocumentUtils = window.DocumentUtils;
    var escapeHtml = DocumentUtils.escapeHtml;
    var FilterCoordinator = window.FilterCoordinator;

    /* ==============================================================
       3. Состояние поиска
       ============================================================== */

    var SearchState = {
        currentQuery: "",
        currentPage: 1,
        resultsPerPage: 16,
        lastResults: []
    };

    /* ==============================================================
       4. Взаимодействие с REST API
       ============================================================== */

    var SearchAPI = {
        /**
         * Выполняет поиск через REST API.
         *
         * @param {string} query — поисковый запрос (может быть пустым).
         * @param {Object} filters — объект с фильтрами.
         * @param {number} limit — максимальное количество документов.
         * @param {number} offset — смещение по документам.
         * @returns {Promise<Object>} Promise с ответом
         *   ``{query, total, results}``.
         */
        search: function (query, filters, limit, offset) {
            var params = new URLSearchParams();
            params.append("q", query);
            params.append("limit", String(limit));
            params.append("offset", String(offset));

            if (filters.object_code) {
                params.append("object_code", filters.object_code);
            }
            if (filters.discipline_code) {
                params.append("discipline_code", filters.discipline_code);
            }
            if (filters.document_type_code) {
                params.append("document_type_code", filters.document_type_code);
            }
            if (filters.unmatched_only) {
                params.append("unmatched_only", "true");
            }

            var url = "/api/search?" + params.toString();
            return fetch(url).then(function (response) {
                if (!response.ok) {
                    return response.json().then(function (data) {
                        throw new Error(
                            data.detail || "Ошибка поиска"
                        );
                    });
                }
                return response.json();
            });
        },

        /**
         * Пакетная загрузка текста страницы для нескольких документов.
         *
         * Используется при пустом поисковом запросе (режим «показать
         * все»): вместо N GET-запросов к
         * ``/api/documents/{id}/pages/0`` отправляется один POST.
         *
         * @param {Array<string>} docIds — список идентификаторов
         *   документов (до 20). Ограничение задано сервером
         *   (``PageTextBatchRequest.max_length``).
         * @param {number} pageNumber — номер страницы (0-based).
         * @returns {Promise<Object>} Promise с ответом
         *   ``{pages: {doc_id: text}, errors: {doc_id: message}}``.
         */
        fetchPagesBatch: function (docIds, pageNumber) {
            return fetch("/api/documents/pages/batch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    doc_ids: docIds,
                    page_number: pageNumber
                })
            }).then(function (response) {
                if (!response.ok) {
                    return response.json().then(function (data) {
                        throw new Error(
                            data.detail || "Ошибка пакетной загрузки"
                        );
                    });
                }
                return response.json();
            });
        }
    };

    /* ==============================================================
       5. Пасхалка «большой белый самолет»
       ============================================================== */

    /**
     * Проверяет условия срабатывания пасхалки и, если они выполнены,
     * запускает анимацию пролёта самолёта.
     *
     * @private
     */
    function checkAirplaneEasterEgg() {
        var searchInput = document.getElementById("search-input");
        var objectInput = document.getElementById("filter-object");
        if (!searchInput || !objectInput) return;

        var searchValue = normalizeFieldValue(searchInput.value);
        var objectValue = normalizeFieldValue(objectInput.value);

        if (
            searchValue === EASTER_EGG_SEARCH_PHRASE &&
            objectValue === EASTER_EGG_OBJECT_VALUE
        ) {
            triggerAirplaneAnimation();
        }
    }

    /**
     * Запускает анимацию пролёта самолёта слева направо.
     *
     * @private
     */
    function triggerAirplaneAnimation() {
        // Если анимация уже идёт — не создаём второй самолёт.
        if (document.querySelector(".dds-airplane-easter-egg")) {
            return;
        }

        var airplane = document.createElement("div");
        airplane.className = "dds-airplane-easter-egg";
        airplane.setAttribute("aria-hidden", "true");
        airplane.innerHTML =
            '<svg xmlns="http://www.w3.org/2000/svg" ' +
            '     viewBox="0 0 24 24" ' +
            '     class="icon dds-airplane-easter-egg__icon" ' +
            '     aria-hidden="true">' +
            '  <path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"></path>' +
            '  </svg>';

        document.body.appendChild(airplane);

        // Удаляем элемент после завершения анимации.
        window.setTimeout(function () {
            if (airplane.parentNode) {
                airplane.parentNode.removeChild(airplane);
            }
        }, EASTER_EGG_ANIMATION_MS);
    }

    /* ==============================================================
       6. Хелперы для SearchRenderer.renderResults
       ============================================================== */

    // Функции вызываются из SearchRenderer.renderResults, который
    // объявлен ниже. К моменту фактического вызова (через
    // performSearch, также определённый ниже) SearchRenderer уже
    // инициализирован. Прямых top-level вызовов нет.

    /**
     * Обновляет статус-бар поиска и визуализацию применённых фильтров.
     *
     * Единая точка для двух независимых побочных эффектов:
     * - запись текущего запроса/фильтров/счётчика документов
     *   в статус-бар (через ``window.DDSApp.updateSearchStatus``);
     * - подсветка полей фильтров с применёнными кодами
     *   (через ``FilterCoordinator.applyVisualState``).
     *
     * Оба вызова обёрнуты в ``typeof``-проверки: если модуль
     * не загружен (например, на странице без соответствующего
     * скрипта) — просто пропускаем.
     *
     * @param {Object} data — ответ API: ``{query, total, results}``.
     * @param {string} actualQuery — фактически использованный
     *   поисковый запрос.
     * @param {Object} appliedFilters — фактически применённые
     *   фильтры: ``{object_code, discipline_code,
     *   document_type_code, unmatched_only}``.
     * @private
     */
    function renderSearchStatus(data, actualQuery, appliedFilters) {
        if (typeof window.DDSApp !== "undefined" &&
            window.DDSApp.updateSearchStatus) {
            window.DDSApp.updateSearchStatus(
                actualQuery,
                data.total,
                appliedFilters
            );
        }

        if (typeof FilterCoordinator !== "undefined" &&
            typeof FilterCoordinator.applyVisualState === "function") {
            FilterCoordinator.applyVisualState(appliedFilters);
        }
    }

    /**
     * Делегирует рендеринг таблицы результатов в
     * ``SearchRenderer.renderTable``.
     *
     * Тривиальная обёртка сохранена для симметрии с остальными
     * хелперами (единый префикс ``render``) и на случай будущих
     * промежуточных операций (например, показа/скрытия индикатора
     * загрузки перед заполнением тела таблицы).
     *
     * @param {Array} results — массив документов.
     * @param {boolean} isQueryEmpty — признак пустого запроса.
     * @private
     */
    function renderResultsTable(results, isQueryEmpty) {
        SearchRenderer.renderTable(results, isQueryEmpty);
    }

    /**
     * Вычисляет количество страниц пагинации и делегирует
     * рендеринг в ``SearchRenderer.renderPagination``.
     *
     * Вычисление ``totalPages`` находится здесь, а не в
     * ``SearchRenderer.renderPagination``: контракт последнего
     * остаётся прежним (принимает уже готовое число страниц).
     *
     * @param {number} total — общее количество документов
     *   (``data.total``).
     * @param {number} currentPage — текущая страница (1-based,
     *   из ``SearchState.currentPage``).
     * @private
     */
    function renderPaginationControls(total, currentPage) {
        var totalPages = Math.ceil(total / SearchState.resultsPerPage);
        SearchRenderer.renderPagination(currentPage, totalPages);
    }

    /**
     * Показывает сообщение «Ничего не найдено» и скрывает таблицу
     * результатов.
     *
     * Вызывается только из оркестратора при ``data.total === 0``.
     * Обратная операция (показать таблицу, скрыть сообщение)
     * выполняется инлайн в оркестраторе: симметричная функция
     * ``renderNonEmptyState`` не вводится — это две строки, ради
     * которых отдельная абстракция была бы избыточной.
     *
     * @private
     */
    function renderEmptyState() {
        var noResults = document.getElementById("search-no-results");
        var wrapper = document.getElementById("search-results-wrapper");
        if (noResults) noResults.classList.remove("hidden");
        if (wrapper) wrapper.classList.add("hidden");
    }

    /* ==============================================================
       7. Рендеринг результатов
       ============================================================== */

    var SearchRenderer = {
        /**
         * Оркестратор рендеринга результатов поиска.
         *
         * Последовательность операций (порядок сохранён идентично
         * монолитной версии до Фазы 7):
         *
         * +----+----------------------------------------------------+
         * | №  | Описание                                           |
         * +====+====================================================+
         * | 1  | Сохранить ``data.results`` в                       |
         * |    | ``SearchState.lastResults`` (для контекстного      |
         * |    | меню).                                             |
         * +----+----------------------------------------------------+
         * | 2  | ``renderSearchStatus`` — статус-бар и фильтры.     |
         * +----+----------------------------------------------------+
         * | 3  | Снять ``hidden`` с ``#search-results-container``.  |
         * +----+----------------------------------------------------+
         * | 4  | При ``data.total === 0`` — ``renderEmptyState()``  |
         * |    | и выход.                                           |
         * +----+----------------------------------------------------+
         * | 5  | Скрыть ``#search-no-results``, показать            |
         * |    | ``#search-results-wrapper``.                       |
         * +----+----------------------------------------------------+
         * | 6  | ``renderResultsTable`` — заполнение таблицы.       |
         * +----+----------------------------------------------------+
         * | 7  | ``renderPaginationControls`` — пагинация.          |
         * +----+----------------------------------------------------+
         *
         * @param {Object} data — ответ API: ``{query, total, results}``.
         * @param {string} actualQuery — фактически использованный
         *   запрос.
         * @param {Object} appliedFilters — фактически применённые
         *   фильтры.
         */
        renderResults: function (data, actualQuery, appliedFilters) {
            // 1. Сохранить результаты для последующей обработки
            // правого клика (формирование termsByPage из готовых
            // terms, подготовленных сервером).
            SearchState.lastResults = data.results || [];

            // 2. Обновляем статус-бар и визуализацию фильтров.
            renderSearchStatus(data, actualQuery, appliedFilters);

            // 3. Показать контейнер результатов.
            var container = document.getElementById(
                "search-results-container"
            );
            if (!container) return;
            container.classList.remove("hidden");

            // 4. Пустое состояние — отдельная ветка с early return.
            if (data.total === 0) {
                renderEmptyState();
                return;
            }

            // 5. Непустое состояние: скрыть сообщение, показать
            // контейнер таблицы.
            var noResults = document.getElementById("search-no-results");
            var wrapper = document.getElementById("search-results-wrapper");
            if (noResults) noResults.classList.add("hidden");
            if (wrapper) wrapper.classList.remove("hidden");

            // 6. Заполнить таблицу результатов.
            renderResultsTable(data.results, actualQuery === "");

            // 7. Отрисовать пагинацию.
            renderPaginationControls(data.total, SearchState.currentPage);
        },

        /**
         * Заполняет тело таблицы сгруппированными по документам строками.
         *
         * Использование ``<template>`` (Фаза 7, ADR-007):
         *   Строки строятся через ``docRowTmpl.content.cloneNode(true)``.
         *   Шаблон ``tmpl-search-doc-row`` оборачивает ``<tr>`` в
         *   ``<table><tbody>``, чтобы парсер HTML5 корректно распознал
         *   table-элемент в контексте ``<template>``; из полученного
         *   fragment'а извлекается **только** ``<tr>`` через
         *   ``querySelector("tr.search-doc-row")``. Обёртка
         *   ``<table><tbody>`` не попадает в целевой ``<tbody>``.
         *
         *   Текст заполняется через ``textContent`` (автоматическое
         *   экранирование). Сниппеты заполняются через ``innerHTML``
         *   с ``escapeAndHighlight`` (безопасная разметка с ``<b>``).
         *
         *   Если шаблоны не найдены (рассинхронизация HTML и JS),
         *   выводится предупреждение в консоль, функция
         *   завершается без изменений в DOM.
         *
         * При ``isQueryEmpty === true`` вместо N GET-запросов к
         * ``/api/documents/{id}/pages/0`` отправляется один POST на
         * ``/api/documents/pages/batch`` (скорректированный план,
         * шаг 3.3). Ссылки на контейнеры сниппетов собираются в
         * ``Map<doc_id, HTMLElement>`` и передаются в
         * ``_loadFirstPagesBatch``.
         *
         * @param {Array} results — массив документов.
         * @param {boolean} isQueryEmpty — признак пустого запроса.
         */
        renderTable: function (results, isQueryEmpty) {
            var tbody = document.getElementById("search-results-body");
            if (!tbody) return;
            tbody.innerHTML = "";

            // Получить шаблоны строк. При отсутствии — early return
            // (защита от рассинхронизации main.html и search.js).
            var docRowTmpl = document.getElementById("tmpl-search-doc-row");
            var pageRowTmpl = document.getElementById("tmpl-search-page-row");
            if (!docRowTmpl || !pageRowTmpl) {
                console.warn(
                    "Шаблоны tmpl-search-doc-row / tmpl-search-page-row " +
                    "не найдены в DOM. Таблица результатов не построена."
                );
                return;
            }

            // Карта «doc_id → контейнер сниппета» для пакетной
            // загрузки первых страниц (используется только при
            // пустом запросе). Map вместо DOM-запроса по
            // data-атрибуту — защита от race с отложенным
            // рендерингом и от неоднозначностей sanitizeId.
            var snippetMap = new Map();

            for (var i = 0; i < results.length; i++) {
                var result = results[i];
                var filePath = result.file_path || "";
                var fileName = filePath
                    ? DocumentUtils.getBaseName(filePath, true)
                    : (result.doc_id || "Без имени");

                // Страницы: сортировка по возрастанию (страховка, сервер
                // уже сортирует).
                var pages = (result.pages || []).slice().sort(function (a, b) {
                    return a.page_number - b.page_number;
                });

                if (pages.length === 0) {
                    // Это не должно происходить на сервере, но защищаемся.
                    console.warn("Документ без страниц в результатах поиска:", result);
                }

                var hasMultiplePages = pages.length > 1;
                var minSnippet = pages.length > 0 ? (pages[0].snippet || "") : "";
                // Номер страницы с наименьшим номером. Используется
                // для открытия вкладки свойств документа по правому
                // клику — согласовано со сниппетом, показанным в
                // столбце «Фрагмент».
                var minPageNumber = pages.length > 0 ? pages[0].page_number : 0;

                // Для пустого запроса документ не привязан к конкретной
                // странице: на сервере создаётся виртуальная страница
                // page_number=0. Пользователю показываем прочерк.
                // Иначе — количество страниц формата «N шт.».
                var pagesListText;
                if (isQueryEmpty) {
                    pagesListText = "—";
                } else {
                    pagesListText = pages.length + " шт.";
                }

                var sanitizedDocId = sanitizeId(result.doc_id);
                var childrenContainerId = "search-children-" + sanitizedDocId;

                // ---------- Родительская строка документа ----------
                // Клонируем шаблон. В шаблоне <tr> обёрнут в
                // <table><tbody> (см. main.html); извлекаем только
                // <tr>, обёртку не вставляем в целевой tbody.
                var wrapper = docRowTmpl.content.cloneNode(true);
                var tr = wrapper.querySelector("tr.search-doc-row");
                if (!tr) {
                    console.warn(
                        "Шаблон tmpl-search-doc-row не содержит " +
                        "<tr class='search-doc-row'>. Пропуск документа."
                    );
                    continue;
                }

                if (hasMultiplePages) {
                    // Класс для курсора-указателя и подсветки.
                    tr.classList.add("expandable");
                }
                tr.dataset.docId = result.doc_id;
                // data-expanded="false" и aria-expanded="false" уже
                // присутствуют в шаблоне.

                // Ячейка 1: имя документа (ссылка).
                // href="#" и class="search-result-link" — из шаблона.
                var link = tr.querySelector(".search-result-link");
                link.dataset.docId = result.doc_id;
                link.dataset.fileName = fileName;
                // Номер страницы для открытия вкладки свойств
                // по правому клику.
                link.dataset.pageNumber = String(minPageNumber);
                link.textContent = fileName;
                link.title = filePath || result.doc_id || "";

                // Ячейка 2: количество страниц (кнопка раскрытия,
                // если > 1). Кнопка в шаблоне изначально
                // style="display: none".
                var tdPages = tr.querySelector(".search-pages-cell");
                var expandBtn = tr.querySelector(".search-expand-btn");
                if (hasMultiplePages) {
                    // Показать кнопку и заполнить текст.
                    expandBtn.style.display = "";
                    expandBtn.setAttribute("aria-controls", childrenContainerId);
                    expandBtn.setAttribute(
                        "aria-label",
                        "Раскрыть страницы документа " + fileName
                    );
                    // aria-expanded="false" — из шаблона.
                    var pagesList = tr.querySelector(".search-pages-list");
                    pagesList.textContent = pagesListText;
                } else {
                    // Кнопка не нужна — удалить; заменить содержимое
                    // ячейки на простой текст.
                    expandBtn.remove();
                    tdPages.textContent = pagesListText;
                }

                // Ячейка 3: сниппет страницы с наименьшим номером.
                var snippetDiv = tr.querySelector(
                    ".search-snippet-cell .snippet"
                );
                if (isQueryEmpty) {
                    // Заполнение контейнера отложенной пакетной
                    // загрузкой. Ссылка сохраняется в Map для
                    // последующей передачи в _loadFirstPagesBatch.
                    snippetMap.set(result.doc_id, snippetDiv);
                } else if (minSnippet) {
                    snippetDiv.innerHTML = this.escapeAndHighlight(minSnippet);
                }

                // Вставить готовую строку в тело таблицы.
                tbody.appendChild(tr);

                // ---------- Дочерняя строка-контейнер ----------
                // Контейнер <tr class="search-children-row"> создаётся
                // программно (не через <template>), так как его
                // <td colspan="3"> и id динамические.
                if (hasMultiplePages) {
                    var trChild = document.createElement("tr");
                    trChild.className = "search-children-row";
                    trChild.dataset.docId = result.doc_id;
                    trChild.dataset.expanded = "false";

                    var tdChild = document.createElement("td");
                    tdChild.colSpan = 3;
                    tdChild.className = "search-children-cell";

                    var childContainer = document.createElement("div");
                    childContainer.id = childrenContainerId;
                    childContainer.className = "search-children-container";

                    for (var j = 0; j < pages.length; j++) {
                        var page = pages[j];

                        // Клонируем шаблон дочерней строки страницы.
                        // Здесь проблем с парсингом нет: <div> внутри
                        // <template> разрешён в «in body» mode.
                        var pageFragment = pageRowTmpl.content.cloneNode(true);
                        var pageRow = pageFragment.querySelector(
                            ".search-page-row"
                        );
                        pageRow.dataset.pageNumber = String(page.page_number);

                        var pageNum = pageFragment.querySelector(
                            ".search-page-number"
                        );
                        pageNum.textContent = "Стр. " + (page.page_number + 1);

                        var pageSnippet = pageFragment.querySelector(".snippet");
                        if (isQueryEmpty) {
                            // Аналогично родительской строке:
                            // контейнер попадёт в Map для пакетной
                            // загрузки. Ключом остаётся doc_id —
                            // на одну страницу результата приходится
                            // один документ, одна запись в Map.
                            snippetMap.set(result.doc_id, pageSnippet);
                        } else {
                            pageSnippet.innerHTML = this.escapeAndHighlight(
                                page.snippet || ""
                            );
                        }

                        childContainer.appendChild(pageFragment);
                    }

                    tdChild.appendChild(childContainer);
                    trChild.appendChild(tdChild);
                    tbody.appendChild(trChild);
                }
            }

            this.bindResultLinks(tbody);

            // Пакетная загрузка первых страниц при пустом запросе:
            // один POST вместо N GET (шаг 3.3 плана v5.0).
            if (isQueryEmpty && snippetMap.size > 0) {
                this._loadFirstPagesBatch(snippetMap);
            }
        },

        /**
         * Пакетная загрузка текста первой страницы для всех документов,
         * присутствующих в текущем рендере.
         *
         * Отправляет один POST-запрос на ``/api/documents/pages/batch``
         * с массивом ``doc_ids``. Ограничение размера (20) задано
         * серверной моделью ``PageTextBatchRequest``. При типичном
         * ``resultsPerPage = 16`` укладываемся в лимит; если
         * количество документов превышает лимит, отправляются
         * последовательные партии (батч в батче — не усложняем;
         * используется последовательный обход партий через Promise).
         *
         * Ответ содержит:
         * - ``pages`` — карта успешно загруженных текстов;
         * - ``errors`` — карта сообщений об ошибках по doc_id.
         *
         * Установка текста через ``textContent`` — защита от XSS.
         * При сетевой ошибке всем документам устанавливается
         * сообщение «Ошибка загрузки».
         *
         * @param {Map<string, HTMLElement>} snippetMap — карта
         *   «doc_id → контейнер сниппета» (первый попавшийся
         *   контейнер для документа).
         * @private
         */
        _loadFirstPagesBatch: function (snippetMap) {
            var BATCH_MAX = 20; // Согласовано с серверной моделью.

            var allDocIds = Array.from(snippetMap.keys());
            if (allDocIds.length === 0) return;

            // Разбиваем на партии по BATCH_MAX.
            var batches = [];
            for (var start = 0; start < allDocIds.length; start += BATCH_MAX) {
                batches.push(allDocIds.slice(start, start + BATCH_MAX));
            }

            var self = this;

            // Последовательная обработка партий через reduce:
            // каждая партия дожидается предыдущей. Позволяет
            // работать с любым числом документов без параллельных
            // перегрузок api_executor.
            var chain = Promise.resolve();
            batches.forEach(function (batchDocIds) {
                chain = chain.then(function () {
                    return SearchAPI.fetchPagesBatch(batchDocIds, 0)
                        .then(function (data) {
                            self._applyBatchResult(snippetMap, data);
                        })
                        .catch(function () {
                            // Сетевая ошибка целой партии —
                            // помечаем все документы партии.
                            batchDocIds.forEach(function (docId) {
                                var el = snippetMap.get(docId);
                                if (el) {
                                    el.textContent = "Ошибка загрузки";
                                }
                            });
                        });
                });
            });
        },

        /**
         * Применяет результат пакетной загрузки к контейнерам сниппетов.
         *
         * @param {Map<string, HTMLElement>} snippetMap — карта
         *   «doc_id → контейнер сниппета».
         * @param {Object} data — ответ API
         *   ``{pages: {...}, errors: {...}}``.
         * @private
         */
        _applyBatchResult: function (snippetMap, data) {
            var pages = (data && data.pages) || {};
            var errors = (data && data.errors) || {};

            snippetMap.forEach(function (el, docId) {
                if (Object.prototype.hasOwnProperty.call(pages, docId)) {
                    el.textContent = pages[docId];
                } else if (Object.prototype.hasOwnProperty.call(errors, docId)) {
                    el.textContent = errors[docId];
                } else {
                    el.textContent = "Текст недоступен";
                }
            });
        },

        /**
         * Привязывает делегированные обработчики к tbody.
         *
         * @param {HTMLElement} tbody — тело таблицы результатов.
         */
        bindResultLinks: function (tbody) {
            // ---------- click (левый клик) ----------
            if (tbody.dataset.listenerAttached !== "true") {
                tbody.dataset.listenerAttached = "true";

                tbody.addEventListener("click", function (e) {
                    // 1. Клик по имени файла — скачивание.
                    var link = e.target.closest(".search-result-link");
                    if (link) {
                        e.preventDefault();
                        e.stopPropagation();
                        var progressContainer = link.parentNode.querySelector(
                            ".download-progress"
                        );
                        var docId = link.dataset.docId;
                        var fileName = link.dataset.fileName || docId;
                        SearchRenderer.downloadDocument(
                            docId,
                            fileName,
                            link,
                            progressContainer
                        );
                        return;
                    }

                    // 2. Клик по кнопке раскрытия — toggle.
                    var btn = e.target.closest(".search-expand-btn");
                    if (btn) {
                        e.preventDefault();
                        e.stopPropagation();
                        var btnDocRow = btn.closest("tr.search-doc-row");
                        if (btnDocRow) {
                            SearchRenderer.toggleDocumentGroup(btnDocRow);
                        }
                        return;
                    }

                    // 3. Клик в любом месте строки документа
                    //    (кроме ссылки и кнопки раскрытия) — toggle.
                    var docRow = e.target.closest("tr.search-doc-row");
                    if (docRow && docRow.classList.contains("expandable")) {
                        e.preventDefault();
                        e.stopPropagation();
                        SearchRenderer.toggleDocumentGroup(docRow);
                        return;
                    }
                });
            }

            // ---------- contextmenu (правый клик) ----------
            if (tbody.dataset.contextListenerAttached !== "true") {
                tbody.dataset.contextListenerAttached = "true";

                tbody.addEventListener("contextmenu", function (e) {
                    var link = e.target.closest(".search-result-link");
                    if (!link) return;

                    // Подавляем системное контекстное меню только
                    // над именем файла.
                    e.preventDefault();
                    e.stopPropagation();

                    var docId = link.dataset.docId;
                    if (!docId) return;
                    var fileName = link.dataset.fileName || docId;
                    var pageNumber = parseInt(link.dataset.pageNumber, 10);
                    if (isNaN(pageNumber) || pageNumber < 0) {
                        pageNumber = 0;
                    }

                    // Найти результат в последних результатах поиска.
                    var currentResult = null;
                    for (var i = 0; i < SearchState.lastResults.length; i++) {
                        if (SearchState.lastResults[i].doc_id === docId) {
                            currentResult = SearchState.lastResults[i];
                            break;
                        }
                    }

                    // Формирование карты termsByPage из готовых
                    // терминов, подготовленных сервером (Фаза 6,
                    // ADR-006). Клиент не парсит сниппеты.
                    var termsByPage = {};
                    if (currentResult && currentResult.pages) {
                        for (var j = 0; j < currentResult.pages.length; j++) {
                            var page = currentResult.pages[j];
                            if (page.terms && page.terms.length > 0) {
                                termsByPage[page.page_number] = page.terms;
                            }
                        }
                    }

                    var options = { termsByPage: termsByPage };

                    if (
                        typeof window.DDSApp !== "undefined" &&
                        typeof window.DDSApp.openDocumentTab === "function"
                    ) {
                        window.DDSApp.openDocumentTab(
                            docId,
                            fileName,
                            pageNumber,
                            options
                        );
                    } else {
                        console.warn(
                            "DDSApp.openDocumentTab недоступен: " +
                            "вкладка свойств документа не открыта."
                        );
                    }
                });
            }
        },

        /**
         * Переключает раскрытие/сворачивание группы страниц документа.
         *
         * @param {HTMLTableRowElement} docRow — родительская строка.
         */
        toggleDocumentGroup: function (docRow) {
            var docId = docRow.dataset.docId;
            var childRow = docRow.parentNode.querySelector(
                'tr.search-children-row[data-doc-id="' + docId + '"]'
            );
            if (!childRow) return;

            var container = childRow.querySelector(".search-children-container");
            if (!container) return;

            var isExpanded = docRow.dataset.expanded === "true";
            var next = !isExpanded;

            docRow.dataset.expanded = String(next);
            childRow.dataset.expanded = String(next);
            docRow.setAttribute("aria-expanded", String(next));

            if (next) {
                docRow.classList.add("active");
            } else {
                docRow.classList.remove("active");
            }

            var btn = docRow.querySelector(".search-expand-btn");
            if (btn) {
                btn.setAttribute("aria-expanded", String(next));
            }

            var snippetCell = docRow.querySelector(".search-snippet-cell");
            if (snippetCell) {
                if (next) {
                    snippetCell.setAttribute("aria-hidden", "true");
                } else {
                    snippetCell.removeAttribute("aria-hidden");
                }
            }

            if (next) {
                // Раскрытие: maxHeight из scrollHeight, затем "none".
                container.style.maxHeight = container.scrollHeight + "px";
                void container.offsetHeight;

                var onExpandEnd = function (e) {
                    if (e.propertyName !== "max-height") return;
                    container.removeEventListener("transitionend", onExpandEnd);
                    if (docRow.dataset.expanded === "true") {
                        container.style.maxHeight = "none";
                    }
                };
                container.addEventListener("transitionend", onExpandEnd);

                container.classList.add("is-expanded");
            } else {
                if (
                    container.style.maxHeight === "none" ||
                    container.style.maxHeight === ""
                ) {
                    container.style.maxHeight =
                        container.scrollHeight + "px";
                    void container.offsetHeight;
                }

                var onCollapseEnd = function (e) {
                    if (e.propertyName !== "max-height") return;
                    container.removeEventListener("transitionend", onCollapseEnd);
                    if (docRow.dataset.expanded === "false") {
                        container.style.maxHeight = "";
                    }
                };
                container.addEventListener("transitionend", onCollapseEnd);

                container.classList.remove("is-expanded");
                container.style.maxHeight = "0";
            }
        },

        /**
         * Скачивает документ с отображением прогресса.
         *
         * @param {string} docId — идентификатор документа.
         * @param {string} fileName — имя файла (с расширением).
         * @param {HTMLElement} linkElement — ссылка, по которой кликнули.
         * @param {HTMLElement} progressContainer — контейнер полосы
         *   прогресса.
         */
        downloadDocument: function (docId, fileName, linkElement, progressContainer) {
            linkElement.style.pointerEvents = "none";
            linkElement.style.opacity = "0.6";

            if (linkElement._downloadHideTimer) {
                clearTimeout(linkElement._downloadHideTimer);
                linkElement._downloadHideTimer = null;
            }
            if (linkElement._downloadRevokeTimer) {
                clearTimeout(linkElement._downloadRevokeTimer);
                linkElement._downloadRevokeTimer = null;
            }

            if (progressContainer) {
                progressContainer.style.display = "block";
                var bar = progressContainer.querySelector(".download-progress-bar");
                if (bar) {
                    bar.classList.remove("download-progress-bar--pending");
                    bar.style.width = "0%";
                }
            }

            var xhr = new XMLHttpRequest();
            xhr.open(
                "GET",
                "/api/documents/" + encodeURIComponent(docId) + "/download",
                true
            );
            xhr.responseType = "blob";

            xhr.onprogress = function (event) {
                if (event.lengthComputable && progressContainer) {
                    var percent = (event.loaded / event.total) * 100;
                    var progressBar = progressContainer.querySelector(
                        ".download-progress-bar"
                    );
                    if (progressBar) {
                        progressBar.style.width = percent + "%";
                    }
                }
            };

            xhr.onload = function () {
                linkElement.style.pointerEvents = "";
                linkElement.style.opacity = "";

                if (xhr.status === 200) {
                    var blob = xhr.response;
                    var url = URL.createObjectURL(blob);
                    var a = document.createElement("a");
                    a.href = url;
                    a.download = fileName || "document.pdf";
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);

                    linkElement._downloadRevokeTimer = setTimeout(function () {
                        URL.revokeObjectURL(url);
                        linkElement._downloadRevokeTimer = null;
                    }, 60000);

                    var pendingBar = progressContainer
                        ? progressContainer.querySelector(
                            ".download-progress-bar"
                        )
                        : null;
                    if (pendingBar) {
                        pendingBar.classList.add(
                            "download-progress-bar--pending"
                        );
                    }

                    linkElement._downloadHideTimer = setTimeout(function () {
                        if (progressContainer) {
                            progressContainer.style.display = "none";
                            var hideBar = progressContainer.querySelector(
                                ".download-progress-bar"
                            );
                            if (hideBar) {
                                hideBar.classList.remove(
                                    "download-progress-bar--pending"
                                );
                                hideBar.style.width = "0%";
                            }
                        }
                        linkElement._downloadHideTimer = null;
                    }, 5000);
                } else {
                    if (progressContainer) {
                        progressContainer.style.display = "none";
                    }
                    SearchRenderer.showError("Ошибка при скачивании файла");
                }
            };

            xhr.onerror = function () {
                linkElement.style.pointerEvents = "";
                linkElement.style.opacity = "";
                if (linkElement._downloadHideTimer) {
                    clearTimeout(linkElement._downloadHideTimer);
                    linkElement._downloadHideTimer = null;
                }
                if (linkElement._downloadRevokeTimer) {
                    clearTimeout(linkElement._downloadRevokeTimer);
                    linkElement._downloadRevokeTimer = null;
                }
                if (progressContainer) {
                    progressContainer.style.display = "none";
                }
                SearchRenderer.showError("Ошибка сети при скачивании файла");
            };

            xhr.send();
        },

        /**
         * Рендерит элементы пагинации.
         *
         * @param {number} currentPage — текущая страница (1-based).
         * @param {number} totalPages — общее количество страниц.
         */
        renderPagination: function (currentPage, totalPages) {
            var container = document.getElementById("search-pagination");
            if (!container) return;
            if (totalPages <= 1) {
                container.innerHTML = "";
                return;
            }
            var html = "";
            if (currentPage > 1) {
                html +=
                    '<a href="#" data-search-page="' +
                    (currentPage - 1) +
                    '">← Пред.</a>';
            }
            for (var p = 1; p <= totalPages; p++) {
                if (p === currentPage) {
                    html += '<span class="current">' + p + "</span>";
                } else if (
                    p <= 3 ||
                    p > totalPages - 3 ||
                    (p >= currentPage - 2 && p <= currentPage + 2)
                ) {
                    html +=
                        '<a href="#" data-search-page="' + p + '">' + p + "</a>";
                } else if (p === 4 || p === totalPages - 3) {
                    html += "<span>…</span>";
                }
            }
            if (currentPage < totalPages) {
                html +=
                    '<a href="#" data-search-page="' +
                    (currentPage + 1) +
                    '">След. →</a>';
            }
            container.innerHTML = html;

            var links = container.querySelectorAll("[data-search-page]");
            for (var i = 0; i < links.length; i++) {
                links[i].addEventListener("click", function (e) {
                    e.preventDefault();
                    var page = parseInt(this.dataset.searchPage, 10);
                    performSearch(SearchState.currentQuery, page);
                });
            }
        },

        showLoading: function () {
            var loading = document.getElementById("search-loading");
            var error = document.getElementById("search-error");
            var noResults = document.getElementById("search-no-results");
            var wrapper = document.getElementById("search-results-wrapper");
            if (loading) loading.classList.remove("hidden");
            if (error) error.classList.add("hidden");
            if (noResults) noResults.classList.add("hidden");
            if (wrapper) wrapper.classList.add("hidden");
        },

        hideLoading: function () {
            var loading = document.getElementById("search-loading");
            if (loading) loading.classList.add("hidden");
        },

        showError: function (message) {
            this.hideLoading();
            var error = document.getElementById("search-error");
            if (error) {
                error.textContent = message;
                error.classList.remove("hidden");
            }
        },

        showEmpty: function () {
            this.hideLoading();
            var noResults = document.getElementById("search-no-results");
            if (noResults) noResults.classList.remove("hidden");
        },

        /**
         * Экранирует HTML и заменяет нейтральные маркеры подсветки
         * на теги ``<b>``.
         *
         * Маркеры подсветки совпадений остаются частью сниппета
         * (сервер передаёт сниппет с ними), но клиент использует
         * их только для визуального выделения совпадений. Сами
         * термины подсветки приходят отдельно, в поле
         * ``pages[i].terms`` (см. ADR-006), и не извлекаются
         * из сниппета.
         *
         * @param {string} text — текст сниппета с маркерами.
         * @returns {string} — HTML-строка с тегами ``<b>`` вместо
         *   маркеров и экранированными спецсимволами.
         */
        escapeAndHighlight: function (text) {
            if (!text) return "";
            var escaped = escapeHtml(text);
            var START_MARKER = "[[DDS_HIGHLIGHT_START]]";
            var END_MARKER = "[[DDS_HIGHLIGHT_END]]";
            escaped = escaped.split(START_MARKER).join("<b>");
            escaped = escaped.split(END_MARKER).join("</b>");
            return escaped;
        }
    };

    /* ==============================================================
       8. Контроллер поиска
       ============================================================== */

    /**
     * Выполняет поиск с учётом режимов и фильтра несоответствующих.
     *
     * @param {string} query — текст из основного поля поиска.
     * @param {number} page — номер страницы (1-based).
     */
    function performSearch(query, page) {
        query = query || "";
        SearchState.currentQuery = query;
        SearchState.currentPage = page || 1;
        // Сбрасываем кэш последних результатов.
        SearchState.lastResults = [];
        SearchRenderer.showLoading();

        var filterOnly = FilterCoordinator
            ? FilterCoordinator.isFilterOnlyMode()
            : false;
        var contentOnly = FilterCoordinator
            ? FilterCoordinator.isContentOnlyMode()
            : false;

        // Фактический запрос
        var actualQuery = filterOnly ? "" : query.trim();
        // Получаем значения фильтров (с учётом режима contentOnly)
        var filterValues = FilterCoordinator
            ? FilterCoordinator.getFilterValues()
            : {
                object_code: null,
                discipline_code: null,
                document_type_code: null,
                unmatched_only: false
            };

        // Проверяем наличие активных обычных фильтров через единый метод
        var hasActiveFilters = FilterCoordinator
            ? FilterCoordinator.hasActiveOrdinaryFilters()
            : false;

        // Если активен фильтр несоответствующих, но нет активных обычных
        // фильтров, сбрасываем его в FilterCoordinator и не передаём в API.
        if (filterValues.unmatched_only && !hasActiveFilters) {
            if (
                typeof FilterCoordinator !== "undefined" &&
                typeof FilterCoordinator.setUnmatchedOnly === "function"
            ) {
                FilterCoordinator.setUnmatchedOnly(false);
            }
            filterValues.unmatched_only = false;
        }

        // Формируем фактические фильтры для статус-бара и API
        var appliedFilters = contentOnly
            ? {
                object_code: null,
                discipline_code: null,
                document_type_code: null,
                unmatched_only: false
            }
            : {
                object_code: filterValues.object_code,
                discipline_code: filterValues.discipline_code,
                document_type_code: filterValues.document_type_code,
                unmatched_only: filterValues.unmatched_only
            };

        var apiFilters = appliedFilters;

        var offset =
            (SearchState.currentPage - 1) * SearchState.resultsPerPage;

        SearchAPI.search(
            actualQuery,
            apiFilters,
            SearchState.resultsPerPage,
            offset
        )
            .then(function (data) {
                SearchRenderer.hideLoading();
                SearchRenderer.renderResults(
                    data,
                    actualQuery,
                    appliedFilters
                );
            })
            .catch(function (error) {
                SearchRenderer.showError(error.message);
            });
    }

    /* ==============================================================
       9. Инициализация и привязка обработчиков
       ============================================================== */

    function initSearch() {
        var searchForm = document.getElementById("search-form");
        if (searchForm) {
            searchForm.addEventListener("submit", function (e) {
                e.preventDefault();

                // Пасхалка: проверяем условия до запуска поиска.
                checkAirplaneEasterEgg();

                var searchInput = document.getElementById("search-input");
                var query = searchInput ? searchInput.value : "";
                // Разрешаем пустой запрос
                performSearch(query, 1);
            });
        }
    }

    /* ==============================================================
       10. Экспорт для внешних модулей
       ============================================================== */

    if (typeof window !== "undefined") {
        window.SearchModule = {
            performSearch: performSearch
        };
    }

    /* ==============================================================
       11. Запуск инициализации
       ============================================================== */

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initSearch);
    } else {
        initSearch();
    }
})();
