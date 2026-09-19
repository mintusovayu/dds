/**
 * Логика панели настроек веб-приложения DDS.
 *
 * Модуль реализует панель настроек в стиле Visual Studio Code.
 * Панель открывается как модальное окно и содержит разделы:
 *
 * +----------------------------------+----------------------------------+
 * | Раздел                           | Доступность                      |
 * +==================================+==================================+
 * | Профиль                          | Все пользователи                 |
 * +----------------------------------+----------------------------------+
 * | Интерфейс                        | Все пользователи                 |
 * +----------------------------------+----------------------------------+
 * | Сканирование                     | Только администратор             |
 * +----------------------------------+----------------------------------+
 * | Модули                           | Только администратор             |
 * +----------------------------------+----------------------------------+
 * | Система                          | Только администратор             |
 * +----------------------------------+----------------------------------+
 * | Диагностика                      | Только администратор             |
 * +----------------------------------+----------------------------------+
 *
 * Рефакторинг интерфейса:
 * - Раздел «Интерфейс» содержит активный выпадающий список
 *   для выбора темы оформления (загрузка через ``ThemeManager``).
 * - Размер шрифта остаётся заглушкой (``disabled``).
 * - Все emoji-иконки заменены на плоские SVG-иконки в стиле
 *   Feather/VSCode. SVG помечены ``aria-hidden="true"``, доступность
 *   обеспечивается текстовым содержимым кнопки.
 *
 * Индикация активности Activity Bar:
 * - При открытии панели настроек на иконку ``#activity-settings``
 *   устанавливается класс ``active``. Одновременно активное
 *   состояние иконки поиска (``#activity-search``) временно
 *   снимается и запоминается, чтобы избежать визуального наложения
 *   двух активных иконок.
 * - При закрытии панели класс ``active`` снимается с иконки
 *   настроек, а иконка поиска восстанавливается в прежнее состояние.
 *
 * Симметрия привязки SSE-обработчиков (скорректированный план,
 * шаг 5.6 рефакторинга v5.0):
 *
 * Ранее метод ``initSSEHandlers`` регистрировал обработчики на
 * глобальном ``EventDispatcher`` (внутри ``SSEModule``) при каждом
 * вызове. При повторной инициализации ``SettingsModule`` (например,
 * если архитектура изменится так, что инициализация будет вызываться
 * повторно) обработчики накапливались бы — это привело бы к
 * многократному выполнению одного и того же действия.
 *
 * Введён флаг ``_sseBound``: при первом вызове ``initSSEHandlers``
 * он устанавливается в ``true``, повторные вызовы становятся
 * no-op. Это симметрично защите ``_scanMonitoringStarted`` в
 * ``app.js::startScanMonitoring``.
 *
 * Отписка при закрытии панели не выполняется: ``SettingsPanel``
 * переиспользует глобальный SSE-канал, поэтому обработчики должны
 * продолжать существовать между открытиями/закрытиями панели.
 * Отписка усложнила бы логику без практической пользы.
 *
 * Публичный API модуля:
 * - ``SettingsModule.init(options)`` — инициализация панели.
 * - ``SettingsModule.open()`` — открытие панели.
 * - ``SettingsModule.close()`` — закрытие панели.
 * - ``SettingsModule.isOpen()`` — текущее состояние панели (``true``,
 *   если панель открыта). Используется другими модулями (например,
 *   ``app.js``) для защиты от конфликтов активного состояния иконок.
 *
 * Архитектура модуля:
 *
 * +----------------------------------+----------------------------------+
 * | Компонент                        | Ответственность                  |
 * +==================================+==================================+
 * | ``SettingsAPI``                  | Взаимодействие с REST API.       |
 * +----------------------------------+----------------------------------+
 * | ``SettingsRenderer``             | Рендеринг разделов настроек.     |
 * +----------------------------------+----------------------------------+
 * | ``SettingsPanel``                | Управление модальным окном,      |
 * |                                  | активностью Activity Bar, SSE.   |
 * +----------------------------------+----------------------------------+
 *
 * Принципы:
 * - Модуль не содержит бизнес-логики — только отображение
 *   и координация запросов к API.
 * - Данные загружаются через REST API.
 * - Модуль интегрируется с ``UIStateManager``, ``ThemeManager``.
 * - Для временного переключения активного состояния иконок
 *   используются публичные методы ``DDSApp``.
 *
 * Зависимости:
 * - ``ui_state_manager.js`` — управление состояниями элементов.
 * - ``sse.js`` — модуль для работы с SSE.
 * - ``theme_manager.js`` — управление темами.
 * - ``app.js`` — глобальный объект ``DDSApp`` с методами
 *   ``setSearchIconActive`` и ``getSearchIconActive``.
 * - ``main.html`` — DOM-структура и контекст пользователя.
 * - ``base.css`` — стили ``.icon``, ``.icon--sm``.
 */
(function () {
    "use strict";

    /* ==============================================================
       1. Импорт внешних модулей
       ============================================================== */

    var DocumentUtils = window.DocumentUtils;

    /* ==============================================================
       2. Клиент для REST API настроек и сканирования
       ============================================================== */

    var SettingsAPI = {
        getScanStatus: function () {
            return fetch("/api/scan/status")
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Ошибка загрузки статуса сканирования");
                    }
                    return response.json();
                });
        },

        startScan: function () {
            return fetch("/api/scan/start", { method: "POST" })
                .then(function (response) {
                    if (!response.ok) {
                        return response.json().then(function (data) {
                            throw new Error(data.detail || "Ошибка запуска сканирования");
                        });
                    }
                    return response.json();
                });
        },

        cancelScan: function () {
            return fetch("/api/scan/cancel", { method: "POST" })
                .then(function (response) {
                    if (!response.ok) {
                        return response.json().then(function (data) {
                            throw new Error(data.detail || "Ошибка отмены сканирования");
                        });
                    }
                    return response.json();
                });
        },

        getModules: function () {
            return fetch("/api/modules")
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Ошибка загрузки списка модулей");
                    }
                    return response.json();
                });
        },

        getSettings: function () {
            return fetch("/api/settings")
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Ошибка загрузки настроек");
                    }
                    return response.json();
                });
        },

        updateSettings: function (data) {
            return fetch("/api/settings", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(data),
            })
                .then(function (response) {
                    if (!response.ok) {
                        return response.json().then(function (d) {
                            throw new Error(d.detail || "Ошибка обновления настроек");
                        });
                    }
                    return response.json();
                });
        },

        getDiagnostics: function () {
            return fetch("/api/diagnostics")
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("Ошибка загрузки диагностики");
                    }
                    return response.json();
                });
        },

        changePassword: function (username, oldPassword, newPassword) {
            return fetch("/api/auth/change-password", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    username: username,
                    old_password: oldPassword,
                    new_password: newPassword,
                }),
            })
                .then(function (response) {
                    if (!response.ok) {
                        return response.json().then(function (d) {
                            throw new Error(d.detail || "Ошибка смены пароля");
                        });
                    }
                    return response.json();
                });
        }
    };

    /* ==============================================================
       3. Рендеринг разделов настроек
       ============================================================== */

    var SettingsRenderer = {
        renderProfile: function (data) {
            var html =
                '<div class="settings-section">' +
                '<h3>Профиль</h3>' +
                '<table class="settings-table">' +
                '<tr>' +
                '<td class="settings-label">Имя пользователя</td>' +
                '<td>' + DocumentUtils.escapeHtml(data.username || "") + '</td>' +
                '</tr>' +
                '<tr>' +
                '<td class="settings-label">Роль</td>' +
                '<td>' + DocumentUtils.escapeHtml(data.role || "") + '</td>' +
                '</tr>' +
                '</table>' +
                '</div>';
            html +=
                '<div class="settings-section">' +
                '<h4>Смена пароля</h4>' +
                '<form id="settings-change-password-form">' +
                '<div class="settings-form-group">' +
                '<label for="settings-old-password">Текущий пароль</label>' +
                '<input type="password" id="settings-old-password" ' +
                'autocomplete="current-password" required>' +
                '</div>' +
                '<div class="settings-form-group">' +
                '<label for="settings-new-password">Новый пароль</label>' +
                '<input type="password" id="settings-new-password" ' +
                'autocomplete="new-password" required>' +
                '<p class="settings-hint">' +
                'Минимум 10 символов. Не должен совпадать с именем ' +
                'пользователя или состоять только из цифр.' +
                '</p>' +
                '</div>' +
                '<div class="settings-form-group">' +
                '<label for="settings-confirm-password">' +
                'Подтверждение нового пароля</label>' +
                '<input type="password" id="settings-confirm-password" ' +
                'autocomplete="new-password" required>' +
                '</div>' +
                '<button type="submit" class="btn btn--primary">' +
                'Сменить пароль</button>' +
                '</form>' +
                '<div id="settings-profile-message" ' +
                'class="settings-message"></div>' +
                '</div>';
            return html;
        },

        renderInterfaceSection: function () {
            var html =
                '<div class="settings-section">' +
                '<h3>Интерфейс</h3>' +
                '<div class="settings-form-group">' +
                '<label for="settings-theme">Тема оформления</label>' +
                '<select id="settings-theme" class="input">' +
                '<option value="">Загрузка…</option>' +
                '</select>' +
                '</div>' +
                '<div class="settings-form-group">' +
                '<label for="settings-font-size">Размер шрифта</label>' +
                '<select id="settings-font-size" disabled>' +
                '<option value="small">Маленький</option>' +
                '<option value="medium" selected>Средний</option>' +
                '<option value="large">Большой</option>' +
                '</select>' +
                '</div>' +
                '<p class="settings-hint">' +
                'Настройки размера шрифта будут доступны в будущих версиях.' +
                '</p>' +
                '</div>';
            return html;
        },

        renderScanSection: function (data) {
            var statusText = getScanStatusText(data.status);
            var isRunning = data.status === "running";
            var progressPercent = data.progress_percent || 0;
            var html =
                '<div class="settings-section">' +
                '<h3>Сканирование</h3>' +
                '<div class="settings-scan-status">' +
                '<div class="settings-scan-status-row">' +
                '<span class="settings-label">Статус:</span>' +
                '<span class="settings-value badge--' + DocumentUtils.escapeHtml(data.status) + '">' +
                DocumentUtils.escapeHtml(statusText) + '</span>' +
                '</div>';
            if (isRunning || data.status === "completed" || data.status === "interrupted") {
                html +=
                    '<div class="settings-scan-status-row">' +
                    '<span class="settings-label">Прогресс:</span>' +
                    '<span class="settings-value">' +
                    progressPercent.toFixed(1) + '%</span>' +
                    '</div>' +
                    '<div class="settings-progress">' +
                    '<div class="settings-progress-bar" ' +
                    'style="width: ' + progressPercent.toFixed(1) + '%"></div>' +
                    '</div>' +
                    '<div class="settings-scan-status-row">' +
                    '<span class="settings-label">Обработано:</span>' +
                    '<span class="settings-value">' +
                    data.processed_files + ' / ' + data.total_files + '</span>' +
                    '</div>';
                if (data.current_file) {
                    html +=
                        '<div class="settings-scan-status-row">' +
                        '<span class="settings-label">Текущий файл:</span>' +
                        '<span class="settings-value settings-file-path">' +
                        DocumentUtils.escapeHtml(data.current_file) + '</span>' +
                        '</div>';
                }
                html +=
                    '<div class="settings-scan-counters">' +
                    '<span>Проиндексировано: <b>' + data.indexed_files + '</b></span>' +
                    '<span>Дубликаты: <b>' + data.duplicate_files + '</b></span>' +
                    '<span>Пропущено: <b>' + data.skipped_files + '</b></span>' +
                    '<span>Ошибки: <b>' + data.error_files + '</b></span>' +
                    '</div>';
            }
            html += '</div>';
            html += '<div class="settings-scan-controls">';
            if (isRunning) {
                // Плоская SVG-иконка «стоп» (Feather: square)
                html +=
                    '<button id="settings-btn-cancel-scan" class="btn btn--danger">' +
                    '  <svg xmlns="http://www.w3.org/2000/svg" ' +
                    '       viewBox="0 0 24 24" ' +
                    '       class="icon icon--sm" ' +
                    '       aria-hidden="true">' +
                    '    <rect x="5" y="5" width="14" height="14" rx="1"></rect>' +
                    '  </svg>' +
                    '  <span>Отменить сканирование</span>' +
                    '</button>';
            } else {
                // Плоская SVG-иконка «воспроизведение» (Feather: play)
                html +=
                    '<button id="settings-btn-start-scan" class="btn btn--primary">' +
                    '  <svg xmlns="http://www.w3.org/2000/svg" ' +
                    '       viewBox="0 0 24 24" ' +
                    '       class="icon icon--sm" ' +
                    '       aria-hidden="true">' +
                    '    <polygon points="6 4 20 12 6 20 6 4"></polygon>' +
                    '  </svg>' +
                    '  <span>Запустить сканирование</span>' +
                    '</button>';
            }
            html += '</div>';
            html += '<div id="settings-scan-message" class="settings-message"></div>';
            html += '</div>';
            return html;
        },

        renderSecondaryScanSection: function (data) {
            var statusText = "Вторичная обработка…";
            var isRunning = data.status === "running" || data.stage === "started" ||
                data.stage === "metadata_update" || data.stage === "modules";
            var progressPercent = data.progress_percent || 0;
            var stageText = "";

            if (data.stage === "metadata_update") {
                stageText = " (обновление метаданных)";
            } else if (data.stage === "modules" && data.module_name) {
                stageText = " (" + data.module_name + ")";
            }

            var html =
                '<div class="settings-section">' +
                '<h3>Сканирование</h3>' +
                '<div class="settings-scan-status">' +
                '<div class="settings-scan-status-row">' +
                '<span class="settings-label">Статус:</span>' +
                '<span class="settings-value badge--running">' +
                DocumentUtils.escapeHtml(statusText) + '</span>' +
                '</div>' +
                '<div class="settings-scan-status-row">' +
                '<span class="settings-label">Прогресс:</span>' +
                '<span class="settings-value">' +
                progressPercent.toFixed(1) + '%' + stageText + '</span>' +
                '</div>' +
                '<div class="settings-progress">' +
                '<div class="settings-progress-bar" ' +
                'style="width: ' + progressPercent.toFixed(1) + '%"></div>' +
                '</div>';

            if (typeof data.processed_documents !== "undefined" &&
                typeof data.total_documents !== "undefined") {
                html +=
                    '<div class="settings-scan-status-row">' +
                    '<span class="settings-label">Обработано:</span>' +
                    '<span class="settings-value">' +
                    data.processed_documents + ' / ' + data.total_documents + '</span>' +
                    '</div>';
            }

            html += '</div>';

            html += '<div class="settings-scan-controls">';
            if (isRunning) {
                html +=
                    '<button id="settings-btn-cancel-scan" class="btn btn--danger">' +
                    '  <svg xmlns="http://www.w3.org/2000/svg" ' +
                    '       viewBox="0 0 24 24" ' +
                    '       class="icon icon--sm" ' +
                    '       aria-hidden="true">' +
                    '    <rect x="5" y="5" width="14" height="14" rx="1"></rect>' +
                    '  </svg>' +
                    '  <span>Отменить сканирование</span>' +
                    '</button>';
            }
            html += '</div>';
            html += '<div id="settings-scan-message" class="settings-message"></div>';
            html += '</div>';
            return html;
        },

        renderModulesSection: function (data) {
            var modules = data.modules || [];
            var html =
                '<div class="settings-section">' +
                '<h3>Модули</h3>';
            if (modules.length === 0) {
                html += '<p class="settings-empty">Модули не обнаружены.</p>';
            } else {
                html += '<table class="settings-table">';
                html += '<thead><tr><th>Имя модуля</th><th>Статус</th></tr></thead><tbody>';
                for (var i = 0; i < modules.length; i++) {
                    var module = modules[i];
                    html +=
                        '<tr>' +
                        '<td>' + DocumentUtils.escapeHtml(module.module_name) + '</td>' +
                        '<td><span class="badge badge--' +
                        DocumentUtils.escapeHtml(module.status) + '">' +
                        DocumentUtils.escapeHtml(module.status) + '</span></td>' +
                        '</tr>';
                }
                html += '</tbody></table>';
            }
            html += '</div>';
            return html;
        },

        renderSystemSection: function (data) {
            var html =
                '<div class="settings-section">' +
                '<h3>Система</h3>' +
                '<form id="settings-system-form">' +
                '<div class="settings-form-group">' +
                '<label for="settings-rd-directory">Путь к каталогу с документацией</label>' +
                '<input type="text" id="settings-rd-directory" ' +
                'value="' + DocumentUtils.escapeHtml(data.rd_directory || "") + '" ' +
                'placeholder="/path/to/rd_documents">' +
                '<p class="settings-hint">Каталог с PDF-файлами рабочей документации.</p>' +
                '</div>' +
                '<div class="settings-form-group">' +
                '<label for="settings-db-path">Путь к файлу базы данных</label>' +
                '<input type="text" id="settings-db-path" ' +
                'value="' + DocumentUtils.escapeHtml(data.db_path || "") + '" ' +
                'placeholder="/path/to/dds_database.db">' +
                '<p class="settings-hint">Изменение требует перезапуска DDS.</p>' +
                '</div>' +
                '<div class="settings-form-group">' +
                '<label for="settings-modules-directory">Путь к каталогу модулей</label>' +
                '<input type="text" id="settings-modules-directory" ' +
                'value="' + DocumentUtils.escapeHtml(data.modules_directory || "") + '" disabled>' +
                '<p class="settings-hint">Изменение через веб-интерфейс не поддерживается.</p>' +
                '</div>' +
                // Плоская SVG-иконка «сохранить» (Feather: save)
                '<button type="submit" class="btn btn--primary">' +
                '  <svg xmlns="http://www.w3.org/2000/svg" ' +
                '       viewBox="0 0 24 24" ' +
                '       class="icon icon--sm" ' +
                '       aria-hidden="true">' +
                '    <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path>' +
                '    <polyline points="17 21 17 13 7 13 7 21"></polyline>' +
                '    <polyline points="7 3 7 8 15 8"></polyline>' +
                '  </svg>' +
                '  <span>Сохранить настройки</span>' +
                '</button>' +
                '</form>' +
                '<div id="settings-system-message" class="settings-message"></div>' +
                '</div>';
            return html;
        },

        renderDiagnosticsSection: function (data) {
            var html =
                '<div class="settings-section">' +
                '<h3>Диагностика</h3>';
            if (data.event_bus) {
                html +=
                    '<h4>Шина событий</h4>' +
                    '<table class="settings-table">' +
                    '<tr><td>Размер очереди</td><td>' + data.event_bus.queue_size + '</td></tr>' +
                    '<tr><td>Потеряно событий</td><td>' + data.event_bus.dropped_events_total + '</td></tr>' +
                    '<tr><td>Подписчиков</td><td>' + data.event_bus.subscriber_count + '</td></tr>' +
                    '<tr><td>Работает</td><td>' + (data.event_bus.running ? 'Да' : 'Нет') + '</td></tr>' +
                    '</table>';
            }
            if (data.search_capabilities) {
                html +=
                    '<h4>Возможности поиска</h4>' +
                    '<p>' + DocumentUtils.escapeHtml(data.search_capabilities.join(', ')) + '</p>';
            }
            if (data.db_pool) {
                html +=
                    '<h4>Пул соединений БД</h4>' +
                    '<table class="settings-table">' +
                    '<tr><td>Размер пула чтения</td><td>' + data.db_pool.read_pool_size + '</td></tr>' +
                    '<tr><td>Пул чтения закрыт</td><td>' + (data.db_pool.read_pool_closed ? 'Да' : 'Нет') + '</td></tr>' +
                    '<tr><td>Соединение записи активно</td><td>' + (data.db_pool.write_connection_active ? 'Да' : 'Нет') + '</td></tr>' +
                    '</table>';
            }
            html += '</div>';
            return html;
        }
    };

    /* ==============================================================
       4. Управление панелью настроек
       ============================================================== */

    var SettingsPanel = {
        isOpen: false,
        activeSection: "profile",
        userData: null,
        isAdmin: false,
        /**
         * Сохранённое активное состояние иконки поиска до открытия
         * панели настроек. Используется для восстановления
         * состояния при закрытии панели.
         * @type {boolean}
         * @private
         */
        _searchIconActiveBefore: false,

        /**
         * Флаг привязки SSE-обработчиков (скорректированный план,
         * шаг 5.6 рефакторинга v5.0).
         *
         * ``false`` до первого вызова ``initSSEHandlers``; после
         * успешной привязки — ``true``. Повторные вызовы
         * ``initSSEHandlers`` становятся no-op, что предотвращает
         * накопление обработчиков на глобальном ``EventDispatcher``.
         *
         * Симметричен защите ``_scanMonitoringStarted`` в
         * ``app.js::startScanMonitoring``.
         *
         * @type {boolean}
         * @private
         */
        _sseBound: false,

        init: function (options) {
            this.userData = {
                username: options.username || "",
                role: options.role || "user",
            };
            this.isAdmin = this.userData.role === "admin";
            var settingsBtn = document.getElementById("activity-settings");
            if (settingsBtn) {
                var self = this;
                settingsBtn.addEventListener("click", function () {
                    if (self.isOpen) {
                        self.close();
                    } else {
                        self.open();
                    }
                });
            }
            this.initSSEHandlers();
        },

        open: function () {
            if (this.isOpen) return;
            this.isOpen = true;

            // Запоминаем текущее состояние иконки поиска и временно
            // его снимаем, чтобы визуально активной была только иконка настроек.
            if (typeof window.DDSApp !== "undefined" &&
                typeof window.DDSApp.getSearchIconActive === "function" &&
                typeof window.DDSApp.setSearchIconActive === "function") {
                this._searchIconActiveBefore = window.DDSApp.getSearchIconActive();
                window.DDSApp.setSearchIconActive(false);
            } else {
                this._searchIconActiveBefore = false;
            }

            this._setActivityIconActive(true);

            var overlay = document.createElement("div");
            overlay.className = "settings-overlay";
            overlay.id = "settings-overlay";
            var modal = document.createElement("div");
            modal.className = "settings-modal";
            modal.id = "settings-modal";
            var header = document.createElement("div");
            header.className = "settings-modal-header";
            header.innerHTML =
                '<h2>Настройки</h2>' +
                '<button id="settings-close-btn" class="settings-close-btn">✕</button>';
            var body = document.createElement("div");
            body.className = "settings-modal-body";
            body.id = "settings-modal-body";
            modal.appendChild(header);
            modal.appendChild(body);
            overlay.appendChild(modal);
            document.body.appendChild(overlay);
            this.render();
            this.bindEvents();
            this.loadSection(this.activeSection);
        },

        close: function () {
            if (!this.isOpen) return;
            this.isOpen = false;

            this._setActivityIconActive(false);

            // Восстанавливаем активное состояние иконки поиска,
            // если оно было активно до открытия панели настроек.
            if (typeof window.DDSApp !== "undefined" &&
                typeof window.DDSApp.setSearchIconActive === "function") {
                window.DDSApp.setSearchIconActive(this._searchIconActiveBefore);
            }

            var overlay = document.getElementById("settings-overlay");
            if (overlay) {
                overlay.remove();
            }
        },

        /**
         * Устанавливает или снимает класс ``active`` на иконке
         * настроек Activity Bar.
         *
         * @param {boolean} isActive — если ``true``, класс добавляется;
         *   если ``false`` — снимается.
         * @private
         */
        _setActivityIconActive: function (isActive) {
            var settingsIcon = document.getElementById("activity-settings");
            if (!settingsIcon) return;
            if (isActive) {
                settingsIcon.classList.add("active");
            } else {
                settingsIcon.classList.remove("active");
            }
        },

        render: function () {
            var body = document.getElementById("settings-modal-body");
            if (!body) return;
            var html = '<div class="settings-layout">';
            html += '<div class="settings-nav">';
            html += '<button class="settings-nav-item" data-section="profile">Профиль</button>';
            html += '<button class="settings-nav-item" data-section="interface">Интерфейс</button>';
            if (this.isAdmin) {
                html += '<button class="settings-nav-item" data-section="scan">Сканирование</button>';
                html += '<button class="settings-nav-item" data-section="modules">Модули</button>';
                html += '<button class="settings-nav-item" data-section="system">Система</button>';
                html += '<button class="settings-nav-item" data-section="diagnostics">Диагностика</button>';
            }
            html += '</div>';
            html += '<div class="settings-content" id="settings-content"></div>';
            html += '</div>';
            body.innerHTML = html;
            this.highlightActiveSection();
        },

        highlightActiveSection: function () {
            var navItems = document.querySelectorAll(".settings-nav-item");
            for (var i = 0; i < navItems.length; i++) {
                navItems[i].classList.remove("active");
                if (navItems[i].dataset.section === this.activeSection) {
                    navItems[i].classList.add("active");
                }
            }
        },

        loadSection: function (sectionId) {
            this.activeSection = sectionId;
            this.highlightActiveSection();
            var content = document.getElementById("settings-content");
            if (!content) return;
            var self = this;
            switch (sectionId) {
                case "profile":
                    content.innerHTML = SettingsRenderer.renderProfile(this.userData);
                    self.bindProfileEvents();
                    break;
                case "interface":
                    content.innerHTML = SettingsRenderer.renderInterfaceSection();
                    self.bindInterfaceEvents();
                    break;
                case "scan":
                    SettingsAPI.getScanStatus()
                        .then(function (data) {
                            content.innerHTML = SettingsRenderer.renderScanSection(data);
                            self.bindScanEvents(data.status);
                            self.registerScanRules();
                            UIStateManager.apply();
                            if (data.status === "running") {
                                if (typeof window.DDSApp !== "undefined" &&
                                    window.DDSApp.startScanMonitoring) {
                                    window.DDSApp.startScanMonitoring();
                                }
                            }
                        })
                        .catch(function (error) {
                            content.innerHTML =
                                '<div class="settings-error">' +
                                DocumentUtils.escapeHtml(error.message) + '</div>';
                        });
                    break;
                case "modules":
                    SettingsAPI.getModules()
                        .then(function (data) {
                            content.innerHTML = SettingsRenderer.renderModulesSection(data);
                        })
                        .catch(function (error) {
                            content.innerHTML =
                                '<div class="settings-error">' +
                                DocumentUtils.escapeHtml(error.message) + '</div>';
                        });
                    break;
                case "system":
                    SettingsAPI.getSettings()
                        .then(function (data) {
                            content.innerHTML = SettingsRenderer.renderSystemSection(data);
                            self.bindSystemEvents();
                            self.registerSystemRules();
                            UIStateManager.apply();
                        })
                        .catch(function (error) {
                            content.innerHTML =
                                '<div class="settings-error">' +
                                DocumentUtils.escapeHtml(error.message) + '</div>';
                        });
                    break;
                case "diagnostics":
                    SettingsAPI.getDiagnostics()
                        .then(function (data) {
                            content.innerHTML = SettingsRenderer.renderDiagnosticsSection(data);
                        })
                        .catch(function (error) {
                            content.innerHTML =
                                '<div class="settings-error">' +
                                DocumentUtils.escapeHtml(error.message) + '</div>';
                        });
                    break;
                default:
                    content.innerHTML = '<p>Раздел не найден.</p>';
            }
        },

        registerScanRules: function () {
            if (typeof UIStateManager === "undefined") return;
            UIStateManager.addRule("settings-btn-start-scan", function (f) {
                return f.scan_status !== "running";
            });
            UIStateManager.addRule("settings-btn-cancel-scan", function (f) {
                return f.scan_status === "running";
            });
        },

        registerSystemRules: function () {
            if (typeof UIStateManager === "undefined") return;
            UIStateManager.addRule("settings-system-form", function (f) {
                return f.scan_status !== "running";
            }, { target: "form" });
        },

        bindInterfaceEvents: function () {
            var themeSelect = document.getElementById("settings-theme");
            if (!themeSelect) return;

            if (typeof ThemeManager === "undefined") {
                console.error("ThemeManager не загружен.");
                return;
            }

            // Загружаем список тем через ThemeManager (кэшируется)
            ThemeManager.loadThemes()
                .then(function (themes) {
                    var currentTheme = ThemeManager.getCurrentTheme();
                    themeSelect.innerHTML = "";
                    themes.forEach(function (themeName) {
                        var option = document.createElement("option");
                        option.value = themeName;
                        option.textContent = themeName.charAt(0).toUpperCase() + themeName.slice(1);
                        if (themeName === currentTheme) {
                            option.selected = true;
                        }
                        themeSelect.appendChild(option);
                    });
                })
                .catch(function (error) {
                    themeSelect.innerHTML = '<option value="dark">Dark (по умолчанию)</option>';
                    console.error("Не удалось загрузить темы:", error);
                });

            // Обработчик выбора темы
            themeSelect.addEventListener("change", function () {
                var selectedTheme = themeSelect.value;
                if (selectedTheme) {
                    ThemeManager.applyTheme(selectedTheme);
                }
            });
        },

        /**
         * Регистрирует обработчики SSE-событий сканирования.
         *
         * Защита от повторной привязки (скорректированный план,
         * шаг 5.6 рефакторинга v5.0):
         * При первом вызове метода флаг ``_sseBound`` устанавливается
         * в ``true``; повторные вызовы становятся no-op. Это
         * предотвращает накопление обработчиков на глобальном
         * ``EventDispatcher`` внутри ``SSEModule``.
         *
         * Симметричен защите ``_scanMonitoringStarted`` в
         * ``app.js::startScanMonitoring``: там повторные вызовы
         * также предотвращены флагом.
         *
         * Отписка при закрытии панели **не** выполняется: панель
         * переиспользует глобальный SSE-канал ``SSEModule``, и
         * обработчики должны продолжать существовать между
         * открытиями/закрытиями. Отписка усложнила бы логику без
         * практической пользы.
         */
        initSSEHandlers: function () {
            if (this._sseBound) return;
            this._sseBound = true;

            if (typeof SSEModule === "undefined") return;
            var self = this;

            SSEModule.onProgress(function (data) {
                if (!self.isOpen || self.activeSection !== "scan") return;
                var content = document.getElementById("settings-content");
                if (content) {
                    content.innerHTML = SettingsRenderer.renderScanSection(data);
                    self.bindScanEvents(data.status);
                    self.registerScanRules();
                    UIStateManager.apply();
                }
            });

            SSEModule.onComplete(function (data) {
                if (typeof UIStateManager !== "undefined") {
                    UIStateManager.update({ scan_status: data.status });
                }
                if (self.isOpen && self.activeSection === "scan") {
                    var content = document.getElementById("settings-content");
                    if (content) {
                        content.innerHTML = SettingsRenderer.renderScanSection(data);
                        self.bindScanEvents(data.status);
                        self.registerScanRules();
                        UIStateManager.apply();
                    }
                }
            });

            SSEModule.onSecondaryStarted(function (data) {
                if (!self.isOpen || self.activeSection !== "scan") return;
                var content = document.getElementById("settings-content");
                if (content) {
                    content.innerHTML = SettingsRenderer.renderSecondaryScanSection(data);
                    self.bindScanEvents("running");
                    self.registerScanRules();
                    UIStateManager.apply();
                }
            });

            SSEModule.onSecondaryProgress(function (data) {
                if (!self.isOpen || self.activeSection !== "scan") return;
                var content = document.getElementById("settings-content");
                if (content) {
                    content.innerHTML = SettingsRenderer.renderSecondaryScanSection(data);
                    self.bindScanEvents("running");
                    self.registerScanRules();
                    UIStateManager.apply();
                }
            });

            SSEModule.onSecondaryCompleted(function (data) {
                if (typeof UIStateManager !== "undefined") {
                    UIStateManager.update({ scan_status: "idle" });
                }
                if (self.isOpen && self.activeSection === "scan") {
                    self.loadSection("scan");
                }
            });
        },

        bindEvents: function () {
            var self = this;
            var closeBtn = document.getElementById("settings-close-btn");
            if (closeBtn) {
                closeBtn.addEventListener("click", function () {
                    self.close();
                });
            }
            var navItems = document.querySelectorAll(".settings-nav-item");
            for (var i = 0; i < navItems.length; i++) {
                navItems[i].addEventListener("click", function () {
                    self.loadSection(this.dataset.section);
                });
            }
            var overlay = document.getElementById("settings-overlay");
            if (overlay) {
                overlay.addEventListener("click", function (e) {
                    if (e.target === overlay) {
                        self.close();
                    }
                });
            }
        },

        bindProfileEvents: function () {
            var self = this;
            var form = document.getElementById("settings-change-password-form");
            if (form) {
                form.addEventListener("submit", function (e) {
                    e.preventDefault();
                    var oldPassword = document.getElementById("settings-old-password");
                    var newPassword = document.getElementById("settings-new-password");
                    var confirmPassword = document.getElementById("settings-confirm-password");
                    if (!oldPassword || !newPassword || !confirmPassword) return;
                    var oldVal = oldPassword.value.trim();
                    var newVal = newPassword.value.trim();
                    var confirmVal = confirmPassword.value.trim();
                    if (newVal !== confirmVal) {
                        self.showProfileMessage(
                            "Новый пароль и подтверждение не совпадают.",
                            "error"
                        );
                        return;
                    }
                    if (newVal.length < 10) {
                        self.showProfileMessage(
                            "Новый пароль слишком короткий: минимум 10 символов.",
                            "error"
                        );
                        return;
                    }
                    SettingsAPI.changePassword(
                        self.userData.username,
                        oldVal,
                        newVal
                    )
                        .then(function (result) {
                            self.showProfileMessage(
                                result.message || "Пароль изменён.",
                                "success"
                            );
                            oldPassword.value = "";
                            newPassword.value = "";
                            confirmPassword.value = "";
                        })
                        .catch(function (error) {
                            self.showProfileMessage(error.message, "error");
                        });
                });
            }
        },

        bindScanEvents: function (currentStatus) {
            var self = this;
            var startBtn = document.getElementById("settings-btn-start-scan");
            if (startBtn) {
                startBtn.addEventListener("click", function () {
                    startBtn.disabled = true;
                    SettingsAPI.startScan()
                        .then(function () {
                            self.showScanMessage("Сканирование запущено.", "success");
                            UIStateManager.update({ scan_status: "running" });
                            if (typeof window.DDSApp !== "undefined" &&
                                window.DDSApp.startScanMonitoring) {
                                window.DDSApp.startScanMonitoring();
                            }
                            self.loadSection("scan");
                        })
                        .catch(function (error) {
                            self.showScanMessage(error.message, "error");
                            startBtn.disabled = false;
                            UIStateManager.apply();
                        });
                });
            }
            var cancelBtn = document.getElementById("settings-btn-cancel-scan");
            if (cancelBtn) {
                cancelBtn.addEventListener("click", function () {
                    cancelBtn.disabled = true;
                    SettingsAPI.cancelScan()
                        .then(function (data) {
                            self.showScanMessage(data.message || "Отмена запрошена.", "info");
                            self.loadSection("scan");
                        })
                        .catch(function (error) {
                            self.showScanMessage(error.message, "error");
                            cancelBtn.disabled = false;
                            UIStateManager.apply();
                        });
                });
            }
        },

        bindSystemEvents: function () {
            var self = this;
            var form = document.getElementById("settings-system-form");
            if (form) {
                form.addEventListener("submit", function (e) {
                    e.preventDefault();
                    var rdDirectory = document.getElementById("settings-rd-directory");
                    var dbPath = document.getElementById("settings-db-path");
                    var data = {};
                    if (rdDirectory && rdDirectory.value.trim()) {
                        data.rd_directory = rdDirectory.value.trim();
                    }
                    if (dbPath && dbPath.value.trim()) {
                        data.db_path = dbPath.value.trim();
                    }
                    if (!data.rd_directory && !data.db_path) {
                        self.showSystemMessage("Заполните хотя бы одно поле.", "error");
                        return;
                    }
                    SettingsAPI.updateSettings(data)
                        .then(function (result) {
                            var msgClass = "success";
                            if (result.requires_restart) {
                                msgClass = "info";
                            }
                            self.showSystemMessage(result.message, msgClass);
                        })
                        .catch(function (error) {
                            self.showSystemMessage(error.message, "error");
                        });
                });
            }
        },

        showProfileMessage: function (text, type) {
            var el = document.getElementById("settings-profile-message");
            if (el) {
                el.textContent = text;
                el.className = "settings-message settings-message--" + type;
            }
        },

        showScanMessage: function (text, type) {
            var el = document.getElementById("settings-scan-message");
            if (el) {
                el.textContent = text;
                el.className = "settings-message settings-message--" + type;
            }
        },

        showSystemMessage: function (text, type) {
            var el = document.getElementById("settings-system-message");
            if (el) {
                el.textContent = text;
                el.className = "settings-message settings-message--" + type;
            }
        }
    };

    /* ==============================================================
       5. Вспомогательные функции
       ============================================================== */

    function getScanStatusText(status) {
        switch (status) {
            case "running":
                return "Выполняется";
            case "completed":
                return "Завершено";
            case "interrupted":
                return "Прервано";
            case "error":
                return "Ошибка";
            case "idle":
                return "Ожидание";
            default:
                return status;
        }
    }

    /* ==============================================================
       6. Экспорт для внешних модулей
       ============================================================== */

    if (typeof window !== "undefined") {
        window.SettingsModule = {
            init: function (options) {
                SettingsPanel.init(options);
            },
            open: function () {
                SettingsPanel.open();
            },
            close: function () {
                SettingsPanel.close();
            },
            /**
             * Возвращает текущее состояние панели настроек.
             * Используется другими модулями (например, ``app.js``)
             * для защиты от конфликтов активного состояния иконок
             * Activity Bar.
             *
             * @returns {boolean} — ``true``, если панель открыта.
             */
            isOpen: function () {
                return SettingsPanel.isOpen;
            }
        };
    }
})();
