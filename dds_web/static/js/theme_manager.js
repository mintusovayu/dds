/**
 * Модуль управления темами оформления DDS.
 *
 * Обеспечивает динамическую загрузку списка доступных тем,
 * переключение темы без перезагрузки страницы и сохранение
 * выбора пользователя в localStorage.
 *
 * Cache-busting тем (Фаза 7, ADR-007):
 *
 * +----------------------------------+----------------------------------+
 * | Изменение                        | Описание                         |
 * +==================================+==================================+
 * | ``applyTheme`` добавляет         | К ``href`` активной темы         |
 * | ``?v=<buildHash>``               | добавляется query-параметр       |
 * |                                  | ``?v=<buildHash>``, если         |
 * |                                  | ``window.DDSApp.buildHash`` —    |
 * |                                  | непустая строка.                 |
 * +----------------------------------+----------------------------------+
 * | Fallback без ``?v=``             | Если ``window.DDSApp``           |
 * |                                  | недоступен (ранняя инициализация, |
 * |                                  | тесты), применяется ``href``     |
 * |                                  | без параметра.                   |
 * +----------------------------------+----------------------------------+
 * | Источник ``buildHash``           | Значение читается из              |
 * |                                  | ``window.DDSApp.buildHash``,     |
 * |                                  | которое инициализируется в       |
 * |                                  | ``AppInit`` из                   |
 * |                                  | ``#main-init-data.dataset.buildHash``.|
 * +----------------------------------+----------------------------------+
 *
 * Примечание:
 *   Inline-скрипт в ``base.html`` (ранняя установка темы до
 *   загрузки JS) **не добавляет** ``?v=``, поскольку
 *   ``window.DDSApp`` на этом этапе ещё не создан. Cache-busting
 *   вступает в силу после ``AppInit`` — то есть при смене темы
 *   пользователем и при вызове ``initTheme``.
 *
 * Архитектура модуля:
 *
 * +----------------------------------+----------------------------------+
 * | Компонент                        | Ответственность                  |
 * +==================================+==================================+
 * | ``ThemeManager.loadThemes()``    | Загрузка списка тем с сервера,   |
 * |                                  | проверка сохранённой темы.       |
 * +----------------------------------+----------------------------------+
 * | ``ThemeManager.applyTheme()``    | Применение выбранной темы.       |
 * +----------------------------------+----------------------------------+
 * | ``ThemeManager.initTheme()``     | Инициализация темы при старте    |
 * |                                  | (вызывается из inline-скрипта).  |
 * +----------------------------------+----------------------------------+
 * | ``ThemeManager.getCurrentTheme()``| Получение текущей темы.          |
 * +----------------------------------+----------------------------------+
 *
 * Принципы:
 * - Модуль не содержит бизнес-логики, только управление DOM
 *   и localStorage.
 * - Данные о темах загружаются через REST API ``/api/themes``.
 * - Модуль изолирован в IIFE и экспортирует глобальный объект
 *   ``ThemeManager``.
 * - Тема по умолчанию — ``dark``.
 * - Автоматическая инициализация темы при загрузке скрипта
 *   **не выполняется**: она должна производиться либо через
 *   inline-скрипт в ``base.html``, либо явным вызовом
 *   ``ThemeManager.initTheme()`` из другого модуля.
 *
 * Зависимости:
 * - ``base.html`` — содержит элемент ``<link id="theme-stylesheet">``.
 * - ``app.js`` — предоставляет ``window.DDSApp.buildHash``
 *   для cache-busting (Фаза 7).
 * - Глобальный объект ``localStorage`` (может быть недоступен
 *   в некоторых окружениях, поэтому все операции обёрнуты
 *   в try/catch).
 *
 * Использование:
 * .. code-block:: javascript
 *
 *    // Загрузка списка тем
 *    ThemeManager.loadThemes().then(function(themes) { ... });
 *
 *    // Применение темы
 *    ThemeManager.applyTheme("purple");
 *
 *    // Инициализация при загрузке страницы
 *    ThemeManager.initTheme();
 *
 *    // Получение текущей темы
 *    var current = ThemeManager.getCurrentTheme();
 */
(function () {
    "use strict";

    /* ==============================================================
       1. Константы
       ============================================================== */

    var THEME_STORAGE_KEY = "dds-theme";
    var DEFAULT_THEME = "dark";
    var STYLESHEET_ELEMENT_ID = "theme-stylesheet";
    var THEMES_API_URL = "/api/themes";
    var THEME_NAME_PATTERN = /^[a-zA-Z0-9-]+$/;

    /* ==============================================================
       2. Внутреннее состояние
       ============================================================== */

    var _cachedThemes = null;
    var _currentTheme = null;

    /* ==============================================================
       3. Вспомогательные функции
       ============================================================== */

    /**
     * Безопасно читает значение из localStorage.
     *
     * @param {string} key — ключ.
     * @returns {string|null} — значение или null, если ключ отсутствует
     *   или localStorage недоступен.
     */
    function _storageGet(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (e) {
            return null;
        }
    }

    /**
     * Безопасно сохраняет значение в localStorage.
     *
     * @param {string} key — ключ.
     * @param {string} value — значение.
     */
    function _storageSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (e) {
            // localStorage может быть недоступен (режим инкогнито и т.п.)
        }
    }

    /**
     * Безопасно удаляет значение из localStorage.
     *
     * @param {string} key — ключ.
     */
    function _storageRemove(key) {
        try {
            window.localStorage.removeItem(key);
        } catch (e) {
            // Игнорируем ошибки доступа
        }
    }

    /**
     * Возвращает элемент <link> для темы.
     *
     * @returns {HTMLElement|null} — элемент или null, если не найден.
     */
    function _getStylesheetLink() {
        return document.getElementById(STYLESHEET_ELEMENT_ID);
    }

    /**
     * Проверяет, является ли имя темы безопасным.
     *
     * @param {string} name — имя темы.
     * @returns {boolean} — true, если имя состоит только из букв,
     *   цифр и дефисов.
     */
    function _isValidThemeName(name) {
        return typeof name === "string" && THEME_NAME_PATTERN.test(name);
    }

    /**
     * Нормализует имя темы: если имя невалидно, возвращает тему
     * по умолчанию.
     *
     * @param {string} name — предполагаемое имя темы.
     * @returns {string} — безопасное имя темы.
     */
    function _normalizeThemeName(name) {
        return _isValidThemeName(name) ? name : DEFAULT_THEME;
    }

    /**
     * Возвращает текущий build_hash из фасада ``window.DDSApp``.
     *
     * Используется для cache-busting статических ресурсов
     * (Фаза 7, ADR-007). Значение читается в момент вызова,
     * а не кэшируется — это гарантирует, что после ``AppInit``
     * (когда ``window.DDSApp.buildHash`` обновится из DOM)
     * ``applyTheme`` сразу использует актуальное значение.
     *
     * Возвращает пустую строку, если:
     *   - ``window.DDSApp`` ещё не создан (ранняя инициализация);
     *   - ``window.DDSApp.buildHash`` отсутствует или не является
     *     строкой;
     *   - значение равно пустой строке.
     *
     * @returns {string} — build_hash или пустая строка.
     * @private
     */
    function _getBuildHash() {
        if (typeof window.DDSApp === "undefined"
                || typeof window.DDSApp.buildHash !== "string") {
            return "";
        }
        return window.DDSApp.buildHash;
    }

    /* ==============================================================
       4. Публичный API
       ============================================================== */

    var ThemeManager = {

        /**
         * Загружает список доступных тем с сервера.
         *
         * Результат кэшируется в ``_cachedThemes``. При получении
         * списка проверяется сохранённая в localStorage тема: если
         * она отсутствует в списке или невалидна, применяется тема
         * по умолчанию, а устаревшее значение удаляется.
         *
         * @returns {Promise<string[]>} — Promise со списком имён тем.
         */
        loadThemes: function () {
            var self = this;

            if (_cachedThemes !== null) {
                return Promise.resolve(_cachedThemes.slice());
            }

            return fetch(THEMES_API_URL)
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Ошибка загрузки списка тем");
                    }
                    return response.json();
                })
                .then(function (themes) {
                    // Фильтруем только валидные имена
                    var validThemes = (Array.isArray(themes) ? themes : [])
                        .filter(_isValidThemeName);
                    if (validThemes.length === 0) {
                        validThemes = [DEFAULT_THEME];
                    }

                    // Проверяем сохранённую тему
                    var saved = _storageGet(THEME_STORAGE_KEY);
                    if (saved !== null) {
                        if (!_isValidThemeName(saved) || validThemes.indexOf(saved) === -1) {
                            // Сохранённая тема недоступна: сбрасываем на дефолт
                            _storageRemove(THEME_STORAGE_KEY);
                            self.applyTheme(DEFAULT_THEME);
                        }
                    }

                    _cachedThemes = validThemes;
                    return validThemes.slice();
                })
                .catch(function () {
                    // В случае ошибки возвращаем тему по умолчанию
                    _cachedThemes = [DEFAULT_THEME];
                    return [DEFAULT_THEME];
                });
        },

        /**
         * Применяет тему, меняя href у элемента ``#theme-stylesheet``.
         *
         * Имя темы проверяется на безопасность. Если имя невалидно,
         * применяется тема по умолчанию.
         *
         * Cache-busting (Фаза 7, ADR-007):
         *   Если ``window.DDSApp.buildHash`` содержит непустую
         *   строку, к ``href`` добавляется query-параметр
         *   ``?v=<buildHash>``. Это гарантирует, что после
         *   пересборки статики браузер загрузит свежую версию
         *   CSS-файла темы, а не возьмёт устаревшую из кеша.
         *
         *   Значение ``buildHash`` читается в момент вызова
         *   (см. ``_getBuildHash``), поэтому после ``AppInit``
         *   (когда ``window.DDSApp.buildHash`` обновится из
         *   DOM-атрибута ``data-build-hash``) метод автоматически
         *   использует актуальное значение без дополнительной
         *   синхронизации.
         *
         *   Если ``window.DDSApp`` недоступен (ранняя инициализация
         *   до ``AppInit``, unit-тесты без фасада), ``applyTheme``
         *   работает без ``?v=`` — обратная совместимость сохранена.
         *
         * @param {string} themeName — имя темы (например, "dark", "purple").
         */
        applyTheme: function (themeName) {
            var safeName = _normalizeThemeName(themeName);
            var link = _getStylesheetLink();
            if (link) {
                var hash = _getBuildHash();
                var suffix = hash ? ("?v=" + hash) : "";
                link.href = "/static/css/theme-" + safeName + ".css" + suffix;
            }
            _currentTheme = safeName;
            _storageSet(THEME_STORAGE_KEY, safeName);
        },

        /**
         * Инициализирует тему при старте страницы.
         *
         * Читает сохранённое значение из localStorage и применяет его.
         * Если сохранённого значения нет или оно невалидно, используется
         * тема по умолчанию.
         *
         * Этот метод должен вызываться явно (например, из inline-скрипта
         * в base.html или из app.js).
         *
         * Примечание:
         *   Cache-busting (``?v=<buildHash>``) применяется к ``href``
         *   только если ``window.DDSApp.buildHash`` уже заполнен.
         *   В типичном сценарии ``initTheme`` вызывается до ``AppInit``
         *   (из inline-скрипта), поэтому ``href`` остаётся без
         *   ``?v=`` — это осознанное решение (см. module docstring).
         *   При повторном вызове после ``AppInit`` параметр добавляется.
         */
        initTheme: function () {
            var saved = _storageGet(THEME_STORAGE_KEY);
            var initialTheme = _normalizeThemeName(saved || DEFAULT_THEME);
            this.applyTheme(initialTheme);
        },

        /**
         * Возвращает имя текущей активной темы.
         *
         * @returns {string} — имя темы.
         */
        getCurrentTheme: function () {
            if (_currentTheme) {
                return _currentTheme;
            }
            var saved = _storageGet(THEME_STORAGE_KEY);
            return _normalizeThemeName(saved || DEFAULT_THEME);
        }
    };

    /* ==============================================================
       5. Экспорт
       ============================================================== */

    if (typeof window !== "undefined") {
        window.ThemeManager = ThemeManager;
        // Автоматическая инициализация НЕ выполняется:
        // она должна вызываться из base.html или app.js.
    }
})();
