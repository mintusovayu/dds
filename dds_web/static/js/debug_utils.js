/**
 * Утилита отправки отладочных событий на сервер.
 *
 * Предоставляет функцию ``sendDebugEvent``, которая асинхронно
 * отправляет POST-запрос на эндпоинт ``/api/debug/filter-events``.
 * Серверная часть (``dds_web/api.py``) выводит полученные события
 * в stdout, что позволяет видеть диагностику в терминале запуска
 * приложения.
 *
 * Управление отладкой:
 * - Глобальный флаг ``window.DEBUG_FILTERS`` (по умолчанию ``false``)
 *   включает или отключает отправку событий.
 * - Если флаг не установлен или равен ``false``, функция не выполняет
 *   сетевых запросов.
 *
 * Принципы:
 * - Не зависит от других модулей приложения (только fetch).
 * - Безопасно обрабатывает ошибки: сбои отладки не влияют на
 *   основной функционал.
 * - Экспортирует глобальный объект ``DebugUtils``.
 *
 * Подключение:
 * .. code-block:: html
 *
 *    <script src="/static/js/debug_utils.js"></script>
 *    <script src="/static/js/autocomplete.js"></script>
 *    <script src="/static/js/filter_coordinator.js"></script>
 *
 * Использование:
 * .. code-block:: javascript
 *
 *    DebugUtils.sendDebugEvent("input", { value: "7350" });
 */
(function () {
    "use strict";

    /**
     * Отправляет отладочное событие на сервер.
     *
     * Операции:
     * +---+-----------------------------------------------------+
     * | № | Описание                                            |
     * +===+=====================================================+
     * | 1 | Проверяет, включён ли режим отладки                |
     * |   | (``window.DEBUG_FILTERS``). Если нет — выходит.     |
     * +---+-----------------------------------------------------+
     * | 2 | Формирует объект данных с полями ``event_type``,    |
     * |   | ``category``, ``value`` и ``details`` (если         |
     * |   | указаны).                                          |
     * +---+-----------------------------------------------------+
     * | 3 | Выполняет fetch POST-запрос к                       |
     * |   | ``/api/debug/filter-events``.                       |
     * +---+-----------------------------------------------------+
     * | 4 | Любые ошибки сети игнорируются (выводятся в         |
     * |   | консоль браузера только при необходимости).         |
     * +---+-----------------------------------------------------+
     *
     * @param {string} type — тип события (например, ``"focus"``,
     *     ``"input"``, ``"suggestions_generated"``,
     *     ``"item_selected"``, ``"references_loaded"``).
     * @param {Object} [payload] — дополнительные данные события.
     * @param {string} [payload.category] — категория фильтра
     *     (``"object"``, ``"discipline"``, ``"documentType"``).
     * @param {string} [payload.value] — текущее значение поля
     *     или выбранный код.
     * @param {number} [payload.count] — количество элементов,
     *     например, найденных подсказок.
     */
    function sendDebugEvent(type, payload) {
        // 1. Проверка флага отладки
        if (!window.DEBUG_FILTERS) {
            return;
        }

        // 2. Подготовка тела запроса
        var body = {
            event_type: type,
            category: (payload && payload.category) || "",
            value: (payload && payload.value) || "",
            details: {
                count: (payload && payload.count) || 0
            }
        };

        // 3. Отправка POST-запроса
        try {
            fetch("/api/debug/filter-events", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(body)
            }).catch(function (err) {
                // 4. Игнорируем ошибки отладки
                console.error("Ошибка отправки отладочного события:", err);
            });
        } catch (e) {
            // На случай, если fetch недоступен
            console.error("Fetch недоступен:", e);
        }
    }

    /* ==============================================================
       Экспорт
       ============================================================== */

    if (typeof window !== "undefined") {
        window.DebugUtils = {
            sendDebugEvent: sendDebugEvent
        };
        // По умолчанию отладка выключена
        if (window.DEBUG_FILTERS === undefined) {
            window.DEBUG_FILTERS = false;
        }
    }
})();
