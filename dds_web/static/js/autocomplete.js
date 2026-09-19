/**
 * Универсальный компонент автодополнения (combobox).
 *
 * Позволяет превратить обычное текстовое поле в поле с выпадающим
 * списком подсказок. Используется в модуле ``FilterCoordinator``
 * для фильтров по метаданным (коды и псевдонимы), но может
 * применяться в любом месте интерфейса.
 *
 * Компонент не зависит от конкретных источников данных: все
 * необходимые функции (получение подсказок, рендеринг элемента,
 * обработка выбора) передаются через конструктор, что обеспечивает
 * инверсию зависимостей и переиспользуемость.
 *
 * Архитектура компонента:
 *
 * +----------------------------------+----------------------------------+
 * | Метод/Свойство                   | Описание                         |
 * +==================================+==================================+
 * | ``constructor(options)``         | Инициализация: привязка          |
 * |                                  | обработчиков, хранение ссылок.  |
 * +----------------------------------+----------------------------------+
 * | ``clear()``                      | Очистка поля ввода, скрытие      |
 * |                                  | выпадающего списка.              |
 * +----------------------------------+----------------------------------+
 * | ``_onInput()``                   | Обработчик ввода: получение      |
 * |                                  | подсказок и их отображение.      |
 * +----------------------------------+----------------------------------+
 * | ``_renderSuggestions(items)``    | Заполнение выпадающего списка.   |
 * +----------------------------------+----------------------------------+
 * | ``_selectItem(item)``            | Выбор элемента: вызов ``onSelect``|
 * |                                  | и скрытие списка.                |
 * +----------------------------------+----------------------------------+
 * | ``_handleKeyDown(event)``        | Обработка стрелок, Enter, Escape.|
 * +----------------------------------+----------------------------------+
 * | ``_hideDropdown()``              | Скрытие выпадающего списка.      |
 * +----------------------------------+----------------------------------+
 *
 * Отладка:
 * Компонент опционально отправляет диагностические события на
 * сервер через глобальный объект ``DebugUtils``. Отправка
 * активируется только при ``window.DEBUG_FILTERS === true``.
 *
 * Дополнительный колбэк ``onInputChange``:
 * Позволяет родительскому компоненту (например, ``FilterCoordinator``)
 * отслеживать изменения текста, введённого пользователем, чтобы
 * своевременно сбрасывать ранее применённый фильтр. Колбэк
 * вызывается при каждом пользовательском вводе (событие ``input``),
 * но не при программной установке значения (например, при выборе
 * элемента из списка), так как программное изменение не генерирует
 * событие ``input`` в современных браузерах.
 *
 * Принципы:
 * - Не содержит бизнес-логики — только управление UI-компонентом.
 * - Не зависит от конкретных данных; работает через колбэки.
 * - Инкапсулирует внутреннее состояние и обработчики.
 * - Экспортирует глобальный конструктор ``AutocompleteField``.
 *
 * Пример использования:
 *
 * .. code-block:: javascript
 *
 *    var autocomplete = new AutocompleteField({
 *        input: document.getElementById('my-input'),
 *        dropdown: document.getElementById('my-dropdown'),
 *        getSuggestions: function(query) {
 *            // вернуть массив объектов {left, right, code}
 *        },
 *        renderItem: function(item) {
 *            return '<div class="row">' +
 *                   '<span>' + item.left + '</span>' +
 *                   '<span>' + item.right + '</span>' +
 *                   '</div>';
 *        },
 *        onSelect: function(item) {
 *            console.log('Выбран код:', item.code);
 *        },
 *        onInputChange: function(value) {
 *            console.log('Пользователь изменил поле:', value);
 *        }
 *    });
 */
(function () {
    "use strict";

    /**
     * Конструктор компонента автодополнения.
     *
     * @param {Object} options — настройки компонента.
     * @param {HTMLInputElement} options.input — поле ввода.
     * @param {HTMLElement} options.dropdown — контейнер выпадающего списка.
     * @param {Function} options.getSuggestions — функция, принимающая
     *   текущий текст запроса и возвращающая массив элементов
     *   (объектов с полями ``left``, ``right``, ``code``). Может
     *   возвращать Promise, если данные загружаются асинхронно.
     * @param {Function} options.renderItem — функция, принимающая
     *   элемент и возвращающая HTML-строку для отображения.
     *   Рекомендуется экранировать пользовательские данные.
     * @param {Function} options.onSelect — колбэк, вызываемый при
     *   выборе элемента (клик или Enter). Получает выбранный элемент.
     * @param {Function} [options.onInputChange] — колбэк, вызываемый
     *   при каждом изменении текста пользователем. Получает текущее
     *   значение поля. Используется для сброса внешнего состояния.
     */
    function AutocompleteField(options) {
        if (!options || !options.input || !options.dropdown) {
            throw new Error("AutocompleteField: необходимо указать input и dropdown.");
        }
        if (typeof options.getSuggestions !== "function") {
            throw new Error("AutocompleteField: getSuggestions должна быть функцией.");
        }
        if (typeof options.renderItem !== "function") {
            throw new Error("AutocompleteField: renderItem должна быть функцией.");
        }
        if (typeof options.onSelect !== "function") {
            throw new Error("AutocompleteField: onSelect должна быть функцией.");
        }

        this._input = options.input;
        this._dropdown = options.dropdown;
        this._getSuggestions = options.getSuggestions;
        this._renderItem = options.renderItem;
        this._onSelect = options.onSelect;
        this._onInputChange = options.onInputChange || null; // опциональный колбэк

        // Индекс выделенного элемента (для навигации с клавиатуры)
        this._highlightedIndex = -1;
        // Ссылка на текущий список элементов
        this._currentItems = [];

        // Флаг, указывающий, что значение было установлено программно.
        // Используется для подавления вызова onInputChange при выборе
        // элемента из списка. В современных браузерах программное
        // изменение value не генерирует событие input, поэтому флаг
        // почти не нужен, но оставлен для надёжности.
        this._isInternalUpdate = false;

        // Привязываем контекст к обработчикам
        this._onInput = this._onInput.bind(this);
        this._handleKeyDown = this._handleKeyDown.bind(this);
        this._onDocumentClick = this._onDocumentClick.bind(this);

        // Устанавливаем атрибуты
        this._input.setAttribute("autocomplete", "off");
        this._input.setAttribute("role", "combobox");
        this._input.setAttribute("aria-expanded", "false");
        this._dropdown.classList.add("autocomplete-dropdown");

        // Обработчики событий
        this._input.addEventListener("input", this._onInput);
        this._input.addEventListener("keydown", this._handleKeyDown);
        document.addEventListener("click", this._onDocumentClick);

        // Обработчик фокуса для отладки
        this._input.addEventListener("focus", function () {
            if (window.DebugUtils && window.DEBUG_FILTERS) {
                window.DebugUtils.sendDebugEvent("focus", {
                    value: this.value
                });
            }
        });
    }

    /**
     * Очищает поле ввода и скрывает выпадающий список.
     */
    AutocompleteField.prototype.clear = function () {
        // Программная очистка не должна вызывать onInputChange
        this._isInternalUpdate = true;
        this._input.value = "";
        this._isInternalUpdate = false;
        this._hideDropdown();
    };

    /**
     * Обработчик события ввода: запрашивает подсказки и отображает их.
     * Также вызывает внешний колбэк onInputChange (если задан),
     * за исключением программных изменений.
     */
    AutocompleteField.prototype._onInput = function () {
        var query = this._input.value;

        // Отправка отладочного события
        if (window.DebugUtils && window.DEBUG_FILTERS) {
            window.DebugUtils.sendDebugEvent("input", {
                value: query
            });
        }

        // Вызываем внешний колбэк только при пользовательском вводе
        if (!this._isInternalUpdate && typeof this._onInputChange === "function") {
            this._onInputChange(query);
        }
        // Сбрасываем флаг после обработки
        this._isInternalUpdate = false;

        var self = this;

        // Получаем подсказки (может быть Promise)
        var result = this._getSuggestions(query);
        if (result && typeof result.then === "function") {
            result.then(function (items) {
                self._currentItems = items || [];
                self._renderDropdown(self._currentItems);
            }).catch(function (err) {
                console.error("Ошибка получения подсказок:", err);
                self._hideDropdown();
            });
        } else {
            this._currentItems = result || [];
            this._renderDropdown(this._currentItems);
        }
    };

    /**
     * Отображает выпадающий список с элементами.
     *
     * @param {Array<Object>} items — массив элементов для отображения.
     */
    AutocompleteField.prototype._renderDropdown = function (items) {
        // Отправка отладочного события
        if (window.DebugUtils && window.DEBUG_FILTERS) {
            window.DebugUtils.sendDebugEvent("suggestions_generated", {
                value: this._input.value,
                count: items.length
            });
        }

        if (!items.length) {
            this._hideDropdown();
            return;
        }

        var html = "";
        for (var i = 0; i < items.length; i++) {
            var itemHtml = this._renderItem(items[i]);
            html +=
                '<div class="autocomplete-item" data-index="' + i + '">' +
                itemHtml +
                "</div>";
        }

        this._dropdown.innerHTML = html;
        this._dropdown.style.display = "block";
        this._input.setAttribute("aria-expanded", "true");

        // Привязываем обработчики клика к элементам
        var self = this;
        var itemElements = this._dropdown.querySelectorAll(".autocomplete-item");
        for (var j = 0; j < itemElements.length; j++) {
            itemElements[j].addEventListener("click", function (e) {
                var index = parseInt(this.getAttribute("data-index"), 10);
                self._selectItem(index);
                e.stopPropagation();
            });
        }

        // Сбрасываем выделение
        this._highlightedIndex = -1;
        this._updateHighlight();
    };

    /**
     * Выбирает элемент по индексу: вызывает onSelect, обновляет
     * поле ввода и скрывает список.
     *
     * @param {number} index — индекс элемента в текущем списке.
     */
    AutocompleteField.prototype._selectItem = function (index) {
        if (index < 0 || index >= this._currentItems.length) return;
        var item = this._currentItems[index];

        // Устанавливаем флаг программного изменения, чтобы
        // onInputChange не вызвался (на случай, если браузер
        // всё-таки сгенерирует событие input).
        this._isInternalUpdate = true;

        // Устанавливаем выбранное значение в поле ввода.
        // Приоритет: displayValue, code, left.
        if (item.displayValue !== undefined) {
            this._input.value = item.displayValue;
        } else if (item.code !== undefined && item.code !== null && item.code !== "") {
            this._input.value = item.code;
        } else if (item.left !== undefined) {
            this._input.value = item.left;
        }

        // Отправка отладочного события
        if (window.DebugUtils && window.DEBUG_FILTERS) {
            window.DebugUtils.sendDebugEvent("item_selected", {
                value: this._input.value,
                count: 1
            });
        }

        // Вызываем колбэк выбора
        this._onSelect(item);

        // Скрываем список
        this._hideDropdown();

        // Сбрасываем флаг после завершения
        this._isInternalUpdate = false;
    };

    /**
     * Обрабатывает нажатия клавиш: стрелки для навигации,
     * Enter для выбора, Escape для закрытия.
     *
     * @param {KeyboardEvent} event — событие клавиатуры.
     */
    AutocompleteField.prototype._handleKeyDown = function (event) {
        var dropdownVisible = this._dropdown.style.display === "block";
        if (!dropdownVisible) return;

        switch (event.key) {
            case "ArrowDown":
                event.preventDefault();
                if (this._highlightedIndex < this._currentItems.length - 1) {
                    this._highlightedIndex++;
                    this._updateHighlight();
                }
                break;
            case "ArrowUp":
                event.preventDefault();
                if (this._highlightedIndex > 0) {
                    this._highlightedIndex--;
                    this._updateHighlight();
                }
                break;
            case "Enter":
                event.preventDefault();
                if (this._highlightedIndex >= 0) {
                    this._selectItem(this._highlightedIndex);
                }
                break;
            case "Escape":
                event.preventDefault();
                this._hideDropdown();
                break;
        }
    };

    /**
     * Обновляет визуальное выделение текущего элемента.
     */
    AutocompleteField.prototype._updateHighlight = function () {
        var items = this._dropdown.querySelectorAll(".autocomplete-item");
        for (var i = 0; i < items.length; i++) {
            if (i === this._highlightedIndex) {
                items[i].classList.add("autocomplete-item--highlighted");
            } else {
                items[i].classList.remove("autocomplete-item--highlighted");
            }
        }
    };

    /**
     * Скрывает выпадающий список и сбрасывает состояние.
     */
    AutocompleteField.prototype._hideDropdown = function () {
        this._dropdown.style.display = "none";
        this._dropdown.innerHTML = "";
        this._input.setAttribute("aria-expanded", "false");
        this._highlightedIndex = -1;
        this._currentItems = [];
    };

    /**
     * Глобальный обработчик кликов: скрывает список, если клик
     * был вне поля ввода и выпадающего списка.
     *
     * @param {MouseEvent} event — событие клика.
     */
    AutocompleteField.prototype._onDocumentClick = function (event) {
        var target = event.target;
        if (this._input !== target && !this._dropdown.contains(target)) {
            this._hideDropdown();
        }
    };

    // Экспорт
    if (typeof window !== "undefined") {
        window.AutocompleteField = AutocompleteField;
    }
})();
