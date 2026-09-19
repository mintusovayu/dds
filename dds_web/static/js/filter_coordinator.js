/**
 * Модуль координации фильтров поиска.
 *
 * Управляет состоянием фильтров, основанных на парсинге имён файлов.
 * Каждая категория (объект, дисциплина, тип документа) представлена
 * одним полем ввода с автодополнением. Пользователь может ввести
 * код, русский псевдоним или английский псевдоним; выпадающий список
 * динамически показывает соответствующие варианты.
 *
 * Фильтры категорий объединяются по логическому «И». Чекбокс
 * «Только несоответствующие» является специальным фильтром,
 * который может применяться только при наличии хотя бы одного
 * активного обычного фильтра (кода объекта, дисциплины или типа
 * документа). Чекбоксы «Только фильтр» и «Только содержание»
 * взаимоисключающие.
 *
 * При изменении текста в поле фильтра соответствующий применённый
 * фильтр автоматически сбрасывается. Если после этого не осталось
 * активных обычных фильтров, чекбокс «Только несоответствующие»
 * снимается и блокируется. Также он блокируется при активации
 * режима «Только содержание».
 *
 * Модуль загружает справочники через REST API ``GET /api/filters/metadata``.
 *
 * Архитектурные улучшения:
 * - Единый метод ``hasActiveOrdinaryFilters()`` для проверки наличия
 *   обычных фильтров.
 * - Централизованные методы установки состояний чекбоксов
 *   (``_setFilterOnly``, ``_setContentOnly``, ``_setUnmatchedOnly``),
 *   которые обеспечивают взаимные сбросы и обновление доступности.
 * - Чекбокс «Только несоответствующие» автоматически блокируется,
 *   если его использование невозможно.
 *
 * Принципы:
 * - Модуль не содержит бизнес-логики — только управление
 *   состоянием фильтров и взаимодействие с DOM.
 * - Данные справочников загружаются через REST API (инверсия
 *   зависимостей).
 * - Модуль изолирован в IIFE и экспортирует глобальный объект
 *   ``FilterCoordinator``.
 */
(function () {
    "use strict";

    /* ==============================================================
       1. Импорт внешних модулей
       ============================================================== */

    var DocumentUtils = window.DocumentUtils;

    /* ==============================================================
       2. Константы и идентификаторы элементов
       ============================================================== */

    var INPUT_IDS = {
        object: "filter-object",
        discipline: "filter-discipline",
        documentType: "filter-document-type"
    };

    var DROPDOWN_IDS = {
        object: "filter-object-dropdown",
        discipline: "filter-discipline-dropdown",
        documentType: "filter-document-type-dropdown"
    };

    var CHECKBOX_ID = "filter-unmatched-only";
    var RESET_BUTTON_ID = "btn-reset-filters";
    var FILTER_ONLY_CHECKBOX_ID = "search-filter-only";
    var CONTENT_ONLY_CHECKBOX_ID = "search-content-only";
    var API_ENDPOINT = "/api/filters/metadata";

    /* ==============================================================
       3. Состояние фильтров
       ============================================================== */

    var FilterState = {
        objectCode: null,
        disciplineCode: null,
        documentTypeCode: null,
        unmatchedOnly: false,
        searchFilterOnly: false,
        searchContentOnly: false,
        references: {
            objects: { codes: [], aliases_en: [], aliases_ru: [] },
            disciplines: { codes: [], aliases_en: [], aliases_ru: [] },
            documentTypes: { codes: [], aliases_en: [], aliases_ru: [] }
        },
        autocompleteComponents: {}
    };

    /* ==============================================================
       4. Взаимодействие с REST API
       ============================================================== */

    var FilterAPI = {
        /**
         * Загружает справочники кодов и псевдонимов.
         *
         * @returns {Promise<Object>} Promise с объектом, содержащим
         *   справочники для объектов, дисциплин и типов документов.
         *   Каждая категория имеет структуру:
         *   {
         *     codes: string[],
         *     aliases_en: Array<{alias: string, code: string}>,
         *     aliases_ru: Array<{alias: string, code: string}>
         *   }
         */
        fetchReferences: function () {
            return fetch(API_ENDPOINT)
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Ошибка загрузки справочников фильтров");
                    }
                    return response.json();
                });
        }
    };

    /* ==============================================================
       5. Управление фильтрами
       ============================================================== */

    var FilterCoordinator = {

        /**
         * Инициализирует модуль: загружает справочники, создаёт
         * компоненты автодополнения, привязывает обработчики.
         *
         * @returns {Promise<void>} Promise, который разрешается после
         *   завершения инициализации.
         */
        init: function () {
            var self = this;
            return FilterAPI.fetchReferences()
                .then(function (data) {
                    self._storeReferences(data);

                    if (window.DebugUtils && window.DEBUG_FILTERS) {
                        var totalItems = 0;
                        ["objects", "disciplines", "documentTypes"].forEach(function (cat) {
                            totalItems += data[cat].codes.length;
                            totalItems += data[cat].aliases_en.length;
                            totalItems += data[cat].aliases_ru.length;
                        });
                        window.DebugUtils.sendDebugEvent("references_loaded", {
                            count: totalItems
                        });
                    }

                    self._setupAutocomplete();
                    self._bindEvents();
                    self._applyState();
                })
                .catch(function (error) {
                    console.error("Не удалось инициализировать фильтры:", error);
                    self._bindEvents();
                });
        },

        /**
         * Возвращает текущие значения фильтров с учётом режимов.
         * Если активен режим «только содержание», все коды
         * возвращаются как null.
         *
         * @returns {Object} Объект с полями:
         *   ``object_code``, ``discipline_code``,
         *   ``document_type_code``, ``unmatched_only``.
         */
        getFilterValues: function () {
            if (FilterState.searchContentOnly) {
                return {
                    object_code: null,
                    discipline_code: null,
                    document_type_code: null,
                    unmatched_only: false
                };
            }
            return {
                object_code: FilterState.objectCode,
                discipline_code: FilterState.disciplineCode,
                document_type_code: FilterState.documentTypeCode,
                unmatched_only: FilterState.unmatchedOnly
            };
        },

        /**
         * Проверяет, есть ли активные обычные фильтры (коды).
         *
         * @returns {boolean} true, если хотя бы один из кодов задан.
         */
        hasActiveOrdinaryFilters: function () {
            return FilterState.objectCode !== null ||
                FilterState.disciplineCode !== null ||
                FilterState.documentTypeCode !== null;
        },

        /**
         * Устанавливает значение фильтра «только несоответствующие».
         * Синхронизирует состояние и DOM.
         *
         * @param {boolean} value — новое значение.
         */
        setUnmatchedOnly: function (value) {
            this._setUnmatchedOnly(value);
        },

        /**
         * Возвращает признак, активен ли режим «только фильтр».
         *
         * @returns {boolean}
         */
        isFilterOnlyMode: function () {
            return FilterState.searchFilterOnly;
        },

        /**
         * Возвращает признак, активен ли режим «только содержание».
         *
         * @returns {boolean}
         */
        isContentOnlyMode: function () {
            return FilterState.searchContentOnly;
        },

        /**
         * Возвращает признак, активен ли фильтр «только несоответствующие».
         *
         * @returns {boolean}
         */
        isUnmatchedActive: function () {
            return FilterState.unmatchedOnly;
        },

        /**
         * Сбрасывает все фильтры и режимы к значениям по умолчанию.
         */
        reset: function () {
            FilterState.objectCode = null;
            FilterState.disciplineCode = null;
            FilterState.documentTypeCode = null;
            this._setUnmatchedOnly(false);
            this._setFilterOnly(false);
            this._setContentOnly(false);

            for (var key in INPUT_IDS) {
                if (INPUT_IDS.hasOwnProperty(key)) {
                    var input = document.getElementById(INPUT_IDS[key]);
                    if (input) {
                        input.value = "";
                    }
                    var component = FilterState.autocompleteComponents[key];
                    if (component && typeof component.clear === "function") {
                        component.clear();
                    }
                }
            }

            this.clearAppliedMarkers();
            this._applyState();
        },

        /**
         * Применяет визуальное выделение применённых фильтров.
         * Для категорий, где код не равен null, полю ввода добавляется
         * класс ``filter-applied``. Для остальных полей класс снимается
         * и значение очищается.
         *
         * @param {Object} appliedFilters — объект с фактически применёнными
         *   кодами: { object_code, discipline_code, document_type_code }.
         */
        applyVisualState: function (appliedFilters) {
            var mapping = {
                object: appliedFilters.object_code,
                discipline: appliedFilters.discipline_code,
                documentType: appliedFilters.document_type_code
            };

            for (var key in mapping) {
                if (!mapping.hasOwnProperty(key)) continue;
                var input = document.getElementById(INPUT_IDS[key]);
                if (!input) continue;

                if (mapping[key]) {
                    input.classList.add("filter-applied");
                } else {
                    input.classList.remove("filter-applied");
                    var component = FilterState.autocompleteComponents[key];
                    if (component && typeof component.clear === "function") {
                        component.clear();
                    } else {
                        input.value = "";
                    }
                }
            }
        },

        /**
         * Снимает визуальное выделение со всех полей фильтров.
         */
        clearAppliedMarkers: function () {
            for (var key in INPUT_IDS) {
                if (INPUT_IDS.hasOwnProperty(key)) {
                    var input = document.getElementById(INPUT_IDS[key]);
                    if (input) {
                        input.classList.remove("filter-applied");
                    }
                }
            }
        },

        /**
         * Сохраняет загруженные справочники в состоянии.
         *
         * @param {Object} data — данные от API.
         * @private
         */
        _storeReferences: function (data) {
            FilterState.references.objects = data.objects || { codes: [], aliases_en: [], aliases_ru: [] };
            FilterState.references.disciplines = data.disciplines || { codes: [], aliases_en: [], aliases_ru: [] };
            FilterState.references.documentTypes = data.documentTypes || { codes: [], aliases_en: [], aliases_ru: [] };
        },

        /**
         * Создаёт компоненты автодополнения для каждой категории.
         *
         * @private
         */
        _setupAutocomplete: function () {
            if (typeof window.AutocompleteField !== "function") {
                console.warn("Модуль AutocompleteField не найден. Фильтры будут недоступны.");
                return;
            }

            var self = this;

            var configs = [
                {
                    key: "object",
                    inputId: INPUT_IDS.object,
                    dropdownId: DROPDOWN_IDS.object,
                    references: FilterState.references.objects
                },
                {
                    key: "discipline",
                    inputId: INPUT_IDS.discipline,
                    dropdownId: DROPDOWN_IDS.discipline,
                    references: FilterState.references.disciplines
                },
                {
                    key: "documentType",
                    inputId: INPUT_IDS.documentType,
                    dropdownId: DROPDOWN_IDS.documentType,
                    references: FilterState.references.documentTypes
                }
            ];

            configs.forEach(function (config) {
                var input = document.getElementById(config.inputId);
                var dropdown = document.getElementById(config.dropdownId);
                if (!input || !dropdown) return;

                var component = new window.AutocompleteField({
                    input: input,
                    dropdown: dropdown,
                    getSuggestions: function (query) {
                        var suggestions = self._getSuggestionsForCategory(config.references, query);

                        if (window.DebugUtils && window.DEBUG_FILTERS) {
                            window.DebugUtils.sendDebugEvent("suggestions_count", {
                                category: config.key,
                                value: query,
                                count: suggestions.length
                            });
                        }

                        return suggestions;
                    },
                    renderItem: function (item) {
                        return self._renderItem(item);
                    },
                    onSelect: function (item) {
                        self._onSelectItem(config.key, item);
                    },
                    onInputChange: function (value) {
                        self._clearFilterForCategory(config.key);
                        self._clearAppliedMarker(config.inputId);
                        // Автоматически пересчитать состояние чекбоксов
                        self._applyState();
                    }
                });

                FilterState.autocompleteComponents[config.key] = component;
            });
        },

        /**
         * Очищает код фильтра для указанной категории.
         *
         * @param {string} categoryKey — ключ категории.
         * @private
         */
        _clearFilterForCategory: function (categoryKey) {
            switch (categoryKey) {
                case "object":
                    FilterState.objectCode = null;
                    break;
                case "discipline":
                    FilterState.disciplineCode = null;
                    break;
                case "documentType":
                    FilterState.documentTypeCode = null;
                    break;
            }
        },

        /**
         * Снимает класс ``filter-applied`` с конкретного поля.
         *
         * @param {string} inputId — id поля ввода.
         * @private
         */
        _clearAppliedMarker: function (inputId) {
            var input = document.getElementById(inputId);
            if (input) {
                input.classList.remove("filter-applied");
            }
        },

        /**
         * Возвращает массив подсказок для категории.
         *
         * @param {Object} references — справочники категории.
         * @param {string} query — текущий ввод.
         * @returns {Array<Object>} Массив элементов с полями:
         *   `left`, `right`, `code`, `codeOnLeft`.
         * @private
         */
        _getSuggestionsForCategory: function (references, query) {
            if (!query || !query.trim()) return [];

            query = query.trim().toLowerCase();
            var suggestions = [];

            var isPotentialCode = /^[0-9a-z\-\.]+$/.test(query) && !/[а-яё]/i.test(query);

            if (isPotentialCode) {
                var codeMatches = references.codes.filter(function (code) {
                    return code.toLowerCase().indexOf(query) === 0;
                });

                if (codeMatches.length > 0) {
                    codeMatches.forEach(function (code) {
                        var ruAlias = references.aliases_ru.find(function (a) {
                            return a.code === code;
                        });
                        suggestions.push({
                            left: code,
                            right: ruAlias ? ruAlias.alias : "",
                            code: code,
                            codeOnLeft: true
                        });
                    });
                    return suggestions;
                }
            }

            var isRussian = /[а-яё]/i.test(query);
            var aliasList = isRussian
                ? references.aliases_ru
                : references.aliases_en;

            aliasList.forEach(function (aliasItem) {
                if (aliasItem.alias.toLowerCase().indexOf(query) === 0) {
                    suggestions.push({
                        left: aliasItem.alias,
                        right: aliasItem.code,
                        code: aliasItem.code,
                        codeOnLeft: false
                    });
                }
            });

            return suggestions;
        },

        /**
         * Рендерит HTML для одного элемента выпадающего списка.
         *
         * @param {Object} item — элемент с полями `left`, `right`, `codeOnLeft`.
         * @returns {string} HTML-строка.
         * @private
         */
        _renderItem: function (item) {
            var leftText = DocumentUtils.escapeHtml(item.left);
            var rightText = DocumentUtils.escapeHtml(item.right);

            var leftClass = item.codeOnLeft
                ? "autocomplete-col--narrow"
                : "autocomplete-col--wide";
            var rightClass = item.codeOnLeft
                ? "autocomplete-col--wide"
                : "autocomplete-col--narrow";

            return (
                '<div class="autocomplete-row">' +
                '<span class="' + leftClass + '">' + leftText + '</span>' +
                '<span class="' + rightClass + '">' + rightText + '</span>' +
                '</div>'
            );
        },

        /**
         * Обрабатывает выбор элемента из выпадающего списка.
         *
         * @param {string} categoryKey — ключ категории.
         * @param {Object} item — выбранный элемент (содержит `code`).
         * @private
         */
        _onSelectItem: function (categoryKey, item) {
            switch (categoryKey) {
                case "object":
                    FilterState.objectCode = item.code;
                    break;
                case "discipline":
                    FilterState.disciplineCode = item.code;
                    break;
                case "documentType":
                    FilterState.documentTypeCode = item.code;
                    break;
            }

            if (window.DebugUtils && window.DEBUG_FILTERS) {
                window.DebugUtils.sendDebugEvent("item_selected", {
                    category: categoryKey,
                    value: item.code,
                    count: 1
                });
            }

            // При появлении обычного фильтра чекбокс может стать доступным
            this._applyState();
        },

        /**
         * Привязывает обработчики событий.
         *
         * @private
         */
        _bindEvents: function () {
            var self = this;

            var checkbox = document.getElementById(CHECKBOX_ID);
            if (checkbox) {
                checkbox.addEventListener("change", function () {
                    self._setUnmatchedOnly(this.checked);
                });
            }

            var filterOnlyCheckbox = document.getElementById(FILTER_ONLY_CHECKBOX_ID);
            if (filterOnlyCheckbox) {
                filterOnlyCheckbox.addEventListener("change", function () {
                    self._setFilterOnly(this.checked);
                });
            }

            var contentOnlyCheckbox = document.getElementById(CONTENT_ONLY_CHECKBOX_ID);
            if (contentOnlyCheckbox) {
                contentOnlyCheckbox.addEventListener("change", function () {
                    self._setContentOnly(this.checked);
                });
            }

            var resetButton = document.getElementById(RESET_BUTTON_ID);
            if (resetButton) {
                resetButton.addEventListener("click", function () {
                    self.reset();
                });
            }
        },

        /**
         * Устанавливает состояние «Только фильтр».
         *
         * @param {boolean} value — новое значение.
         * @private
         */
        _setFilterOnly: function (value) {
            FilterState.searchFilterOnly = value;
            var checkbox = document.getElementById(FILTER_ONLY_CHECKBOX_ID);
            if (checkbox) {
                checkbox.checked = value;
            }
            if (value) {
                this._setContentOnly(false);
            }
            this._applyState();
        },

        /**
         * Устанавливает состояние «Только содержание».
         *
         * @param {boolean} value — новое значение.
         * @private
         */
        _setContentOnly: function (value) {
            FilterState.searchContentOnly = value;
            var checkbox = document.getElementById(CONTENT_ONLY_CHECKBOX_ID);
            if (checkbox) {
                checkbox.checked = value;
            }
            if (value) {
                this._setFilterOnly(false);
                // «Только содержание» исключает «Только несоответствующие»
                this._setUnmatchedOnly(false);
            }
            this._applyState();
        },

        /**
         * Устанавливает состояние «Только несоответствующие».
         *
         * @param {boolean} value — новое значение.
         * @private
         */
        _setUnmatchedOnly: function (value) {
            FilterState.unmatchedOnly = value;
            var checkbox = document.getElementById(CHECKBOX_ID);
            if (checkbox) {
                checkbox.checked = value;
            }
            if (value) {
                // Исключаем «Только содержание»
                this._setContentOnly(false);
            }
            this._applyState();
        },

        /**
         * Обновляет доступность чекбокса «Только несоответствующие».
         * Блокирует его, если нет активных обычных фильтров или
         * включён режим «Только содержание».
         *
         * @private
         */
        _updateUnmatchedAvailability: function () {
            var checkbox = document.getElementById(CHECKBOX_ID);
            if (!checkbox) return;

            var disabled = !this.hasActiveOrdinaryFilters() || FilterState.searchContentOnly;
            checkbox.disabled = disabled;

            if (disabled && FilterState.unmatchedOnly) {
                // Если чекбокс стал недоступен, снимаем его
                this._setUnmatchedOnly(false);
            }
        },

        /**
         * Применяет текущее состояние к DOM.
         *
         * Блокирует поля ввода фильтров при активном режиме
         * «Только несоответствующие» (в этом режиме обычные
         * фильтры игнорируются) и обновляет доступность чекбокса
         * «Только несоответствующие».
         *
         * @private
         */
        _applyState: function () {
            var unmatched = FilterState.unmatchedOnly;

            for (var key in INPUT_IDS) {
                if (INPUT_IDS.hasOwnProperty(key)) {
                    var input = document.getElementById(INPUT_IDS[key]);
                    if (input) {
                        input.disabled = unmatched;
                    }
                }
            }

            this._updateUnmatchedAvailability();
        }
    };

    /* ==============================================================
       6. Экспорт для внешних модулей
       ============================================================== */

    if (typeof window !== "undefined") {
        window.FilterCoordinator = FilterCoordinator;
    }
})();
