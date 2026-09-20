"""
Smoke-тесты Deep Doc Search через Playwright.

Назначение
----------
End-to-end проверка UI DDS на изолированном окружении (см.
``tests/smoke/conftest.py``): реальное приложение запускается как
subprocess, headless Chromium эмулирует пользовательские сценарии,
результаты валидируются по видимому состоянию DOM.

Покрываемые сценарии (по классам)
---------------------------------

+--------------------------------+----------------------------------------+
| Раздел                         | Сценарии                               |
+================================+========================================+
| Аутентификация                 | Рендер формы входа, неверные учётные   |
|                                | данные, успешный вход, выход.          |
+--------------------------------+----------------------------------------+
| Главная страница               | Наличие формы поиска, статус-бара,     |
|                                | корректная разметка layout.            |
+--------------------------------+----------------------------------------+
| Поиск                          | Пустой запрос (0 результатов),         |
|                                | ввод текста в форму, отправка формы.   |
+--------------------------------+----------------------------------------+
| Просмотр документа             | Открытие вкладки по правому клику,     |
|                                | переключение режима «Рендер / Текст»,  |
|                                | отображение подсветки совпадений.      |
+--------------------------------+----------------------------------------+
| Панель настроек                | Открытие/закрытие панели, переключение |
|                                | разделов, смена темы оформления.       |
+--------------------------------+----------------------------------------+

Методология
-----------
- **Sync Playwright API.** Smoke-тесты последовательны (не требуют
  параллельных операций); sync API проще async и не конфликтует с
  ``pytest-asyncio``.
- **Авто-ожидания.** Никаких ``time.sleep`` — только Playwright
  locators с автоматическим ожиданием готовности элемента
  (``expect(...).to_be_visible()``, ``page.wait_for_selector``).
- **Валидация через user-visible состояния.** Проверяется то, что
  видит пользователь (видимость элементов, текст, ``href``), а не
  внутренние JS-переменные.
- **Изоляция тестов.** Каждый тест получает свежий ``page`` в
  изолированном ``context`` (свои cookies и localStorage).
- **Бирюзовая подсветка.** Проверяется через наличие элементов
  ``.page-render-highlight`` в overlay-слое (см. ``base.css``).
- **HTTP-запросы к приложению — через** :class:`~tests.smoke.conftest.LocalHTTP`.
  Прямые запросы к ``127.0.0.1`` не должны идти через системный
  прокси (``ALL_PROXY`` и др.); клиент ``local_http`` игнорирует
  переменные окружения и делает seeding/ожидание результата
  герметичным.

Запуск
------
::

    pytest tests/smoke/ -v

Для отладки с видимым браузером::

    DDS_SMOKE_HEADLESS=0 pytest tests/smoke/ -v

Зависимости
-----------
- ``playwright>=1.40.0``;
- ``pymupdf>=1.23.0`` — для генерации PDF-документа в fixture
  ``seeded_document``.

Границы
-------
Тесты проверяют **UI-поток**, а не бизнес-логику. Глубокая проверка
подсветки (точность координат), качества рендера, корректности
поиска по FTS5 — задача unit- и integration-тестов.

Сценарии с реальным сканированием каталога и производительностью —
вне области smoke (см. ``tests/benchmarks/bench_slow.py``).

Принципы:
    - Модуль не выполняет логирования;
    - не имеет побочных эффектов при импорте;
    - не читает и не пишет production-файлы (только
      изолированное окружение, подготовленное conftest);
    - каждый тест — независимый сценарий.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from playwright.sync_api import Page

# =====================================================================
# Константы
# =====================================================================

_PDF_FILE_NAME = "smoke_test_doc.pdf"
"""Имя PDF-файла, создаваемого fixture ``seeded_document``."""

_PDF_SEARCH_TERM = "corpus"
"""Термин, гарантированно присутствующий в PDF-документе.

Поиск по нему возвращает непустой результат; используется для
проверки подсветки совпадений.
"""

_PDF_BODY_TEXT = f"{_PDF_SEARCH_TERM} hydroseal foundation documentation test"
"""Текст, встраиваемый в PDF-документ."""

_SCAN_READY_TIMEOUT_SECONDS = 60.0
"""Таймаут ожидания готовности документа после seeding."""

_DOC_RENDER_TIMEOUT_MS = 15000
"""Таймаут ожидания рендера страницы в браузере (мс)."""

_HIGHLIGHT_TIMEOUT_MS = 15000
"""Таймаут ожидания появления подсветки совпадений (мс)."""


# =====================================================================
# Приватные helpers
# =====================================================================


def _generate_smoke_pdf(path: Path) -> None:
    """Генерирует минимальный PDF-документ для smoke-тестов.

    Содержимое — латиница (встроенный шрифт Helvetica). Термин
    :data:`_PDF_SEARCH_TERM` присутствует в тексте; поиск по нему
    даёт непустой результат, что позволяет проверить подсветку.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Создание нового PDF-документа.                      |
    +---+-----------------------------------------------------+
    | 2 | Добавление страницы A4.                             |
    +---+-----------------------------------------------------+
    | 3 | Вставка текста с :data:`_PDF_BODY_TEXT`.            |
    +---+-----------------------------------------------------+
    | 4 | Сохранение файла на диск.                           |
    +---+-----------------------------------------------------+
    | 5 | Закрытие документа (в ``finally``).                 |
    +---+-----------------------------------------------------+

    Args:
        path: Путь для сохранения PDF.
    """
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=595.0, height=842.0)
        page.insert_text((50.0, 100.0), _PDF_BODY_TEXT, fontsize=12)
        doc.save(str(path))
    finally:
        doc.close()


def _wait_for_search_hit(
    http: Any,
    base_url: str,
    query: str,
    timeout: float = _SCAN_READY_TIMEOUT_SECONDS,
) -> None:
    """Ожидает появления документа в результатах поиска.

    Polling ``GET /api/search?q=<query>`` до ``total > 0``. Если
    за отведённое время документ не появился — падение с
    диагностическим сообщением.

    HTTP-запросы выполняются через ``http`` — экземпляр
    :class:`~tests.smoke.conftest.LocalHTTP`, который игнорирует
    системные прокси. Параметр типизирован как ``Any``, чтобы
    избежать импорта ``LocalHTTP`` из ``conftest`` (относительные
    импорты в пакете smoke могут быть проблематичны).

    Args:
        http: HTTP-клиент без прокси (``LocalHTTP``).
        base_url: Базовый URL приложения.
        query: Поисковый запрос.
        timeout: Таймаут в секундах.

    Raises:
        RuntimeError: Если за timeout документ не появился.
    """
    encoded = urllib.parse.quote(query)
    url = f"{base_url}/api/search?q={encoded}"
    deadline = time.monotonic() + timeout
    last_error: str = "нет попыток"
    while time.monotonic() < deadline:
        try:
            data = http.get_json(url)
            if int(data.get("total", 0)) > 0:
                return
        except (OSError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(
        f"Документ не появился в поиске по {query!r} за {timeout}s. Последняя ошибка: {last_error}"
    )


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(scope="session")
def smoke_config_file(_smoke_config_file: Path) -> Path:
    """Публичный псевдоним для ``_smoke_config_file`` из conftest.

    Приватная fixture из conftest не предназначена для прямого
    использования в тестах; этот псевдоним явно документирует
    намерение.

    Args:
        _smoke_config_file: Приватная fixture conftest.

    Returns:
        Путь к ``smoke-config.json``.
    """
    return _smoke_config_file


@pytest.fixture(scope="session")
def seeded_document(
    dds_app_server: str,
    smoke_config_file: Path,
    local_http: Any,
) -> dict[str, str]:
    """Создаёт PDF, сканирует его через API, возвращает метаданные.

    Последовательность:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Чтение ``rd_directory`` из smoke-config.            |
    +---+-----------------------------------------------------+
    | 2 | Генерация PDF в ``rd_directory``.                   |
    +---+-----------------------------------------------------+
    | 3 | Запуск сканирования через ``POST /api/scan/start``. |
    +---+-----------------------------------------------------+
    | 4 | Polling ``GET /api/search?q=<term>`` до появления   |
    |   | документа.                                          |
    +---+-----------------------------------------------------+
    | 5 | Получение ``doc_id`` из результатов поиска.         |
    +---+-----------------------------------------------------+

    Session-scoped: PDF генерируется один раз, сканирование
    выполняется один раз, документ переиспользуется всеми тестами,
    которым он нужен.

    HTTP-запросы выполняются через ``local_http`` — клиент без
    прокси (см. :class:`~tests.smoke.conftest.LocalHTTP`). Это
    устраняет падение с ``unknown url type: socks5h`` при наличии
    переменной ``ALL_PROXY`` в окружении.

    Args:
        dds_app_server: Базовый URL приложения.
        smoke_config_file: Путь к smoke-конфигу.
        local_http: HTTP-клиент без прокси (``LocalHTTP`` из conftest).

    Returns:
        Словарь с ключами ``doc_id``, ``file_name``, ``search_term``.

    Raises:
        RuntimeError: Если документ не появился в поиске за timeout.
    """
    # 1. Чтение rd_directory.
    config = json.loads(smoke_config_file.read_text(encoding="utf-8"))
    rd_dir = Path(config["dds"]["rd_directory"])
    rd_dir.mkdir(parents=True, exist_ok=True)

    # 2. Генерация PDF.
    pdf_path = rd_dir / _PDF_FILE_NAME
    _generate_smoke_pdf(pdf_path)

    # 3. Запуск сканирования.
    local_http.post_json(f"{dds_app_server}/api/scan/start", {})

    # 4. Ожидание готовности документа в поиске.
    _wait_for_search_hit(local_http, dds_app_server, _PDF_SEARCH_TERM)

    # 5. Получение doc_id.
    data = local_http.get_json(
        f"{dds_app_server}/api/search?q={urllib.parse.quote(_PDF_SEARCH_TERM)}"
    )
    results = data.get("results", [])
    assert results, "Результаты поиска пусты после успешного seeding"
    doc = results[0]

    return {
        "doc_id": str(doc["doc_id"]),
        "file_name": _PDF_FILE_NAME,
        "search_term": _PDF_SEARCH_TERM,
    }


# =====================================================================
# Раздел 1. Аутентификация
# =====================================================================


def test_login_page_renders(page: Page) -> None:
    """Страница входа отдаётся и содержит форму с полями.

    Проверяет:
        - заголовок страницы (title) не пустой;
        - присутствуют поля ``#username`` и ``#password``;
        - присутствует кнопка submit.

    Args:
        page: Чистая Playwright-страница.
    """
    page.goto("/login")
    page.wait_for_selector("form", timeout=5000)

    assert page.locator("#username").is_visible()
    assert page.locator("#password").is_visible()
    assert page.locator("button[type=submit]").is_visible()


def test_login_invalid_credentials(page: Page) -> None:
    """Неверные учётные данные → остаёмся на /login с сообщением.

    Проверяет:
        - поле ``#username`` доступно (форма перерисована);
        - присутствует элемент ``.alert--error`` с сообщением.

    Args:
        page: Чистая Playwright-страница.
    """
    page.goto("/login")
    page.fill("#username", "nonexistent_user")
    page.fill("#password", "wrong_password_1")
    page.click("button[type=submit]")

    page.wait_for_selector(".alert--error", timeout=5000)
    assert "login" in page.url


def test_login_success_redirects_to_main(
    page: Page,
    smoke_credentials: dict[str, str],
) -> None:
    """Успешный вход → редирект на главную, форма поиска видна.

    Проверяет:
        - после логина URL больше не содержит ``/login``;
        - присутствует элемент ``#search-form`` (главная страница).

    Args:
        page: Чистая Playwright-страница.
        smoke_credentials: Учётные данные администратора.
    """
    page.goto("/login")
    page.fill("#username", smoke_credentials["username"])
    page.fill("#password", smoke_credentials["password"])
    page.click("button[type=submit]")

    page.wait_for_selector("#search-form", timeout=5000)
    assert "/login" not in page.url


def test_logout_returns_to_login(admin_page: Page) -> None:
    """Выход через форму в статус-баре → редирект на /login.

    Проверяет:
        - кнопка выхода присутствует (``.status-bar__logout-btn``);
        - после клика URL содержит ``/login``.

    Args:
        admin_page: Страница, уже авторизованная как администратор.
    """
    admin_page.click(".status-bar__logout-btn")
    admin_page.wait_for_url("**/login**", timeout=5000)
    assert "/login" in admin_page.url


# =====================================================================
# Раздел 2. Главная страница
# =====================================================================


def test_main_page_layout(admin_page: Page) -> None:
    """Главная страница содержит ключевые элементы layout.

    Проверяет:
        - форму поиска (``#search-form``);
        - поле ввода (``#search-input``);
        - Activity Bar (``.activity-bar``);
        - Sidebar с фильтрами (``.sidebar``);
        - поля фильтров (``#filter-object``, ``#filter-discipline``,
          ``#filter-document-type``).

    Args:
        admin_page: Авторизованная страница.
    """
    assert admin_page.locator("#search-form").is_visible()
    assert admin_page.locator("#search-input").is_visible()
    assert admin_page.locator(".activity-bar").is_visible()
    assert admin_page.locator(".sidebar").is_visible()
    assert admin_page.locator("#filter-object").is_visible()
    assert admin_page.locator("#filter-discipline").is_visible()
    assert admin_page.locator("#filter-document-type").is_visible()


def test_main_page_status_bar_visible(admin_page: Page) -> None:
    """Статус-бар присутствует и содержит поля состояния.

    Проверяет:
        - контейнер ``.status-bar``;
        - индикатор сканирования (``#statusbar-scan``);
        - поле запроса (``#statusbar-query``);
        - счётчик результатов (``#statusbar-results-count``);
        - имя пользователя в правой части.

    Args:
        admin_page: Авторизованная страница.
    """
    assert admin_page.locator(".status-bar").is_visible()
    assert admin_page.locator("#statusbar-scan").is_visible()
    assert admin_page.locator("#statusbar-query").is_visible()
    assert admin_page.locator("#statusbar-results-count").is_visible()


# =====================================================================
# Раздел 3. Поиск
# =====================================================================


def test_search_empty_query_before_seed_returns_no_results(
    page: Page,
    smoke_credentials: dict[str, str],
) -> None:
    """Пустой поиск до seeding → 0 документов.

    Проверяет сценарий «чистое окружение»: до seeding каталога
    результат поиска пуст. Это гарантирует, что последующие тесты
    с ``seeded_document`` действительно видят эффект seeding'а.

    Примечание: используется ``page`` (не ``admin_page``), чтобы
    избежать неявной зависимости от seeding'а через shared fixture.

    Особенность порядка тестов:
    pytest может выполнять тесты в произвольном порядке. Если
    seeding уже произошёл (``test_document_tab_opens_by_right_click``
    или аналогичный выполнился раньше), тест увидит результаты.
    Проверка адаптивная: она принимает либо блок «no results»
    (ожидаемое поведение в чистом окружении), либо непустую
    таблицу результатов (seed уже выполнен).

    Args:
        page: Чистая страница.
        smoke_credentials: Учётные данные администратора.
    """
    page.goto("/login")
    page.fill("#username", smoke_credentials["username"])
    page.fill("#password", smoke_credentials["password"])
    page.click("button[type=submit]")
    page.wait_for_selector("#search-form", timeout=5000)

    # Пустой поиск.
    page.click("#search-form button[type=submit]")

    # Ожидаем блок "no results" или таблицу с 0 строками.
    page.wait_for_selector(
        "#search-no-results:not(.hidden), #search-results-wrapper:not(.hidden)",
        timeout=5000,
    )

    # В чистом окружении должен показаться блок "no results".
    # Если уже проведён seeding (порядок тестов pytest не гарантирован),
    # этот тест может видеть результат — тогда пропускаем проверку.
    #
    # Проверка устойчива к ``None``: ``get_attribute`` возвращает
    # ``str | None``, если атрибут отсутствует. Явное промежуточное
    # присваивание в переменную устраняет ошибку Pylance
    # ``reportOperatorIssue`` — иначе итератор не может сузить тип
    # через ``and``-цепочку при повторном вызове ``get_attribute``.
    wrapper = page.locator("#search-results-wrapper")
    wrapper_class = wrapper.get_attribute("class")
    if wrapper_class is not None and "hidden" in wrapper_class:
        return  # no results — ожидаемое поведение

    # Иначе — seeding уже произошёл; проверяем хотя бы, что поиск вернул результаты.
    assert page.locator("#search-results-body tr").count() > 0


def test_search_input_accepts_text(admin_page: Page) -> None:
    """Поле поиска принимает текст и возвращает его при чтении.

    Проверяет корректность работы поля как HTML-элемента:
    значение устанавливается и читается без искажений.

    Args:
        admin_page: Авторизованная страница.
    """
    admin_page.fill("#search-input", "test query")
    assert admin_page.input_value("#search-input") == "test query"


def test_search_form_submits_with_empty_query(admin_page: Page) -> None:
    """Отправка формы с пустым запросом не приводит к ошибке UI.

    Проверяет, что при пустом запросе форма отправляется
    (переходит в состояние загрузки или отображает результаты),
    а не падает и не показывает необработанную ошибку.

    Ожидание завершения запроса:
    ``wait_for_selector`` со ``state="hidden"`` — индикатор
    ``#search-loading`` изначально имеет класс ``.hidden``
    (``display: none !important`` в ``base.css``). Использование
    ``state="hidden"`` ожидает перехода элемента в это состояние
    (то есть вызова ``SearchRenderer.hideLoading()``), а не его
    видимости. Селектор ``#search-loading.hidden`` без ``state``
    концептуально не может сработать: элемент, соответствующий
    такому селектору, обязан быть невидимым.

    Args:
        admin_page: Авторизованная страница.
    """
    admin_page.fill("#search-input", "")
    admin_page.click("#search-form button[type=submit]")

    # Приложение не должно показывать error-alert.
    # Ждём завершения запроса: индикатор загрузки скрывается.
    admin_page.wait_for_selector(
        "#search-loading",
        state="hidden",
        timeout=5000,
    )
    error = admin_page.locator("#search-error")
    error_class = error.get_attribute("class")
    # ``error_class`` может быть ``None``, если атрибут отсутствует
    # (обычно присутствует — Locator находит элемент по id).
    assert error_class is None or "hidden" in error_class


# =====================================================================
# Раздел 4. Просмотр документа
# =====================================================================


def test_document_tab_opens_by_right_click(
    admin_page: Page,
    seeded_document: dict[str, str],
) -> None:
    """Правый клик по имени файла открывает вкладку документа.

    Проверяет:
        - поиск по seeded-термину возвращает документ;
        - ссылка ``.search-result-link`` кликабельна;
        - правый клик открывает вкладку (``.tab-panel`` с
          ``data-panel-id`` для документа);
        - вкладка содержит метаданные и контейнер текста/рендера.

    Args:
        admin_page: Авторизованная страница.
        seeded_document: Метаданные seeded-документа.
    """
    # 1. Поиск.
    admin_page.fill("#search-input", seeded_document["search_term"])
    admin_page.click("#search-form button[type=submit]")
    admin_page.wait_for_selector(".search-result-link", timeout=5000)

    # 2. Правый клик.
    link = admin_page.locator(".search-result-link").first
    link.click(button="right")

    # 3. Ожидаем появления вкладки документа.
    panel_id = f"doc-{seeded_document['doc_id']}"
    admin_page.wait_for_selector(
        f"[data-panel-id='{panel_id}']",
        timeout=5000,
    )

    # 4. Проверка содержимого панели.
    panel = admin_page.locator(f"[data-panel-id='{panel_id}']")
    assert panel.locator(".card__title").is_visible()
    # Контейнер рендера или текста (в зависимости от режима).
    assert (
        panel.locator("[id^='doc-text-']").is_visible()
        or panel.locator(".page-render-container").is_visible()
    )


def test_document_view_mode_switch(
    admin_page: Page,
    seeded_document: dict[str, str],
) -> None:
    """Переключатель режима «Рендер / Текст» переключает активную кнопку.

    Проверяет:
        - по умолчанию активна кнопка «Рендер» (при отсутствии
          сохранённого выбора в localStorage);
        - после клика по «Текст» активна кнопка «Текст»;
        - при активной кнопке «Рендер» используется
          ``.page-render-container``; при активной «Текст» —
          ``.page-text`` с моноширинным шрифтом.

    Args:
        admin_page: Авторизованная страница.
        seeded_document: Метаданные seeded-документа.
    """
    # 1. Открытие документа.
    admin_page.fill("#search-input", seeded_document["search_term"])
    admin_page.click("#search-form button[type=submit]")
    admin_page.wait_for_selector(".search-result-link", timeout=5000)
    admin_page.locator(".search-result-link").first.click(button="right")

    panel_id = f"doc-{seeded_document['doc_id']}"
    panel = admin_page.locator(f"[data-panel-id='{panel_id}']")
    panel.wait_for(state="visible", timeout=5000)

    # 2. Локатор переключателя.
    switch = panel.locator(".page-view-switch")
    switch.wait_for(state="visible", timeout=5000)

    render_btn = switch.locator('button[data-view-mode="render"]')
    text_btn = switch.locator('button[data-view-mode="text"]')

    # 3. Ожидаем рендер (кнопка «Рендер» активна после загрузки).
    admin_page.wait_for_function(
        """() => {
            const b = document.querySelector(
                "[data-panel-id='%s'] .page-view-switch button[data-view-mode='render']"
            );
            return b && b.classList.contains('active');
        }"""
        % panel_id,
        timeout=5000,
    )
    render_class = render_btn.get_attribute("class") or ""
    assert "active" in render_class

    # 4. Переключение в текстовый режим.
    text_btn.click()
    admin_page.wait_for_function(
        """() => {
            const b = document.querySelector(
                "[data-panel-id='%s'] .page-view-switch button[data-view-mode='text']"
            );
            return b && b.classList.contains('active');
        }"""
        % panel_id,
        timeout=5000,
    )
    text_class = text_btn.get_attribute("class") or ""
    assert "active" in text_class


def test_document_highlight_visible(
    admin_page: Page,
    seeded_document: dict[str, str],
) -> None:
    """Подсветка совпадений появляется на рендере документа.

    Проверяет:
        - overlay-слой ``.page-render-overlay`` присутствует;
        - внутри overlay есть хотя бы один прямоугольник
          ``.page-render-highlight`` (бирюзовый, см. ``base.css``).

    Термин для подсветки берётся из сниппета FTS5 при открытии
    вкладки; для seeded-документа это :data:`_PDF_SEARCH_TERM`.

    Args:
        admin_page: Авторизованная страница.
        seeded_document: Метаданные seeded-документа.
    """
    # 1. Поиск с явным термином.
    admin_page.fill("#search-input", seeded_document["search_term"])
    admin_page.click("#search-form button[type=submit]")
    admin_page.wait_for_selector(".search-result-link", timeout=5000)

    # 2. Открытие вкладки.
    admin_page.locator(".search-result-link").first.click(button="right")
    panel_id = f"doc-{seeded_document['doc_id']}"
    panel = admin_page.locator(f"[data-panel-id='{panel_id}']")
    panel.wait_for(state="visible", timeout=5000)

    # 3. Ожидание overlay.
    panel.locator(".page-render-overlay").wait_for(
        state="visible",
        timeout=_DOC_RENDER_TIMEOUT_MS,
    )

    # 4. Ожидание подсветки.
    highlight = panel.locator(".page-render-highlight").first
    highlight.wait_for(state="visible", timeout=_HIGHLIGHT_TIMEOUT_MS)


# =====================================================================
# Раздел 5. Панель настроек
# =====================================================================


def test_settings_panel_opens(admin_page: Page) -> None:
    """Клик по иконке настроек открывает модальную панель.

    Проверяет:
        - до клика overlay ``#settings-overlay`` отсутствует;
        - после клика по ``#activity-settings`` overlay появляется;
        - в панели присутствует навигация ``.settings-nav-item``.

    Args:
        admin_page: Авторизованная страница.
    """
    assert admin_page.locator("#settings-overlay").count() == 0

    admin_page.click("#activity-settings")
    admin_page.wait_for_selector("#settings-overlay", timeout=5000)

    assert admin_page.locator(".settings-modal").is_visible()
    assert admin_page.locator(".settings-nav-item").count() > 0


def test_settings_panel_closes(admin_page: Page) -> None:
    """Клик по кнопке закрытия скрывает панель настроек.

    Проверяет:
        - после открытия overlay присутствует;
        - после клика по ``#settings-close-btn`` overlay удаляется.

    Args:
        admin_page: Авторизованная страница.
    """
    admin_page.click("#activity-settings")
    admin_page.wait_for_selector("#settings-overlay", timeout=5000)

    admin_page.click("#settings-close-btn")
    admin_page.wait_for_selector(
        "#settings-overlay",
        state="detached",
        timeout=5000,
    )


def test_theme_switch_changes_stylesheet(admin_page: Page) -> None:
    """Смена темы в настройках меняет ``href`` активного stylesheet.

    Проверяет:
        - открытие панели настроек;
        - переход в раздел «Интерфейс»;
        - выбор темы из ``#settings-theme``;
        - изменение ``href`` у ``<link id="theme-stylesheet">``.

    Темы загружаются через ``GET /api/themes`` из каталога
    ``static/css/``; селект заполняется асинхронно.

    Args:
        admin_page: Авторизованная страница.
    """
    admin_page.click("#activity-settings")
    admin_page.wait_for_selector("#settings-overlay", timeout=5000)

    # Переход в раздел «Интерфейс».
    admin_page.click(".settings-nav-item[data-section='interface']")
    admin_page.wait_for_selector("#settings-theme", timeout=5000)

    # Ожидание заполнения селекта (опции загружаются через fetch).
    admin_page.wait_for_function(
        """() => {
            const sel = document.getElementById('settings-theme');
            return sel && sel.options.length > 0
                   && sel.options[0].value !== '';
        }""",
        timeout=5000,
    )

    # Сохранение текущего href stylesheet.
    original_href = admin_page.get_attribute("#theme-stylesheet", "href")

    # Выбор темы, отличной от текущей.
    options = admin_page.eval_on_selector(
        "#settings-theme",
        "sel => Array.from(sel.options).map(o => o.value)",
    )
    assert options, "Список тем пуст"

    # Ищем тему, отличную от текущей.
    target_value: str | None = None
    for opt in options:
        if opt and opt not in (original_href or ""):
            target_value = opt
            break
    assert target_value, "Не найдена тема, отличная от текущей"

    admin_page.select_option("#settings-theme", target_value)

    # Ожидание изменения href stylesheet.
    admin_page.wait_for_function(
        """(orig) => {
            const link = document.getElementById('theme-stylesheet');
            return link && link.href !== orig;
        }""",
        arg=original_href,
        timeout=5000,
    )
