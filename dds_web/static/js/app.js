/**
 * Главный скрипт веб-приложения DDS.
 *
 * Управляет системой вкладок в центральной области интерфейса
 * (референс — Visual Studio Code), загрузкой и отображением
 * документов, а также координацией между компонентами.
 *
 * Рефакторинг статус-бара:
 *
 * +----------------------------------+----------------------------------+
 * | Изменение                        | Описание                         |
 * +==================================+==================================+
 * | Удалён элемент индекса           | Из статус-бара убран индикатор   |
 * |                                  | состояния индекса.               |
 * +----------------------------------+----------------------------------+
 * | Добавлены поля поиска            | В статус-бар добавлены элементы  |
 * |                                  | ``#statusbar-query``,             |
 * |                                  | ``#statusbar-object``,            |
 * |                                  | ``#statusbar-discipline``,        |
 * |                                  | ``#statusbar-type``,              |
 * |                                  | ``#statusbar-unmatched`` и        |
 * |                                  | ``#statusbar-results-count``.    |
 * +----------------------------------+----------------------------------+
 * | Новая логика статуса сканирования| Статус сканирования формируется  |
 * |                                  | на основе состояния индекса.     |
 * +----------------------------------+----------------------------------+
 * | Управление сообщением о          | После завершения сканирования    |
 * | завершении                       | сообщение сбрасывается через     |
 * |                                  | таймер.                          |
 * +----------------------------------+----------------------------------+
 * | Инициализация UIStateManager     | Глобальное состояние приложения  |
 * | и SettingsModule                 | инициализируется в ``AppInit()``.|
 * +----------------------------------+----------------------------------+
 * | Централизованное управление SSE  | Функция ``startScanMonitoring``  |
 * |                                  | регистрирует обработчики один раз|
 * |                                  | и управляет подключением.        |
 * +----------------------------------+----------------------------------+
 * | Актуальный статус сканирования   | При инициализации статус-бара    |
 * |                                  | выполняется запрос                |
 * |                                  | ``GET /api/scan/status``.         |
 * +----------------------------------+----------------------------------+
 * | Автоматическая проверка индекса  | Если сканирование не активно,    |
 * | при запуске                      | вызывается принудительный         |
 * |                                  | пересчёт состояния индекса.      |
 * +----------------------------------+----------------------------------+
 * | Условная панель прокрутки        | Добавлена функция                |
 * |                                  | ``setupConditionalScrollbar``.    |
 * +----------------------------------+----------------------------------+
 * | Инициализация фильтров           | Добавлен вызов                    |
 * |                                  | ``FilterCoordinator.init()``.     |
 * +----------------------------------+----------------------------------+
 * | Индикация вторичного сканирования| Добавлены обработчики событий    |
 * |                                  | ``onSecondaryStarted``,           |
 * |                                  | ``onSecondaryProgress``,          |
 * |                                  | ``onSecondaryCompleted``.         |
 * +----------------------------------+----------------------------------+
 * | Расширенный статус поиска        | Функция ``updateSearchStatus``    |
 * |                                  | принимает информацию о фильтрах.  |
 * +----------------------------------+----------------------------------+
 * | Индикация активности             | При переключении вкладки          |
 * | Activity Bar                     | устанавливается класс ``active``  |
 * |                                  | на соответствующей иконке        |
 * |                                  | Activity Bar.                     |
 * +----------------------------------+----------------------------------+
 * | Плоские SVG-иконки вкладок       | Emoji ``📄`` заменён на          |
 * |                                  | инлайновый SVG документ. SVG      |
 * |                                  | помечен ``aria-hidden="true"``.   |
 * +----------------------------------+----------------------------------+
 * | API для управления иконкой поиска| Экспортированы методы            |
 * |                                  | ``setSearchIconActive(bool)`` и   |
 * |                                  | ``getSearchIconActive()`` для     |
 * |                                  | координации с ``settings.js``.    |
 * +----------------------------------+----------------------------------+
 * | Унификация управления иконкой    | Применение активного состояния   |
 * | поиска                           | иконки поиска централизовано     |
 * |                                  | в приватном методе                |
 * |                                  | ``TabManager._applySearchIconState``.|
 * |                                  | Публичный ``setSearchIconActive`` |
 * |                                  | делегирует в него.               |
 * +----------------------------------+----------------------------------+
 * | Защита от конфликта с панелью    | ``TabManager._updateActivityBarState``|
 * | настроек                         | пропускает обновление иконки      |
 * |                                  | поиска, если панель настроек     |
 * |                                  | открыта (``SettingsModule.isOpen()``).|
 * +----------------------------------+----------------------------------+
 * | Подпись счётчика результатов     | В ``updateSearchStatus`` подпись  |
 * |                                  | изменена с «Результатов: N» на   |
 * |                                  | «Документов: N», так как поиск   |
 * |                                  | возвращает одну запись на        |
 * |                                  | документ (серверная группировка).|
 * +----------------------------------+----------------------------------+
 * | Экспорт ``openDocumentTab``      | В ``window.DDSApp`` добавлен      |
 * |                                  | публичный метод                   |
 * |                                  | ``openDocumentTab(docId,          |
 * |                                  | fileName, pageNumber, options)``, |
 * |                                  | делегирующий в                    |
 * |                                  | ``TabManager.openDocumentTab``.   |
 * |                                  | Используется из ``search.js``     |
 * |                                  | при правом клике по имени файла  |
 * |                                  | в результатах поиска для         |
 * |                                  | открытия вкладки свойств         |
 * |                                  | документа.                        |
 * +----------------------------------+----------------------------------+
 *
 * Расширение для предпросмотра документа (Фаза 4):
 *
 * +----------------------------------+----------------------------------+
 * | Изменение                        | Описание                         |
 * +==================================+==================================+
 * | Расширение ``AppState``          | В записи ``openDocuments``       |
 * |                                  | добавлены поля ``options``,      |
 * |                                  | ``renderer``, ``viewMode``,      |
 * |                                  | ``navAllPages`` и ``pageCount``. |
 * |                                  | Поле ``_seq`` (защита от race)   |
 * |                                  | было добавлено в Фазе 4 и        |
 * |                                  | удалено в Фазе 7 (ADR-007) при   |
 * |                                  | переходе на ``AbortController``. |
 * +----------------------------------+----------------------------------+
 * | Фасад ``window.DDSApp``          | Добавлены методы                  |
 * |                                  | ``getDocumentRecord``,            |
 * |                                  | ``updateDocumentRecord``,         |
 * |                                  | ``getActiveDocumentRecord`` и     |
 * |                                  | ``ensureRenderer``.               |
 * |                                  | Обеспечивают доступ к per-tab    |
 * |                                  | состоянию из ``document.js``     |
 * |                                  | без прямого доступа к ``AppState``|
 * |                                  | (IIFE-скоуп).                    |
 * +----------------------------------+----------------------------------+
 * | Создание ``PageRenderer``        | В ``openDocumentTab`` создаётся  |
 * |                                  | ``createPageRenderer(docId)`` —  |
 * |                                  | изолированный per-tab объект     |
 * |                                  | с собственным ``_blobUrl``,      |
 * |                                  | ``_zoom`` и флагом fit-width.    |
 * +----------------------------------+----------------------------------+
 * | Переключатель режима             | В panel вставляется контейнер    |
 * |                                  | ``doc-view-switch-{docId}``;      |
 * |                                  | ``ViewModeManager.installSwitcher``|
 * |                                  | создаёт кнопки «Рендер» /        |
 * |                                  | «Текст».                          |
 * +----------------------------------+----------------------------------+
 * | Чекбокс «Ротация координат»      | В panel добавлен блок            |
 * |                                  | ``.doc-render-controls`` с        |
 * |                                  | чекбоксом                         |
 * |                                  | ``doc-transform-{docId}`` между   |
 * |                                  | ``doc-view-switch`` и             |
 * |                                  | ``doc-meta``. Чекбокс изначально  |
 * |                                  | ``disabled`` и устанавливается    |
 * |                                  | по результату авто-диагностики    |
 * |                                  | сервера (см. ``document.js``).    |
 * +----------------------------------+----------------------------------+
 * | Унификация чекбокс-метки         | Класс ``.doc-nav-filter``         |
 * |                                  | переименован в                    |
 * |                                  | ``.doc-checkbox-label`` (общий    |
 * |                                  | для навигации и трансформации).   |
 * |                                  | Состояние ``is-disabled``         |
 * |                                  | сохраняется как модификатор.      |
 * +----------------------------------+----------------------------------+
 * | Панель навигации и чекбокс       | В panel добавлен контейнер       |
 * | «Навигация по всем страницам»    | ``.doc-nav-controls`` с          |
 * |                                  | пагинацией слева и чекбоксом     |
 * |                                  | справа. При снятом чекбоксе      |
 * |                                  | навигация идёт только по         |
 * |                                  | страницам с совпадениями         |
 * |                                  | (``options.termsByPage``).       |
 * |                                  | Чекбокс disabled, если у         |
 * |                                  | документа нет совпадений         |
 * |                                  | (открыт без ``termsByPage``).    |
 * +----------------------------------+----------------------------------+
 * | Обработчик ``Ctrl+0``            | В ``AppInit`` регистрируется     |
 * |                                  | единственный обработчик          |
 * |                                  | ``keydown`` для сброса zoom      |
 * |                                  | активной вкладки документа       |
 * |                                  | через ``renderer.resetZoom()``.  |
 * +----------------------------------+----------------------------------+
 * | ``closeDocumentTab`` с cleanup   | При закрытии вкладки вызывается  |
 * |                                  | ``renderer.cleanup()`` для       |
 * |                                  | освобождения blob URL и          |
 * |                                  | pan-обработчиков.                |
 * +----------------------------------+----------------------------------+
 *
 * Инкапсуляция публичного API предпросмотра (скорректированный план):
 *
 * +----------------------------------+----------------------------------+
 * | Изменение                        | Описание                         |
 * +==================================+==================================+
 * | ``refreshHighlights`` вместо     | Обработчик чекбокса «Ротация     |
 * | ``_fetchAndApplyHighlights``     | координат» вызывает публичный    |
 * |                                  | ``renderer.refreshHighlights(    |
 * |                                  | page, terms)`` вместо приватного |
 * |                                  | ``_fetchAndApplyHighlights``.    |
 * +----------------------------------+----------------------------------+
 *
 * Cache-busting тем (Фаза 7, ADR-007):
 *
 * +----------------------------------+----------------------------------+
 * | Изменение                        | Описание                         |
 * +==================================+==================================+
 * | Чтение ``build_hash``            | В ``AppInit`` из                 |
 * |                                  | ``#main-init-data.dataset.buildHash``|
 * |                                  | читается строка build_hash.      |
 * +----------------------------------+----------------------------------+
 * | Экспорт ``window.DDSApp.buildHash``| Публичное поле обновляется     |
 * |                                  | в ``AppInit`` после чтения.      |
 * +----------------------------------+----------------------------------+
 * | Использование в ``ThemeManager``  | ``applyTheme`` добавляет        |
 * |                                  | ``?v=<buildHash>`` к href темы.  |
 * +----------------------------------+----------------------------------+
 *
 * ``AbortController`` для отмены fetch (Фаза 7, ADR-007):
 *
 * +----------------------------------+----------------------------------+
 * | Изменение                        | Описание                         |
 * +==================================+==================================+
 * | Поле ``_abortController``        | Добавлено в каждую запись        |
 * | в записи вкладки                 | ``AppState.openDocuments``.      |
 * |                                  | Один контроллер на вкладку.      |
 * +----------------------------------+----------------------------------+
 * | ``getAbortController(docId)``    | Возвращает существующий          |
 * |                                  | контроллер или создаёт новый,    |
 * |                                  | если запись не имела его.        |
 * +----------------------------------+----------------------------------+
 * | ``resetAbortController(docId)``  | Прерывает все pending fetch      |
 * |                                  | вкладки, создаёт свежий          |
 * |                                  | контроллер, возвращает его.      |
 * |                                  | Вызывается в ``loadDocument``    |
 * |                                  | при смене страницы/режима.       |
 * +----------------------------------+----------------------------------+
 * | ``abort()`` при закрытии         | В ``closeDocumentTab`` перед     |
 * | вкладки                          | ``renderer.cleanup()``           |
 * |                                  | вызывается ``abort()`` —         |
 * |                                  | устаревшие fetch прерываются.    |
 * +----------------------------------+----------------------------------+
 *
 * Удаление ``_seq`` (Фаза 7, ADR-007, шаг 4b):
 *
 *   До Фазы 7 защита от race condition обеспечивалась счётчиком
 *   ``_seq`` (инкрементировался при каждой загрузке, все async-ответы
 *   проверяли актуальность). В Фазе 7 (шаг 4a) ``AbortController``
 *   добавлен **параллельно** с ``_seq``: pending fetch прерываются
 *   через ``resetAbortController`` при смене страницы/режима.
 *
 *   На шаге 4b (текущий) ``_seq`` **полностью удалён** из ``app.js``
 *   и ``document.js``: удалены методы ``incrementLoadSeq`` /
 *   ``getLoadSeq`` и поле ``_seq`` из записи вкладки; все
 *   ``_seq``-проверки в ``document.js`` заменены на ``signal.aborted``.
 *
 *   Дополнительная защита для случая ``refreshHighlights``
 *   (пересчёт подсветки без передачи ``signal``) — проверка
 *   ``rec.page !== pageNumber`` в ``document.js``.
 *
 * Примечание:
 * Инициализация темы оформления выполняется на более раннем этапе
 * (в ``base.html`` через inline-скрипт), поэтому в ``AppInit()``
 * этот шаг отсутствует.
 *
 * Архитектура модуля:
 *
 * +----------------------------------+----------------------------------+
 * | Компонент                        | Ответственность                  |
 * +==================================+==================================+
 * | ``AppState``                     | Глобальное состояние приложения. |
 * +----------------------------------+----------------------------------+
 * | ``TabManager``                   | Управление вкладками и активным  |
 * |                                  | состоянием иконок Activity Bar.  |
 * +----------------------------------+----------------------------------+
 * | ``updateScanStatusHint``         | Формирует текст статуса          |
 * |                                  | сканирования.                    |
 * +----------------------------------+----------------------------------+
 * | ``updateSearchStatus``           | Обновляет информацию о поиске.   |
 * +----------------------------------+----------------------------------+
 * | ``startScanMonitoring``          | Запускает мониторинг SSE.        |
 * +----------------------------------+----------------------------------+
 * | ``initStatusBar``                | Инициализация статус-бара.       |
 * +----------------------------------+----------------------------------+
 * | ``setupConditionalScrollbar``    | Управление видимостью полосы     |
 * |                                  | прокрутки.                       |
 * +----------------------------------+----------------------------------+
 * | ``setSearchIconActive``          | Публичный API для управления     |
 * | ``getSearchIconActive``          | активным состоянием иконки поиска|
 * |                                  | (используется ``settings.js``).  |
 * +----------------------------------+----------------------------------+
 * | ``openDocumentTab``              | Публичный API для открытия       |
 * |                                  | вкладки свойств документа        |
 * |                                  | (используется ``search.js``).    |
 * +----------------------------------+----------------------------------+
 * | ``getDocumentRecord``            | Публичный API для доступа к      |
 * | ``updateDocumentRecord``         | per-tab состоянию из             |
 * | ``getActiveDocumentRecord``      | ``document.js`` (фасад над       |
 * | ``ensureRenderer``               | ``AppState``).                   |
 * | ``getAbortController``           |                                  |
 * | ``resetAbortController``         |                                  |
 * +----------------------------------+----------------------------------+
 * | ``AppInit``                      | Инициализация приложения.        |
 * +----------------------------------+----------------------------------+
 *
 * Принципы:
 * - Модуль не содержит бизнес-логики — только отображение
 *   и координация.
 * - Данные загружаются через REST API.
 * - Интеграция с ``UIStateManager``, ``SSEModule``,
 *   ``FilterCoordinator``, ``SettingsModule``, ``ThemeManager``
 *   (косвенно).
 * - Публичный API ``window.DDSApp`` служит фасадом для внешних
 *   модулей; внутренние детали (``TabManager``, ``AppState``)
 *   не экспортируются напрямую.
 * - Per-tab состояние (renderer, blob URL, zoom, viewMode, options,
 *   navAllPages, pageCount, ``AbortController``) хранится в записях
 *   ``AppState.openDocuments``; доступ из других модулей — только
 *   через фасад ``window.DDSApp``.
 * - ``AbortController`` — единственный механизм отмены pending
 *   fetch (Фаза 7, ADR-007, шаг 4b).
 *
 * Зависимости:
 * - ``ui_state_manager.js``, ``sse.js``, ``document.js``,
 *   ``filter_coordinator.js``, ``search.js``, ``settings.js``,
 *   ``theme_manager.js``.
 * - ``main.html`` — DOM-структура.
 *
 * Порядок загрузки скриптов в ``main.html`` (важно для фасада):
 * ``document.js`` подключается **до** ``app.js``, поэтому
 * ``window.createPageRenderer`` и ``window.ViewModeManager``
 * доступны на момент инициализации ``TabManager``.
 */
(function () {
    "use strict";

    /* ==============================================================
       1. Константы
       ============================================================== */

    var MAX_OPEN_TABS = 10;

    /**
     * Короткий идентификатор сборки для cache-busting статических
     * ресурсов (Фаза 7, ADR-007).
     *
     * Инициализируется в :func:`AppInit` из атрибута
     * ``data-build-hash`` элемента ``#main-init-data``
     * (передаётся из ``main.html``, где доступен через
     * ``templates.env.globals``).
     *
     * Используется ``ThemeManager.applyTheme`` для добавления
     * query-параметра ``?v=<buildHash>`` к ``href`` активной
     * темы. Это устраняет использование устаревшей версии CSS
     * из браузерного кеша после пересборки.
     *
     * Начальное значение — пустая строка. После ``AppInit``
     * содержит либо hash (``"dev"`` в не-git окружении, либо
     * короткий git-хеш), либо пустую строку, если атрибут
     * отсутствует.
     *
     * @type {string}
     */
    var _buildHash = "";

    /* ==============================================================
       2. Импорт внешних модулей
       ============================================================== */

    var DocumentLoader = window.DocumentLoader;
    var DocumentUtils = window.DocumentUtils;
    var escapeHtml = DocumentUtils.escapeHtml;
    var FilterCoordinator = window.FilterCoordinator;

    /* ==============================================================
       3. Состояние приложения
       ============================================================== */

    /**
     * Глобальное состояние приложения.
     *
     * Хранит активную вкладку и список открытых документов.
     * Каждая запись в ``openDocuments`` — отдельный документ
     * со своим per-tab состоянием:
     *
     * - ``docId`` — идентификатор документа.
     * - ``fileName`` — отображаемое имя файла.
     * - ``page`` — текущая страница (0-based).
     * - ``options`` — настройки вкладки, включая карту терминов
     *   для подсветки ``termsByPage`` (или ``null``).
     * - ``renderer`` — объект ``PageRenderer``, созданный через
     *   ``createPageRenderer(docId)``. Хранит ``_blobUrl``,
     *   ``_zoom``, флаг fit-width и др.
     * - ``viewMode`` — текущий режим: ``"render"`` или ``"text"``.
     * - ``_abortController`` — ``AbortController`` вкладки
     *   (Фаза 7, ADR-007). Прерывает pending fetch при смене
     *   страницы/режима/закрытии вкладки. Единственный механизм
     *   отмены pending fetch (шаг 4b: ``_seq`` удалён).
     * - ``navAllPages`` — режим навигации: ``true`` = все
     *   страницы; ``false`` = только страницы с совпадениями.
     * - ``pageCount`` — общее количество страниц документа.
     *   Заполняется в ``DocumentLoader.loadDocument`` после
     *   получения метаданных; используется
     *   ``DocumentLoader.refreshNavigation`` для перерисовки
     *   пагинации без повторного запроса метаданных.
     */
    var AppState = {
        activeTabId: "search",
        openDocuments: []
    };

    /* ==============================================================
       4. Управление вкладками
       ============================================================== */

    var TabManager = {
        /**
         * Переключает активную вкладку и обновляет состояние
         * иконок Activity Bar.
         *
         * @param {string} tabId — идентификатор вкладки.
         */
        switchTab: function (tabId) {
            var tabs = document.querySelectorAll(".tab");
            for (var i = 0; i < tabs.length; i++) {
                tabs[i].classList.remove("active");
            }
            var panels = document.querySelectorAll(".tab-panel");
            for (var j = 0; j < panels.length; j++) {
                panels[j].style.display = "none";
            }
            var targetTab = this.getTabElement(tabId);
            if (targetTab) {
                targetTab.classList.add("active");
            }
            var targetPanel = this.getPanelElement(tabId);
            if (targetPanel) {
                targetPanel.style.display = "block";
            }
            AppState.activeTabId = tabId;
            this._updateActivityBarState(tabId);
        },

        /**
         * Обновляет активное состояние иконок Activity Bar.
         *
         * Иконка поиска активна при открытой вкладке поиска
         * или любой вкладке документа. Иконка настроек управляется
         * отдельно из ``settings.js``.
         *
         * Если панель настроек открыта, обновление иконки поиска
         * пропускается, чтобы не конфликтовать с временным
         * состоянием, установленным ``settings.js``.
         *
         * @param {string} tabId — идентификатор активной вкладки.
         * @private
         */
        _updateActivityBarState: function (tabId) {
            // Если панель настроек открыта, не трогаем иконку поиска:
            // settings.js временно управляет её активным состоянием.
            if (window.SettingsModule &&
                typeof window.SettingsModule.isOpen === "function" &&
                window.SettingsModule.isOpen()) {
                return;
            }
            var isSearchActive = (tabId === "search") || (tabId.indexOf("doc-") === 0);
            this._applySearchIconState(isSearchActive);
        },

        /**
         * Устанавливает активное состояние иконки поиска Activity Bar.
         * Единая точка изменения класса ``active`` на ``#activity-search``.
         *
         * Используется как внутренней логикой ``TabManager``,
         * так и публичным API ``DDSApp.setSearchIconActive``,
         * вызываемым из ``settings.js``.
         *
         * @param {boolean} isActive — если ``true``, класс ``active``
         *   добавляется; если ``false`` — снимается.
         * @private
         */
        _applySearchIconState: function (isActive) {
            var searchIcon = document.getElementById("activity-search");
            if (!searchIcon) return;
            if (isActive) {
                searchIcon.classList.add("active");
            } else {
                searchIcon.classList.remove("active");
            }
        },

        /**
         * Открывает вкладку документа.
         *
         * Если вкладка уже открыта — обновляет ``options`` и ``page``
         * в записи ``AppState.openDocuments``, синхронизирует
         * состояние чекбокса «Навигация по всем страницам» и
         * переключается на неё. Иначе создаёт новую вкладку и панель,
         * добавляет их в DOM, создаёт ``PageRenderer`` через
         * ``createPageRenderer``, устанавливает переключатель
         * режимов, чекбокс «Ротация координат» и чекбокс навигации,
         * запускает загрузку через ``DocumentLoader.loadDocument``.
         *
         * При превышении лимита ``MAX_OPEN_TABS`` закрывает
         * самую старую вкладку документа.
         *
         * Проверка `hasMatches`:
         * `hasMatches` вычисляется один раз в начале метода
         * по входному `options`. В ветке `existingTab` оно
         * пересчитывается по актуальным `newOptions`
         * (могут отличаться от входных, если `options`
         * не передан и используются сохранённые опции вкладки).
         *
         * Метод экспортируется через ``window.DDSApp.openDocumentTab``
         * и вызывается из ``search.js`` при правом клике по имени
         * файла в результатах поиска. Также может вызываться из
         * других модулей через публичный API.
         *
         * @param {string} docId — идентификатор документа.
         * @param {string} fileName — отображаемое имя файла.
         * @param {number} pageNumber — номер страницы (0-based),
         *   с которой начинается просмотр.
         * @param {Object} [options] — дополнительные параметры:
         *   - ``termsByPage`` — карта «номер страницы → термины»
         *     для подсветки на рендере. Формируется в ``search.js``
         *     из сниппетов FTS5.
         */
        openDocumentTab: function (docId, fileName, pageNumber, options) {
            var tabId = "doc-" + docId;
            var existingTab = this.getTabElement(tabId);
            if (existingTab) {
                // Обновление существующей вкладки.
                var rec = window.DDSApp.getDocumentRecord(docId);
                var newOptions = options || (rec ? rec.options : null);

                // Пересчёт hasMatches для новых options.
                var hasMatchesExisting = !!(newOptions
                    && newOptions.termsByPage
                    && Object.keys(newOptions.termsByPage).length > 0);

                var updates = {
                    page: pageNumber,
                    options: newOptions,
                };

                // При отсутствии совпадений принудительно включаем
                // режим «все страницы» — иначе фильтрованная
                // навигация окажется пустой.
                if (!hasMatchesExisting) {
                    updates.navAllPages = true;
                }

                window.DDSApp.updateDocumentRecord(docId, updates);

                // Синхронизация UI чекбокса с новым состоянием.
                var navAllCheckbox = document.getElementById("doc-nav-all-" + docId);
                if (navAllCheckbox) {
                    var label = navAllCheckbox.closest(".doc-checkbox-label");
                    if (!hasMatchesExisting) {
                        navAllCheckbox.disabled = true;
                        navAllCheckbox.checked = true;
                        if (label) label.classList.add("is-disabled");
                    } else {
                        navAllCheckbox.disabled = false;
                        navAllCheckbox.checked = (rec.navAllPages !== false);
                        if (label) label.classList.remove("is-disabled");
                    }
                }

                this.switchTab(tabId);
                var renderer = window.DDSApp.ensureRenderer(docId);
                DocumentLoader.loadDocument(
                    docId, fileName, pageNumber, newOptions, renderer
                );
                return;
            }

            if (AppState.openDocuments.length >= MAX_OPEN_TABS) {
                var oldestDoc = AppState.openDocuments[0];
                var oldestTabId = "doc-" + oldestDoc.docId;
                this.closeDocumentTab(oldestTabId);
            }

            // Вычисляем hasMatches один раз для разметки и записи.
            var hasMatches = !!(options
                && options.termsByPage
                && Object.keys(options.termsByPage).length > 0);

            var tab = document.createElement("div");
            tab.className = "tab";
            tab.dataset.tabId = tabId;
            // Плоская SVG-иконка документа (стиль Feather/VSCode).
            // SVG помечен aria-hidden="true" — вкладка озвучивается по тексту.
            tab.innerHTML =
                '<span class="tab__icon">' +
                '  <svg xmlns="http://www.w3.org/2000/svg" ' +
                '       viewBox="0 0 24 24" ' +
                '       class="icon icon--xs" ' +
                '       aria-hidden="true">' +
                '    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>' +
                '    <polyline points="14 2 14 8 20 8"></polyline>' +
                '  </svg>' +
                '</span>' +
                '<span class="tab__label">' + escapeHtml(fileName) + "</span>" +
                '<span class="tab__close" data-close-tab="' + tabId + '">✕</span>';

            var tabBar = document.querySelector(".tab-bar");
            if (tabBar) {
                tabBar.appendChild(tab);
            }

            // Разметка panel: view-switch, чекбокс «Ротация координат»,
            // метаданные, панель навигации с чекбоксом «Навигация по
            // всем страницам», контейнер содержимого страницы.
            //
            // Чекбокс «Ротация координат» создаётся в состоянии
            // disabled (авто-диагностика ещё не выполнялась). После
            // первого запроса подсветки чекбокс будет активирован и
            // установлен по фактическому флагу трансформации
            // (см. ``document.js::_syncTransformCheckbox``).
            //
            // Чекбокс «Навигация по всем страницам» находится в
            // обёртке ``.doc-checkbox-label`` (общий класс для
            // чекбокс-меток вкладки) внутри ``.doc-nav-controls``;
            // при отсутствии совпадений он отключается и помечается
            // классом ``is-disabled``. ``<label>`` не содержит
            // атрибута ``for`` — связь с ``<input>`` обеспечивается
            // вложенностью.
            var navAllDisabledAttr = hasMatches ? "" : " disabled";
            var filterLabelClass = hasMatches
                ? "doc-checkbox-label"
                : "doc-checkbox-label is-disabled";

            var panel = document.createElement("div");
            panel.className = "tab-panel";
            panel.dataset.panelId = tabId;
            panel.style.display = "none";
            panel.innerHTML =
                '<div class="card">' +
                '  <div class="card__title">Документ: ' +
                escapeHtml(fileName) +
                "</div>" +
                '  <div id="doc-view-switch-' + docId + '"></div>' +
                '  <div class="doc-render-controls">' +
                '    <label class="doc-checkbox-label" ' +
                '           id="doc-transform-label-' + docId + '">' +
                '      <input type="checkbox" id="doc-transform-' + docId + '" disabled>' +
                '      Ротация координат' +
                '    </label>' +
                '  </div>' +
                '  <div id="doc-meta-' + docId + '"></div>' +
                '  <div class="doc-nav-controls">' +
                '    <div id="doc-nav-' + docId + '" class="doc-nav"></div>' +
                '    <label class="' + filterLabelClass + '">' +
                '      <input type="checkbox" id="doc-nav-all-' + docId + '"' +
                '             checked' + navAllDisabledAttr + '>' +
                '      Навигация по всем страницам' +
                '    </label>' +
                '  </div>' +
                '  <div id="doc-text-' + docId + '" class="page-text">' +
                "Загрузка…</div>" +
                "</div>";

            var container = document.getElementById("document-panels-container");
            if (container) {
                container.appendChild(panel);
            }

            // Создать per-tab renderer и view mode.
            var renderer = window.createPageRenderer(docId);
            var viewMode = window.ViewModeManager.getDefault();

            AppState.openDocuments.push({
                docId: docId,
                fileName: fileName,
                page: pageNumber,
                options: options || null,
                renderer: renderer,
                viewMode: viewMode,
                _abortController: new AbortController(),
                navAllPages: true,
                pageCount: undefined,
            });

            // Установить переключатель режимов.
            var switchContainer = document.getElementById("doc-view-switch-" + docId);
            if (switchContainer) {
                window.ViewModeManager.installSwitcher(switchContainer, docId);
            }

            // Привязать обработчик изменения чекбокса
            // «Навигация по всем страницам». При переключении
            // обновляется флаг navAllPages в записи вкладки и
            // перерисовывается только пагинация (без перезагрузки
            // содержимого страницы).
            var navAllCheckbox = document.getElementById("doc-nav-all-" + docId);
            if (navAllCheckbox) {
                navAllCheckbox.addEventListener("change", function () {
                    window.DDSApp.updateDocumentRecord(docId, {
                        navAllPages: this.checked,
                    });
                    DocumentLoader.refreshNavigation(docId);
                });
            }

            // Привязать обработчик изменения чекбокса
            // «Ротация координат». При переключении:
            // 1. Сохраняется per-page флаг трансформации в renderer
            //    (``_transformByPage[page]``).
            // 2. Пересчитывается подсветка для текущей страницы
            //    через публичный ``renderer.refreshHighlights`` —
            //    без перезагрузки PNG-рендера. Публичный метод
            //    делегирует в приватный ``_fetchAndApplyHighlights``
            //    с ``signal = undefined`` (пересчёт подсветки не
            //    должен прерывать активный PNG-рендер).
            //
            // Если для страницы нет терминов, запрос не отправляется:
            // чекбокс в этом случае отключён (см. логику
            // ``_updateTransformCheckboxState`` в ``document.js``).
            var transformCheckbox = document.getElementById("doc-transform-" + docId);
            if (transformCheckbox) {
                transformCheckbox.addEventListener("change", function () {
                    var recTransform = window.DDSApp.getDocumentRecord(docId);
                    if (!recTransform || !recTransform.renderer) return;

                    var page = recTransform.page;
                    recTransform.renderer.setTransformForPage(page, this.checked);

                    var terms = (recTransform.options &&
                        recTransform.options.termsByPage &&
                        recTransform.options.termsByPage[page]) || [];
                    if (terms.length === 0) return;

                    // refreshHighlights — публичный метод renderer'а.
                    // Прямой вызов приватного _fetchAndApplyHighlights
                    // запрещён — нарушает инкапсуляцию document.js.
                    recTransform.renderer.refreshHighlights(page, terms);
                });
            }

            this.switchTab(tabId);
            DocumentLoader.loadDocument(docId, fileName, pageNumber, options, renderer);
        },

        /**
         * Закрывает вкладку документа.
         *
         * Удаляет tab и panel из DOM, вызывает ``renderer.cleanup()``
         * для освобождения blob URL и pan-обработчиков, удаляет
         * запись из ``AppState.openDocuments``. При закрытии
         * активной вкладки переключается на вкладку поиска.
         *
         * Перед ``cleanup()`` вызывается ``abort()`` активного
         * ``AbortController`` (Фаза 7, ADR-007): pending fetch
         * вкладки прерываются. Это предотвращает обработку
         * устаревших ответов в закрытой вкладке (например,
         * обновление несуществующих DOM-элементов в ``.then``
         * обработчиках).
         *
         * @param {string} tabId — идентификатор вкладки (``doc-{docId}``).
         */
        closeDocumentTab: function (tabId) {
            var tab = this.getTabElement(tabId);
            if (tab) {
                tab.remove();
            }
            var panel = this.getPanelElement(tabId);
            if (panel) {
                panel.remove();
            }

            var docId = tabId.replace("doc-", "");
            var rec = window.DDSApp.getDocumentRecord(docId);
            if (rec) {
                if (rec._abortController) {
                    rec._abortController.abort();
                }
                if (rec.renderer) {
                    rec.renderer.cleanup();
                }
                for (var i = 0; i < AppState.openDocuments.length; i++) {
                    if (AppState.openDocuments[i].docId === docId) {
                        AppState.openDocuments.splice(i, 1);
                        break;
                    }
                }
            }

            if (AppState.activeTabId === tabId) {
                this.switchTab("search");
            } else {
                // Пересчитать активное состояние иконки без смены вкладки
                this._updateActivityBarState(AppState.activeTabId);
            }
        },

        getTabElement: function (tabId) {
            return document.querySelector('[data-tab-id="' + tabId + '"]');
        },

        getPanelElement: function (tabId) {
            return document.querySelector('[data-panel-id="' + tabId + '"]');
        }
    };

    /* ==============================================================
       5. Обновление статуса сканирования
       ============================================================== */

    function updateScanStatusHint(forceRefresh) {
        var statusEl = document.getElementById("statusbar-scan");
        if (statusEl && forceRefresh) {
            statusEl.textContent = "Сканирование: обновление";
        }

        var url = "/api/index/status";
        if (forceRefresh) {
            url += "?refresh=true";
        }
        fetch(url)
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Ошибка получения состояния");
                }
                return response.json();
            })
            .then(function (data) {
                var statusEl = document.getElementById("statusbar-scan");
                if (!statusEl) return;
                var state = data.state;
                var text = "Сканирование: —";
                switch (state) {
                    case "fully_indexed":
                        text = "Сканирование не требуется";
                        break;
                    case "needs_update":
                    case "partially_indexed":
                    case "empty":
                        text = "Требуется сканирование";
                        break;
                    case "no_source_files":
                        text = "Нет файлов для сканирования";
                        break;
                    default:
                        text = "Сканирование: —";
                }
                statusEl.textContent = text;
            })
            .catch(function () {
                var statusEl = document.getElementById("statusbar-scan");
                if (statusEl) {
                    statusEl.textContent = "Сканирование: —";
                }
            });
    }

    /* ==============================================================
       6. Обновление статуса поиска (расширенная версия)
       ============================================================== */

    /**
     * Обновляет информацию о поиске в статус-баре.
     *
     * Поле ``total`` — количество уникальных документов
     * (серверная группировка результатов поиска). Подпись в
     * статус-баре — «Документов: N».
     *
     * @param {string} query — фактически использованный поисковый запрос.
     * @param {number} total — общее количество документов.
     * @param {Object} [appliedFilters] — объект с фактически применёнными
     *   фильтрами: { object_code, discipline_code, document_type_code,
     *   unmatched_only }.
     *   Если не передан, фильтры считаются пустыми.
     */
    function updateSearchStatus(query, total, appliedFilters) {
        var queryEl = document.getElementById("statusbar-query");
        var objectEl = document.getElementById("statusbar-object");
        var disciplineEl = document.getElementById("statusbar-discipline");
        var typeEl = document.getElementById("statusbar-type");
        var unmatchedEl = document.getElementById("statusbar-unmatched");
        var countEl = document.getElementById("statusbar-results-count");

        if (queryEl) {
            queryEl.textContent = query ? 'Запрос: "' + query + '"' : "Запрос: —";
        }

        appliedFilters = appliedFilters || {};
        if (objectEl) {
            objectEl.textContent = appliedFilters.object_code
                ? 'Объект: "' + appliedFilters.object_code + '"'
                : "Объект: —";
        }
        if (disciplineEl) {
            disciplineEl.textContent = appliedFilters.discipline_code
                ? 'Дисциплина: "' + appliedFilters.discipline_code + '"'
                : "Дисциплина: —";
        }
        if (typeEl) {
            typeEl.textContent = appliedFilters.document_type_code
                ? 'Тип: "' + appliedFilters.document_type_code + '"'
                : "Тип: —";
        }
        if (unmatchedEl) {
            unmatchedEl.textContent = appliedFilters.unmatched_only
                ? "Несоответств.: да"
                : "Несоответств.: —";
        }

        if (countEl) {
            countEl.textContent = typeof total === "number"
                ? "Документов: " + total
                : "Документов: —";
        }
    }

    /* ==============================================================
       7. Мониторинг сканирования через SSE
       ============================================================== */

    var _scanMonitoringStarted = false;

    function startScanMonitoring() {
        if (typeof SSEModule === "undefined") {
            return;
        }
        if (!_scanMonitoringStarted) {
            _scanMonitoringStarted = true;

            SSEModule.onProgress(function (data) {
                var statusbarScan = document.getElementById("statusbar-scan");
                if (!statusbarScan) return;
                var percent = data.progress_percent || 0;
                statusbarScan.innerHTML =
                    '<span class="status-bar__dot status-bar__dot--running"></span> ' +
                    "Сканирование: " + percent.toFixed(1) + "%";
            });

            SSEModule.onComplete(function (data) {
                var statusbarScan = document.getElementById("statusbar-scan");
                if (statusbarScan) {
                    var statusText = data.status || "завершено";
                    var displayStatus = statusText;
                    if (statusText === "completed") displayStatus = "завершено";
                    else if (statusText === "interrupted") displayStatus = "прервано";
                    else if (statusText === "error") displayStatus = "ошибка";
                    statusbarScan.textContent = "Сканирование: " + displayStatus;
                    setTimeout(function () {
                        updateScanStatusHint(true);
                    }, 5000);
                }
                if (typeof UIStateManager !== "undefined") {
                    UIStateManager.update({ scan_status: data.status });
                }
            });

            SSEModule.onSecondaryStarted(function (data) {
                var statusbarScan = document.getElementById("statusbar-scan");
                if (statusbarScan) {
                    statusbarScan.innerHTML =
                        '<span class="status-bar__dot status-bar__dot--running"></span> ' +
                        "Сканирование: вторичная обработка…";
                }
            });

            SSEModule.onSecondaryProgress(function (data) {
                var statusbarScan = document.getElementById("statusbar-scan");
                if (!statusbarScan) return;
                var percent = data.progress_percent || 0;
                var stageText = data.stage === "modules" && data.module_name
                    ? " (" + data.module_name + ")"
                    : "";
                statusbarScan.innerHTML =
                    '<span class="status-bar__dot status-bar__dot--running"></span> ' +
                    "Сканирование: вторичная обработка " +
                    percent.toFixed(1) + "%" + stageText;
            });

            SSEModule.onSecondaryCompleted(function (data) {
                var statusbarScan = document.getElementById("statusbar-scan");
                if (statusbarScan) {
                    var statusText = data.status || "завершено";
                    var displayStatus = statusText;
                    if (statusText === "completed") displayStatus = "завершено";
                    else if (statusText === "error") displayStatus = "ошибка";
                    statusbarScan.textContent = "Сканирование: " + displayStatus + " (вторичная обработка завершена)";
                    setTimeout(function () {
                        updateScanStatusHint(true);
                    }, 5000);
                }
                if (typeof UIStateManager !== "undefined") {
                    UIStateManager.update({ scan_status: "idle" });
                }
            });
        }
        SSEModule.connect();
    }

    /* ==============================================================
       8. Условная панель прокрутки
       ============================================================== */

    function setupConditionalScrollbar() {
        var content = document.querySelector(".editor-content");
        if (!content) return;

        var updateScrollState = function () {
            if (content.scrollHeight > content.clientHeight) {
                content.classList.add("has-scroll");
                content.classList.remove("no-scroll");
            } else {
                content.classList.add("no-scroll");
                content.classList.remove("has-scroll");
            }
        };

        updateScrollState();

        var mutationObserver = new MutationObserver(function () {
            clearTimeout(mutationObserver._timeout);
            mutationObserver._timeout = setTimeout(updateScrollState, 100);
        });
        mutationObserver.observe(content, {
            childList: true,
            subtree: true,
            characterData: true
        });

        var resizeObserver = new ResizeObserver(function () {
            clearTimeout(resizeObserver._timeout);
            resizeObserver._timeout = setTimeout(updateScrollState, 100);
        });
        resizeObserver.observe(content);

        window._scrollObservers = {
            mutation: mutationObserver,
            resize: resizeObserver
        };
    }

    /* ==============================================================
       9. Инициализация панели статуса
       ============================================================== */

    function initStatusBar() {
        fetch("/api/scan/status")
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Ошибка получения статуса сканирования");
                }
                return response.json();
            })
            .then(function (data) {
                if (typeof UIStateManager !== "undefined") {
                    UIStateManager.update({ scan_status: data.status });
                }
                if (data.status === "running") {
                    startScanMonitoring();
                } else {
                    updateScanStatusHint(true);
                }
            })
            .catch(function () {
                updateScanStatusHint(false);
            });
    }

    /* ==============================================================
       10. Управление активным состоянием иконки поиска
       ============================================================== */

    /**
     * Устанавливает активное состояние иконки поиска Activity Bar.
     * Делегирует вызов в единый приватный метод
     * ``TabManager._applySearchIconState``, который является
     * единственной точкой изменения класса ``active``.
     *
     * Метод предназначен для внешних модулей (например,
     * ``settings.js``), которым требуется временно снять
     * активное состояние иконки поиска при открытии панели
     * настроек и восстановить его при закрытии.
     *
     * @param {boolean} isActive — если ``true``, класс ``active``
     *   добавляется; если ``false`` — снимается.
     */
    function setSearchIconActive(isActive) {
        TabManager._applySearchIconState(isActive);
    }

    /**
     * Возвращает текущее активное состояние иконки поиска.
     *
     * @returns {boolean} — ``true``, если иконка активна.
     */
    function getSearchIconActive() {
        var searchIcon = document.getElementById("activity-search");
        return searchIcon ? searchIcon.classList.contains("active") : false;
    }

    /* ==============================================================
       11. Публичный фасад window.DDSApp
       ============================================================== */

    /**
     * Публичный фасад для внешних модулей.
     *
     * Инкапсулирует IIFE-скоуп ``AppState`` и ``TabManager``,
     * предоставляя методы для:
     *
     * - Обновления статус-бара (``updateSearchStatus``).
     * - Управления SSE-мониторингом (``startScanMonitoring``).
     * - Управления иконкой поиска
     *   (``setSearchIconActive`` / ``getSearchIconActive``).
     * - Открытия вкладки документа (``openDocumentTab``).
     * - Доступа к per-tab состоянию
     *   (``getDocumentRecord`` / ``updateDocumentRecord`` /
     *   ``getActiveDocumentRecord``).
     * - Гарантированного наличия ``renderer`` у вкладки
     *   (``ensureRenderer``).
     * - Доступа к ``buildHash`` для cache-busting тем (Фаза 7).
     * - Управления ``AbortController`` вкладки
     *   (``getAbortController`` / ``resetAbortController``).
     *
     * Модуль ``document.js`` использует эти методы для доступа
     * к состоянию вкладки, не нарушая инкапсуляцию ``AppState``.
     */
    window.DDSApp = {
        /**
         * Короткий идентификатор сборки для cache-busting
         * статических ресурсов (Фаза 7, ADR-007).
         *
         * Начальное значение — пустая строка. Обновляется в
         * :func:`AppInit` из ``#main-init-data.dataset.buildHash``.
         * Используется ``ThemeManager.applyTheme`` для добавления
         * ``?v=<buildHash>`` к ``href`` активной темы.
         *
         * @type {string}
         */
        buildHash: _buildHash,

        updateSearchStatus: updateSearchStatus,
        startScanMonitoring: startScanMonitoring,
        setSearchIconActive: setSearchIconActive,
        getSearchIconActive: getSearchIconActive,

        /**
         * Открывает вкладку документа с опциональным набором
         * параметров (в частности, картой ``termsByPage`` для
         * подсветки).
         *
         * @param {string} docId — идентификатор документа.
         * @param {string} fileName — отображаемое имя файла.
         * @param {number} pageNumber — номер страницы (0-based).
         * @param {Object} [options] — дополнительные параметры
         *   (например, ``{termsByPage: {...}}``).
         */
        openDocumentTab: function (docId, fileName, pageNumber, options) {
            TabManager.openDocumentTab(docId, fileName, pageNumber, options);
        },

        /**
         * Возвращает запись о документе из ``AppState.openDocuments``
         * или ``null``, если документ не открыт.
         *
         * @param {string} docId — идентификатор документа.
         * @returns {Object|null}
         */
        getDocumentRecord: function (docId) {
            for (var i = 0; i < AppState.openDocuments.length; i++) {
                if (AppState.openDocuments[i].docId === docId) {
                    return AppState.openDocuments[i];
                }
            }
            return null;
        },

        /**
         * Обновляет поля записи о документе.
         *
         * @param {string} docId — идентификатор документа.
         * @param {Object} updates — объект с полями для обновления.
         * @returns {boolean} — ``true``, если запись найдена и обновлена.
         */
        updateDocumentRecord: function (docId, updates) {
            var rec = this.getDocumentRecord(docId);
            if (!rec) return false;
            for (var key in updates) {
                if (updates.hasOwnProperty(key)) {
                    rec[key] = updates[key];
                }
            }
            return true;
        },

        /**
         * Возвращает запись об активной вкладке документа
         * или ``null``, если активна вкладка поиска.
         *
         * @returns {Object|null}
         */
        getActiveDocumentRecord: function () {
            var tabId = AppState.activeTabId;
            if (!tabId || tabId.indexOf("doc-") !== 0) return null;
            return this.getDocumentRecord(tabId.substring(4));
        },

        /**
         * Гарантирует наличие ``renderer`` в записи о документе.
         * Если ``renderer`` отсутствует (например, запись создана
         * до правки или обходным путём), создаётся через
         * ``createPageRenderer(docId)``.
         *
         * @param {string} docId — идентификатор документа.
         * @returns {Object|null} — renderer или ``null``, если
         *   запись не найдена.
         */
        ensureRenderer: function (docId) {
            var rec = this.getDocumentRecord(docId);
            if (!rec) return null;
            if (!rec.renderer) {
                rec.renderer = window.createPageRenderer(docId);
            }
            return rec.renderer;
        },

        /**
         * Возвращает ``AbortController`` вкладки (Фаза 7, ADR-007).
         *
         * Если контроллер отсутствует в записи (например, запись
         * создана до правки или обходным путём) — создаётся новый.
         * Это обеспечивает обратную совместимость с кодом, который
         * ожидает наличие ``_abortController``.
         *
         * Используется ``document.js`` для передачи ``signal`` в
         * ``fetch`` без сброса контроллера (например,
         * ``refreshHighlights`` — пересчёт подсветки без
         * прерывания активного рендера).
         *
         * @param {string} docId — идентификатор документа.
         * @returns {AbortController|null} — контроллер вкладки или
         *   ``null``, если запись не найдена.
         */
        getAbortController: function (docId) {
            var rec = this.getDocumentRecord(docId);
            if (!rec) return null;
            if (!rec._abortController) {
                rec._abortController = new AbortController();
            }
            return rec._abortController;
        },

        /**
         * Сбрасывает ``AbortController`` вкладки (Фаза 7, ADR-007).
         *
         * Прерывает все pending fetch, связанные с текущим
         * контроллером (через ``abort()``), и создаёт свежий
         * контроллер. Возвращает новый контроллер.
         *
         * Вызывается в ``DocumentLoader.loadDocument`` при смене
         * страницы/режима: устаревшие fetch прерываются, а новые
         * получают ``signal`` от свежего контроллера.
         *
         * Если запись не найдена — возвращает ``null`` без побочных
         * эффектов.
         *
         * @param {string} docId — идентификатор документа.
         * @returns {AbortController|null} — свежий контроллер или
         *   ``null``, если запись не найдена.
         */
        resetAbortController: function (docId) {
            var rec = this.getDocumentRecord(docId);
            if (!rec) return null;
            if (rec._abortController) {
                rec._abortController.abort();
            }
            rec._abortController = new AbortController();
            return rec._abortController;
        },
    };

    /* ==============================================================
       12. Инициализация приложения
       ============================================================== */

    function AppInit() {
        // Шаг 1: Чтение начальных данных из DOM.
        var initData = document.getElementById("main-init-data");
        var scanStatus = initData ? initData.dataset.scanStatus : "idle";
        var username = initData ? initData.dataset.username : "";
        var userRole = initData ? initData.dataset.role : "user";

        // Шаг 1a: Чтение build_hash для cache-busting статических
        // ресурсов (Фаза 7, ADR-007).
        //
        // window.DDSApp создан в IIFE **до** вызова AppInit,
        // поэтому поле buildHash в объекте инициализировано пустой
        // строкой. Здесь обновляем его актуальным значением из DOM.
        //
        // Если атрибут data-build-hash отсутствует или пуст —
        // оставляем пустую строку: ThemeManager.applyTheme
        // в этом случае не добавляет ?v= к href темы.
        _buildHash = initData ? (initData.dataset.buildHash || "") : "";
        if (window.DDSApp) {
            window.DDSApp.buildHash = _buildHash;
        }

        // Шаг 2: Инициализация UIStateManager.
        if (typeof UIStateManager !== "undefined") {
            UIStateManager.init({
                scan_status: scanStatus,
                index_refresh: "idle",
                user_role: userRole
            });
        }

        // Шаг 3: Инициализация SettingsModule.
        if (typeof SettingsModule !== "undefined") {
            SettingsModule.init({
                username: username,
                role: userRole
            });
        }

        // Шаг 4: Инициализация FilterCoordinator (загрузка справочников).
        if (typeof FilterCoordinator !== "undefined") {
            FilterCoordinator.init().catch(function (error) {
                console.error("Не удалось инициализировать фильтры:", error);
            });
        }

        // Шаг 5: Обработчик клика на Tab Bar.
        var tabBar = document.querySelector(".tab-bar");
        if (tabBar) {
            tabBar.addEventListener("click", function (e) {
                var closeBtn = e.target.closest("[data-close-tab]");
                if (closeBtn) {
                    e.stopPropagation();
                    TabManager.closeDocumentTab(closeBtn.dataset.closeTab);
                    return;
                }
                var tab = e.target.closest(".tab");
                if (tab && tab.dataset.tabId) {
                    TabManager.switchTab(tab.dataset.tabId);
                }
            });
        }

        // Шаг 6: Инициализация панели статуса.
        initStatusBar();

        // Шаг 7: Настройка условной панели прокрутки.
        setupConditionalScrollbar();

        // Шаг 8: Начальное переключение на вкладку «Поиск».
        TabManager.switchTab("search");

        // Шаг 9: Единый обработчик Ctrl+0 — сброс zoom активной
        // вкладки документа. Регистрируется один раз в AppInit,
        // чтобы избежать накопления listener'ов при создании
        // новых вкладок.
        //
        // Вызывается `renderer.resetZoom()`, который учитывает
        // флаг `_userZoomed`: если пользователь ещё не менял zoom
        // вручную — возвращает fit-width; иначе — устанавливает
        // zoom 1.0.
        document.addEventListener("keydown", function (e) {
            if (!(e.ctrlKey || e.metaKey) || e.key !== "0") return;
            var rec = window.DDSApp.getActiveDocumentRecord();
            if (!rec) return;
            e.preventDefault();
            if (rec.renderer && typeof rec.renderer.resetZoom === "function") {
                rec.renderer.resetZoom();
            }
        });
    }

    /* ==============================================================
       13. Запуск инициализации
       ============================================================== */

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", AppInit);
    } else {
        AppInit();
    }
})();
