"""
Pytest fixtures для smoke-тестов Deep Doc Search.

Назначение
----------
Общий setup/teardown для smoke-тестов: изоляция рабочего окружения,
запуск приложения DDS как subprocess, инициализация headless-браузера
через Playwright (sync API), сбор артефактов при падении теста.

Подробное описание методологии, границ ответственности и правил
написания тестов — в ``tests/smoke/__init__.py``.

Архитектура запуска
-------------------
Последовательность, реализуемая fixtures:

1. ``_smoke_resources_root`` — изолированный корневой каталог
   ресурсов (``tmp_path_factory``) с подкаталогами
   ``rd_directory/``, ``modules_directory/``, ``db/``.
2. ``_smoke_config_file`` — минимальный ``smoke-config.json`` с
   путями к временным ресурсам и тестовым пользователем.
   Сохраняется также в ``tests/smoke/smoke-config.json`` для
   артефактов CI.
3. ``dds_app_server`` — запуск ``python run.py`` как subprocess
   на порту ``DDS_SMOKE_PORT`` (8765 по умолчанию); polling
   ``/api/scan/status`` до готовности; сохранение stdout/stderr
   в ``tests/smoke/app.log``. Возвращает ``base_url``.
4. ``playwright`` / ``browser`` — headless Chromium (session-scoped).
5. ``context`` — изолированный browser context на каждый тест
   (свежие cookies, localStorage); tracing с скриншотами и
   снапшотами DOM; сохранение trace при падении.
6. ``page`` — чистая страница для каждого теста; скриншот при
   падении.
7. ``admin_page`` — page, уже авторизованный как администратор
   через форму ``/login``.

Изоляция окружения
------------------
Все ресурсы (SQLite, ``rd_directory``, справочники) — во временных
каталогах. Реальный ``config.json`` проекта **не используется**;
приложение запускается с изолированным smoke-конфигом. Порт 8765
изолирован от production (8000).

Порядок teardown
----------------
Зависимости fixtures задают порядок:

- ``browser`` зависит от ``playwright``;
- ``context`` зависит от ``browser``;
- ``dds_app_server`` — независим от browser; закрывается после
  завершения всех тестов;
- pytest снимает fixtures в обратном порядке (сначала per-test
  контекст и страница, затем browser, затем app_server).

Это гарантирует, что приложение остаётся запущенным на момент
завершения браузерных сессий.

Обработка падений
-----------------
Хук ``pytest_runtest_makereport`` сохраняет результат каждой фазы
(``setup``/``call``/``teardown``) в атрибуты ``item.rep_*``.
Fixtures ``context`` и ``page`` проверяют ``rep_call``/``rep_setup``
в teardown и сохраняют артефакты при падении:

- ``tests/smoke/artifacts/<test_name>/trace.zip`` — Playwright trace;
- ``tests/smoke/artifacts/<test_name>/screenshot.png`` — полный скриншот.

Артефакты загружаются CI-workflow'ом (``smoke.yml``) с ``if: always()``.

Retry
-----
Retry-механизм реализован на уровне workflow
(``nick-invision/retry@v3``), не в этом файле. Дублирование retry
внутри pytest (``pytest-rerunfailures``) увеличило бы время и
замаскировало бы системные проблемы.

Переменные окружения
--------------------
- ``DDS_SMOKE_PORT``           — порт приложения (по умолчанию 8765).
- ``DDS_SMOKE_BASE_URL``       — базовый URL (по умолчанию
                                 ``http://127.0.0.1:<port>``).
- ``DDS_SMOKE_READY_TIMEOUT``  — таймаут ожидания готовности
                                 (по умолчанию 60 секунд).
- ``DDS_SMOKE_HEADLESS``       — ``"0"`` для видимого браузера
                                 при локальной отладке.

Зависимости
-----------
- ``playwright>=1.40.0`` (sync API; без ``pytest-playwright``);
- ``pytest>=7.0.0``.

Модуль не выполняет логирования; диагностика — через stdout/stderr
pytest'а и артефакты.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    ViewportSize,
    sync_playwright,
)

# =====================================================================
# Константы
# =====================================================================

_REPO_ROOT = Path(__file__).resolve().parents[2]
"""Корень репозитория (``tests/smoke/conftest.py`` → ``.../repo``)."""

_SMOKE_DIR = Path(__file__).resolve().parent
"""Каталог ``tests/smoke``."""

_ARTIFACTS_DIR = _SMOKE_DIR / "artifacts"
"""Каталог артефактов падений; перезаписывается на каждой сессии."""

_APP_LOG_PATH = _SMOKE_DIR / "app.log"
"""Путь к логу приложения (stdout+stderr). Обновляется при старте."""

_SMOKE_CONFIG_PATH = _SMOKE_DIR / "smoke-config.json"
"""Путь к smoke-конфигу для артефактов CI."""

_DEFAULT_PORT = 8765
"""Порт приложения по умолчанию (изолирован от production 8000)."""

_DEFAULT_READY_TIMEOUT_SECONDS = 60.0
"""Таймаут ожидания готовности приложения по умолчанию."""

_READY_POLL_INTERVAL_SECONDS = 0.5
"""Интервал polling'а готовности."""

_LOG_TAIL_LINES = 40
"""Количество последних строк app.log в сообщении об ошибке."""

_SMOKE_ADMIN_USERNAME = "smoke_admin"
"""Имя тестового администратора."""

_SMOKE_ADMIN_PASSWORD = "SmokePass1234"
"""Пароль тестового администратора (удовлетворяет политике)."""

_SMOKE_ADMIN_ROLE = "admin"
"""Роль тестового пользователя."""

_DEFAULT_VIEWPORT: ViewportSize = {"width": 1440, "height": 900}
"""Размер viewport по умолчанию для browser context.

Тип ``ViewportSize`` (TypedDict из ``playwright.sync_api``) требует
ровно двух целочисленных полей ``width`` и ``height`` — это
соответствует контракту параметра ``viewport`` метода
``Browser.new_context``. Использование обычного ``dict[str, int]``
приводит к ошибке статического анализа Pylance
(``reportArgumentType``), так как произвольный ``dict`` несовместим
с ``TypedDict``.
"""

_LOGIN_TIMEOUT_MS = 5000
"""Таймаут ожидания элемента главной страницы после логина."""

_MAIN_PAGE_SELECTOR = "#search-form"
"""CSS-селектор элемента, гарантирующего загрузку главной страницы."""

_REFERENCES_TEMPLATE: dict[str, Any] = {
    "objects": {"codes": [], "aliases_en": [], "aliases_ru": []},
    "disciplines": {"codes": [], "aliases_en": [], "aliases_ru": []},
    "documentTypes": {"codes": [], "aliases_en": [], "aliases_ru": []},
}
"""Минимальный шаблон ``references.json`` для smoke-окружения."""

_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
"""Регулярное выражение для санитизации имени теста в пути артефактов."""


# =====================================================================
# Приватные helpers
# =====================================================================


def _make_smoke_config(resources_root: Path, port: int) -> dict[str, Any]:
    """Формирует содержимое ``smoke-config.json``.

    Конфиг описывает изолированное окружение: временную SQLite,
    пустой ``rd_directory``, пустой ``modules_directory``,
    минимальный ``references.json`` и одного администратора.

    Args:
        resources_root: Корневой каталог ресурсов.
        port: Порт для uvicorn.

    Returns:
        Словарь конфигурации приложения.
    """
    return {
        "dds": {
            "db_path": str(resources_root / "db" / "smoke.db"),
            "rd_directory": str(resources_root / "rd_directory"),
            "modules_directory": str(resources_root / "modules_directory"),
            "host": "127.0.0.1",
            "port": port,
            "reference_data_path": str(resources_root / "references.json"),
        },
        "modules": {},
        "auth": {
            "users": [
                {
                    "username": _SMOKE_ADMIN_USERNAME,
                    "password": _SMOKE_ADMIN_PASSWORD,
                    "role": _SMOKE_ADMIN_ROLE,
                },
            ],
            "session_ttl_seconds": 3600,
            "auto_login": {
                "enabled": False,
                "allowed_networks": [],
                "ip_user_map": {},
            },
        },
    }


def _write_smoke_resources(resources_root: Path, port: int) -> Path:
    """Создаёт ``references.json`` и ``smoke-config.json`` на диске.

    ``references.json`` — минимальный шаблон со всеми тремя
    категориями (объекты, дисциплины, типы). Пустые списки
    корректны: ``ReferenceDataInitializer`` заполнит таблицы нулём
    записей, приложение запустится.

    ``smoke-config.json`` записывается в
    :data:`_SMOKE_CONFIG_PATH` (для артефактов CI) и возвращается
    как путь.

    Args:
        resources_root: Корневой каталог ресурсов.
        port: Порт приложения.

    Returns:
        Путь к созданному ``smoke-config.json``.
    """
    # 1. references.json — минимальный шаблон.
    references_path = resources_root / "references.json"
    references_path.write_text(
        json.dumps(_REFERENCES_TEMPLATE, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 2. smoke-config.json — конфиг приложения.
    config = _make_smoke_config(resources_root, port)
    serialized = json.dumps(config, ensure_ascii=False, indent=2)

    # 2a. Персистентная копия для артефактов CI.
    _SMOKE_CONFIG_PATH.write_text(serialized, encoding="utf-8")

    # 2b. Рабочая копия, передаваемая приложению.
    #     Используем ту же, что и 2a — единый источник.
    return _SMOKE_CONFIG_PATH


def _safe_test_name(nodeid: str) -> str:
    """Преобразует pytest-nodeid в безопасное имя каталога.

    Пример: ``test_smoke_playwright.py::test_login_page`` →
    ``test_smoke_playwright.py__test_login_page``.

    Args:
        nodeid: Полный pytest-nodeid.

    Returns:
        Санитизированное имя без служебных символов.
    """
    return _SAFE_NAME_PATTERN.sub("_", nodeid.replace("::", "__"))


def _artifacts_path_for(nodeid: str) -> Path:
    """Создаёт и возвращает каталог артефактов для теста.

    Args:
        nodeid: Полный pytest-nodeid.

    Returns:
        Путь к созданному каталогу ``artifacts/<safe_name>/``.
    """
    path = _ARTIFACTS_DIR / _safe_test_name(nodeid)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_log_tail(path: Path, lines: int) -> str:
    """Возвращает последние ``lines`` строк файла.

    Используется для диагностики при неудачном старте приложения.
    Файл может не существовать или быть нечитаемым — в этом случае
    возвращается плейсхолдер.

    Args:
        path: Путь к файлу.
        lines: Количество последних строк.

    Returns:
        Строка с содержимым (или сообщение об ошибке чтения).
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(не удалось прочитать {path}: {exc})"
    all_lines = content.splitlines()
    return "\n".join(all_lines[-lines:])


def _wait_for_ready(
    proc: subprocess.Popen,
    ready_url: str,
    timeout: float,
    log_path: Path,
) -> None:
    """Ожидает готовности приложения через polling HTTP.

    Готовность = успешный ответ 200 от ``/api/scan/status``.
    При преждевременном завершении subprocess — падение с
    диагностикой (код возврата + хвост лога). При истечении
    таймаута — падение с последней ошибкой соединения + хвост лога.

    Args:
        proc: Процесс приложения.
        ready_url: URL для polling.
        timeout: Таймаут ожидания в секундах.
        log_path: Путь к логу приложения.

    Raises:
        RuntimeError: Если приложение не готово за отведённое время
            или завершилось преждевременно.
    """
    deadline = time.monotonic() + timeout
    last_error: str = "нет попыток"
    while time.monotonic() < deadline:
        # 1. Проверка преждевременного завершения.
        if proc.poll() is not None:
            tail = _read_log_tail(log_path, _LOG_TAIL_LINES)
            raise RuntimeError(
                f"Приложение завершилось с кодом {proc.returncode} "
                f"до готовности. Хвост лога:\n{tail}"
            )
        # 2. HTTP-проверка.
        try:
            with urllib.request.urlopen(ready_url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = str(exc)
        time.sleep(_READY_POLL_INTERVAL_SECONDS)

    tail = _read_log_tail(log_path, _LOG_TAIL_LINES)
    raise RuntimeError(
        f"Приложение не стало готово за {timeout} секунд. "
        f"Последняя ошибка: {last_error}. Хвост лога:\n{tail}"
    )


def _terminate_process(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Корректно завершает процесс.

    Сначала ``terminate`` (SIGTERM на POSIX) — позволяет uvicorn
    выполнить lifespan shutdown. При истечении таймаута — ``kill``
    (SIGKILL). Метод идемпотентен: повторный вызов на завершённом
    процессе — no-op.

    Args:
        proc: Процесс приложения.
        timeout: Таймаут ожидания graceful shutdown.
    """
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            # Процесс не завершается даже после SIGKILL — крайне
            # редкий случай (зависший syscall на уровне ядра).
            # Не блокируем teardown.
            pass


def _login(page: Page, username: str, password: str) -> None:
    """Выполняет вход через форму ``/login``.

    Заполняет поля, отправляет форму, ожидает появления элемента
    главной страницы (:data:`_MAIN_PAGE_SELECTOR`). При неудачном
    входе элемент не появится, и Playwright упадёт по таймауту с
    диагностикой.

    Args:
        page: Playwright-страница.
        username: Имя пользователя.
        password: Пароль.
    """
    page.goto("/login")
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_selector(_MAIN_PAGE_SELECTOR, timeout=_LOGIN_TIMEOUT_MS)


def _is_test_failed(request: pytest.FixtureRequest) -> bool:
    """Определяет, завершился ли тест с ошибкой.

    Проверяет фазы setup и call. Фаза teardown не проверяется:
    к моменту выполнения teardown-хуков fixtures результат уже
    известен, а падение в teardown — отдельная категория.

    Args:
        request: Объект запроса fixture.

    Returns:
        ``True``, если setup или call фаза провалились.
    """
    rep_setup = getattr(request.node, "rep_setup", None)
    rep_call = getattr(request.node, "rep_call", None)
    if rep_setup is not None and rep_setup.failed:
        return True
    if rep_call is not None and rep_call.failed:
        return True
    return False


# =====================================================================
# Pytest hooks
# =====================================================================


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):  # noqa: ANN001
    """Сохраняет результат фазы в ``item.rep_<when>``.

    Стандартный приём: даёт fixtures доступ к результату теста
    (``rep_setup``, ``rep_call``, ``rep_teardown``) для условного
    сохранения артефактов при падении.

    ``tryfirst=True`` гарантирует, что хук выполнится до других
    обработчиков makereport; ``hookwrapper=True`` — что он
    обернёт вызов остальных хуков и получит итоговый report.

    Args:
        item: Тестовый элемент pytest.
        call: Объект вызова фазы.
    """
    outcome = yield
    report = outcome.get_result()
    setattr(item, "rep_" + report.when, report)


# =====================================================================
# Fixtures: изоляция окружения и запуск приложения
# =====================================================================


@pytest.fixture(scope="session")
def _smoke_resources_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Корневой каталог временных ресурсов smoke-окружения.

    Создаётся один раз на сессию через ``tmp_path_factory``.
    pytest автоматически очищает каталог по завершении.

    Подкаталоги:

    - ``rd_directory/``       — пустой каталог РД (без PDF);
    - ``modules_directory/``  — пустой каталог модулей расширений;
    - ``db/``                 — родительский каталог для SQLite.

    Args:
        tmp_path_factory: Встроенная фикстура pytest.

    Returns:
        Путь к корневому каталогу ресурсов.
    """
    root = tmp_path_factory.mktemp("dds_smoke")
    (root / "rd_directory").mkdir()
    (root / "modules_directory").mkdir()
    (root / "db").mkdir()
    return root


@pytest.fixture(scope="session")
def _smoke_config_file(_smoke_resources_root: Path) -> Path:
    """Путь к smoke-конфигу приложения.

    Создаёт ``references.json`` и ``smoke-config.json`` в
    ``_smoke_resources_root``; дополнительно записывает копию
    конфига в :data:`_SMOKE_CONFIG_PATH` для артефактов CI.

    Args:
        _smoke_resources_root: Корневой каталог ресурсов.

    Returns:
        Путь к ``smoke-config.json``.
    """
    port = int(os.environ.get("DDS_SMOKE_PORT", str(_DEFAULT_PORT)))
    return _write_smoke_resources(_smoke_resources_root, port)


@pytest.fixture(scope="session")
def smoke_credentials() -> dict[str, str]:
    """Учётные данные тестового администратора.

    Значения совпадают с пользователем, созданным в
    ``smoke-config.json``. Используются fixture ``admin_page`` и
    тестами, которые проверяют вход.

    Returns:
        Словарь с ключами ``username`` и ``password``.
    """
    return {
        "username": _SMOKE_ADMIN_USERNAME,
        "password": _SMOKE_ADMIN_PASSWORD,
    }


@pytest.fixture(scope="session")
def dds_app_server(_smoke_config_file: Path) -> Iterator[str]:
    """Запускает приложение DDS как subprocess и ждёт готовности.

    Последовательность:

    1. Открытие ``tests/smoke/app.log`` для stdout+stderr subprocess.
    2. Запуск ``python run.py --config <smoke-config.json>`` с
       ``cwd=<repo_root>`` и ``PYTHONUNBUFFERED=1``.
    3. Polling ``/api/scan/status`` до 200 (таймаут
       ``DDS_SMOKE_READY_TIMEOUT``, по умолчанию 60 с).
    4. Возврат ``base_url`` (используется fixtures ``context``).
    5. Teardown — ``terminate`` subprocess и закрытие лог-файла.

    При преждевременном завершении или таймауте — падение с хвостом
    лога в сообщении.

    Args:
        _smoke_config_file: Путь к smoke-конфигу.

    Yields:
        Базовый URL приложения (например, ``http://127.0.0.1:8765``).
    """
    port = int(os.environ.get("DDS_SMOKE_PORT", str(_DEFAULT_PORT)))
    timeout = float(os.environ.get("DDS_SMOKE_READY_TIMEOUT", str(_DEFAULT_READY_TIMEOUT_SECONDS)))
    base_url = os.environ.get("DDS_SMOKE_BASE_URL", f"http://127.0.0.1:{port}")
    ready_url = f"{base_url}/api/scan/status"

    # 1. Лог-файл subprocess (перезаписывается).
    log_fh = _APP_LOG_PATH.open("w", encoding="utf-8")

    # 2. Запуск приложения.
    cmd = [
        sys.executable,
        str(_REPO_ROOT / "run.py"),
        "--config",
        str(_smoke_config_file),
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    proc = subprocess.Popen(  # noqa: S603 — команда формируется в этом модуле
        cmd,
        cwd=str(_REPO_ROOT),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
    )

    try:
        # 3. Ожидание готовности.
        _wait_for_ready(proc, ready_url, timeout, _APP_LOG_PATH)
    except Exception:
        _terminate_process(proc)
        log_fh.close()
        raise

    try:
        yield base_url
    finally:
        # 4. Teardown: graceful shutdown + закрытие лога.
        _terminate_process(proc)
        log_fh.close()


# =====================================================================
# Fixtures: Playwright
# =====================================================================


@pytest.fixture(scope="session")
def playwright() -> Iterator[Playwright]:
    """Session-scoped экземпляр Playwright.

    Используется sync API (не async): smoke-тесты не требуют
    параллельных операций; sync API проще и не конфликтует с
    pytest-asyncio.

    Yields:
        Запущенный :class:`Playwright`.
    """
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def browser(playwright: Playwright) -> Iterator[Browser]:
    """Session-scoped headless Chromium.

    По умолчанию headless. Для локальной отладки задайте
    ``DDS_SMOKE_HEADLESS=0`` — браузер будет видимым.

    Args:
        playwright: Экземпляр Playwright.

    Yields:
        Экземпляр :class:`Browser`.
    """
    headless = os.environ.get("DDS_SMOKE_HEADLESS", "1") != "0"
    browser = playwright.chromium.launch(headless=headless)
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def context(
    browser: Browser,
    dds_app_server: str,
    request: pytest.FixtureRequest,
) -> Iterator[BrowserContext]:
    """Изолированный browser context на каждый тест.

    Каждый тест получает свежий context — свои cookies, localStorage,
    sessionStorage. Это исключает протечки состояния между тестами.

    Включает tracing (скриншоты + снапшоты DOM) для диагностики.
    При падении теста trace сохраняется в
    ``tests/smoke/artifacts/<test_name>/trace.zip``. При успехе —
    отбрасывается.

    Args:
        browser: Экземпляр :class:`Browser`.
        dds_app_server: Базовый URL приложения (для ``base_url``).
        request: Объект запроса fixture (для определения падения).

    Yields:
        :class:`BrowserContext`.
    """
    ctx = browser.new_context(
        base_url=dds_app_server,
        viewport=_DEFAULT_VIEWPORT,
    )
    ctx.tracing.start(screenshots=True, snapshots=True, sources=False)
    try:
        yield ctx
    finally:
        if _is_test_failed(request):
            artifacts = _artifacts_path_for(request.node.nodeid)
            try:
                ctx.tracing.stop(path=str(artifacts / "trace.zip"))
            except Exception:  # noqa: BLE001 — сохранение артефакта не должно маскировать падение
                # Если trace не сохранился, оригинальное падение
                # остаётся приоритетным; ошибка трассировки подавляется.
                ctx.tracing.stop()
        else:
            ctx.tracing.stop()
        ctx.close()


@pytest.fixture
def page(context: BrowserContext, request: pytest.FixtureRequest) -> Iterator[Page]:
    """Чистая страница для каждого теста.

    При падении теста сохраняется full-page скриншот в
    ``tests/smoke/artifacts/<test_name>/screenshot.png``. При
    успехе — скриншот не создаётся.

    Args:
        context: Изолированный browser context.
        request: Объект запроса fixture.

    Yields:
        :class:`Page`.
    """
    p = context.new_page()
    try:
        yield p
    finally:
        if _is_test_failed(request):
            artifacts = _artifacts_path_for(request.node.nodeid)
            try:
                p.screenshot(
                    path=str(artifacts / "screenshot.png"),
                    full_page=True,
                )
            except Exception:  # noqa: BLE001 — сохранение артефакта не должно маскировать падение
                pass
        p.close()


@pytest.fixture
def admin_page(
    page: Page,
    dds_app_server: str,
    smoke_credentials: dict[str, str],
) -> Page:
    """Страница, уже авторизованная как администратор.

    Выполняет вход через форму ``/login``; при успехе на странице
    присутствует элемент ``#search-form`` (см. :func:`_login`).
    Все тесты, работающие с UI после логина, используют эту
    fixture вместо явного логина в теле теста.

    Args:
        page: Чистая страница.
        dds_app_server: Базовый URL (гарантирует порядок setup).
        smoke_credentials: Учётные данные администратора.

    Returns:
        Авторизованная :class:`Page`.
    """
    _login(
        page,
        smoke_credentials["username"],
        smoke_credentials["password"],
    )
    return page
