"""
Тесты FastAPI dependency-функций ``get_context`` и ``get_auth_service``.

Назначение
----------
Проверка контракта DI-функций, введённых в Фазе 2 (удаление Service
Locator):

- ``dds_web.api.get_context(request)`` — читает
  ``request.app.state.api_context``; при отсутствии или ``None``
  выбрасывает ``HTTPException(503)``.
- ``dds_web.auth.get_auth_service(request)`` — читает
  ``request.app.state.auth_service``; при отсутствии или ``None``
  выбрасывает ``HTTPException(503)``.

Функции являются точкой интеграции presentation layer с
lifespan-инициализацией: значения ``api_context`` и ``auth_service``
сохраняются в ``app.state`` при старте приложения, а читаются
FastAPI в момент обработки каждого запроса.

Проверяемые сценарии
--------------------

Unit-тесты (6):

+--------------------------------+-----------------------------------+
| Сценарий                       | Ожидаемый результат               |
+================================+===================================+
| ``app.state.api_context``      | Функция возвращает этот объект    |
| задан (не ``None``)            | без изменений                     |
+--------------------------------+-----------------------------------+
| ``app.state.api_context``      | ``HTTPException(503)`` с          |
| отсутствует (атрибут не задан) | осмысленным сообщением            |
+--------------------------------+-----------------------------------+
| ``app.state.api_context``      | ``HTTPException(503)``            |
| установлен в ``None``          |                                   |
+--------------------------------+-----------------------------------+
| ``app.state.auth_service``     | Функция возвращает этот объект    |
| задан                           | без изменений                     |
+--------------------------------+-----------------------------------+
| ``app.state.auth_service``     | ``HTTPException(503)``            |
| отсутствует                     |                                   |
+--------------------------------+-----------------------------------+
| ``app.state.auth_service``     | ``HTTPException(503)``            |
| установлен в ``None``          |                                   |
+--------------------------------+-----------------------------------+

Интеграционные тесты (2):

- ``app.dependency_overrides[get_context]`` подменяет контекст для
  всех эндпоинтов, использующих ``CtxDep``. Проверяется через
  минимальный FastAPI-эндпоинт + ``TestClient``.
- ``app.dependency_overrides[get_auth_service]`` подменяет сервис
  аутентификации для всех эндпоинтов, использующих ``AuthDep``.

Интеграционные тесты требуют ``httpx`` (транзитивная зависимость
``starlette.testclient.TestClient``). При отсутствии ``httpx``
они скипаются через :func:`pytest.importorskip`.

Подавление предупреждений сторонних библиотек
---------------------------------------------

При использовании ``TestClient`` с текущими версиями
``starlette`` / ``anyio`` возникают два deprecation-предупреждения,
не относящихся к коду DDS:

- ``StarletteDeprecationWarning``: «Using ``httpx`` with
  ``starlette.testclient`` is deprecated; install ``httpx2``
  instead» — из ``fastapi/testclient.py``.
- ``DeprecationWarning``: «The ``anyio.abc.BlockingPortal``
  alias is deprecated, use ``anyio.from_thread.BlockingPortal``
  instead» — из ``starlette/testclient.py``.

Оба срабатывают в момент импорта ``fastapi.testclient``.
Подавляются через ``@pytest.mark.filterwarnings`` **локально**
на двух интеграционных тестах — unit-тесты, не использующие
``TestClient``, не затрагиваются. Глобальное подавление в
``pyproject.toml`` отклонено: маскировало бы deprecation-сигналы
в других тестах.

Стратегия тестирования
----------------------
- **``SimpleNamespace`` вместо реального ``Request``.** Функции
  ``get_context`` / ``get_auth_service`` обращаются только к
  ``request.app.state``. ``SimpleNamespace`` с полем ``app.state``
  достаточно для проверки контракта и не требует запуска
  ASGI-стека. Для mypy fake аннотирован ``Any`` — функция не
  проверяет тип ``Request`` в runtime.
- **Реальные экземпляры ``AuthService`` в unit-тестах.** Создание
  ``AuthService(session_ttl_seconds=...)`` не требует внешних
  ресурсов. Тест проверяет, что функция возвращает именно тот
  объект, который был помещён в ``app.state``.
- **``SimpleNamespace`` для ``APIContext``.** Конструктор
  ``APIContext`` требует ``SearchEngine``, ``ScanOrchestrator``,
  ``ModuleLifecycle`` — тяжёлые зависимости, не влияющие на
  контракт DI-функции. ``get_context`` не выполняет
  ``isinstance``-проверок, поэтому подставной объект подходит.
- **``TestClient`` для интеграции.** Через минимальный FastAPI
  с одним эндпоинтом: ``@app.get("/probe")`` с параметром
  ``ctx: CtxDep`` (или ``auth: AuthDep``). Override подменяет
  зависимость, эндпоинт возвращает маркер из подставленного
  объекта. Проверка через HTTP-ответ — что override действительно
  применён на уровне фреймворка.
- **Module-level imports для ``CtxDep`` / ``AuthDep``.**
  Эндпоинты-заглушки определяются внутри тестовых функций, но
  их аннотации ссылаются на ``CtxDep`` / ``AuthDep``. Модуль
  содержит ``from __future__ import annotations``, поэтому
  аннотации хранятся как строки; FastAPI вызывает
  ``typing.get_type_hints`` для их разрешения и использует
  ``func.__globals__`` (модульные глобалы). Локальные импорты
  внутри тестовой функции **не попадают** в ``__globals__`` →
  forward reference не разрешается → FastAPI трактует параметр
  как query/body → 422. Поэтому ``CtxDep`` и ``AuthDep``
  импортируются на уровне модуля. Импорты ``FastAPI`` и
  ``TestClient`` остаются локальными: они не участвуют в
  аннотациях эндпоинтов.

Границы
-------
- **Lifespan** (установка значений в ``app.state``) — вне области:
  тестируется smoke-тестами и интеграционным сценарием запуска
  ``run.py``.
- **Эндпоинты** ``api.py`` — их контракт не менялся (используют
  ``CtxDep``), тестируются существующими интеграционными тестами
  и smoke-тестами.
- **Парольная политика** ``AuthService`` — покрыта
  ``test_auth_lifecycle.py``.

Запуск
------
::

    pytest tests/test_api_context_dependency.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет файлы;
    - не поднимает реальный ASGI-сервер (кроме ``TestClient``);
    - каждый тест изолирован, не зависит от порядка выполнения;
    - тесты детерминированы: одинаковый вход → одинаковый результат;
    - предупреждения сторонних библиотек подавляются точечно
      (см. раздел «Подавление предупреждений»).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from dds_web.api import CtxDep, get_context
from dds_web.auth import AuthDep, AuthService, get_auth_service
from fastapi import HTTPException

# =====================================================================
# Helpers
# =====================================================================


def _make_request(app_state: Any) -> Any:
    """Создаёт минимальный Request-подобный объект с ``app.state``.

    ``get_context`` и ``get_auth_service`` обращаются только к
    ``request.app.state``. ``SimpleNamespace`` с полем ``app.state``
    полностью удовлетворяет этот контракт и не требует запуска
    ASGI-стека.

    Возвращаемый объект аннотирован ``Any``: функции принимают
    ``fastapi.Request`` статически, но в runtime не проверяют тип.
    Такая аннотация позволяет вызывать DI-функции напрямую без
    ``cast`` и не вводить шум в статический анализ.

    Args:
        app_state: Пространство имён, эмулирующее ``app.state``.

    Returns:
        Объект с полем ``app.state``.
    """
    app = SimpleNamespace(state=app_state)
    return SimpleNamespace(app=app)


# =====================================================================
# Раздел 1. get_context
# =====================================================================


def test_get_context_returns_context_from_app_state() -> None:
    """``get_context`` возвращает объект из ``app.state.api_context``.

    Проверяет базовый контракт: функция-зависимость читает значение
    из ``app.state`` и возвращает его без изменений. Используется
    ``SimpleNamespace`` как stand-in для ``APIContext``: ``get_context``
    не выполняет ``isinstance``-проверок и возвращает объект как есть.
    """
    sentinel = SimpleNamespace(marker="api-context-sentinel")
    state = SimpleNamespace(api_context=sentinel)

    result = get_context(_make_request(state))

    assert result is sentinel


def test_get_context_raises_503_when_attribute_missing() -> None:
    """Отсутствие ``app.state.api_context`` → ``HTTPException 503``.

    Сценарий: ``app.state`` не содержит атрибут ``api_context``
    (например, lifespan не выполнен, либо эндпоинт вызван до
    startup). Ожидается явный 503 с осмысленным сообщением.
    """
    state = SimpleNamespace()  # без api_context

    with pytest.raises(HTTPException) as exc_info:
        get_context(_make_request(state))

    assert exc_info.value.status_code == 503
    assert "не инициализировано" in str(exc_info.value.detail).lower()


def test_get_context_raises_503_when_context_is_none() -> None:
    """``app.state.api_context is None`` → ``HTTPException 503``.

    Сценарий: атрибут установлен, но в ``None`` (например, после
    shutdown или в тестовом окружении, где контекст не создан).
    Поведение идентично отсутствию атрибута — явный 503.
    """
    state = SimpleNamespace(api_context=None)

    with pytest.raises(HTTPException) as exc_info:
        get_context(_make_request(state))

    assert exc_info.value.status_code == 503


# =====================================================================
# Раздел 2. get_auth_service
# =====================================================================


def test_get_auth_service_returns_service_from_app_state() -> None:
    """``get_auth_service`` возвращает объект из ``app.state.auth_service``.

    Проверяет базовый контракт: функция возвращает именно тот
    экземпляр ``AuthService``, который был помещён в ``app.state``.
    Используется реальный ``AuthService`` — его конструктор не
    требует внешних ресурсов.
    """
    real_auth = AuthService(session_ttl_seconds=999)
    state = SimpleNamespace(auth_service=real_auth)

    result = get_auth_service(_make_request(state))

    assert result is real_auth
    assert result.session_ttl == 999


def test_get_auth_service_raises_503_when_attribute_missing() -> None:
    """Отсутствие ``app.state.auth_service`` → ``HTTPException 503``.

    Сценарий: ``app.state`` не содержит атрибут ``auth_service``
    (lifespan не выполнен). Ожидается явный 503.
    """
    state = SimpleNamespace()  # без auth_service

    with pytest.raises(HTTPException) as exc_info:
        get_auth_service(_make_request(state))

    assert exc_info.value.status_code == 503
    assert "не инициализирован" in str(exc_info.value.detail).lower()


def test_get_auth_service_raises_503_when_service_is_none() -> None:
    """``app.state.auth_service is None`` → ``HTTPException 503``.

    Сценарий: атрибут установлен, но в ``None``. Поведение идентично
    отсутствию атрибута.
    """
    state = SimpleNamespace(auth_service=None)

    with pytest.raises(HTTPException) as exc_info:
        get_auth_service(_make_request(state))

    assert exc_info.value.status_code == 503


# =====================================================================
# Раздел 3. Интеграция через app.dependency_overrides
# =====================================================================


@pytest.mark.filterwarnings(r"ignore:Using `httpx` with `starlette\.testclient` is deprecated")
@pytest.mark.filterwarnings(r"ignore:The anyio\.abc\.BlockingPortal alias is deprecated")
def test_dependency_override_for_get_context() -> None:
    """``app.dependency_overrides[get_context]`` подменяет контекст.

    Проверяет, что FastAPI корректно применяет подмену
    dependency-функции ``get_context`` на уровне приложения.
    Сценарий:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Создаётся минимальный ``FastAPI`` без lifespan.     |
    +---+-----------------------------------------------------+
    | 2 | Регистрируется эндпоинт ``GET /probe``, зависящий   |
    |   | от ``CtxDep`` (``Annotated[APIContext,              |
    |   | Depends(get_context)]``).                            |
    +---+-----------------------------------------------------+
    | 3 | В ``app.dependency_overrides`` устанавливается       |
    |   | подмена ``get_context`` → ``lambda: sentinel``.      |
    +---+-----------------------------------------------------+
    | 4 | ``TestClient`` выполняет запрос. FastAPI вызывает   |
    |   | подмену вместо реальной ``get_context`` (которая    |
    |   | вернула бы 503, так как ``app.state.api_context``   |
    |   | не задан).                                          |
    +---+-----------------------------------------------------+
    | 5 | Эндпоинт возвращает ``id(ctx)``.                    |
    +---+-----------------------------------------------------+
    | 6 | Проверка: статус 200, ``id`` совпадает с ``id``    |
    |   | подставленного объекта — значит, FastAPI передал   |
    |   | именно его.                                         |
    +---+-----------------------------------------------------+

    Проверка идентичности через ``id()`` семантически точнее
    и типизированно корректна: обращение к произвольному
    атрибуту stand-in объекта (например, ``ctx.marker``) не
    проходит статический анализ, так как ``ctx`` типизирован
    как ``APIContext``.

    Примечание:
        ``CtxDep`` импортируется на уровне модуля: аннотация
        ``ctx: CtxDep`` внутри вложенной функции разрешается
        FastAPI через ``typing.get_type_hints``, который ищет имя
        в ``func.__globals__``. При локальном импорте forward
        reference не разрешается, и FastAPI трактует ``ctx`` как
        query-параметр — ответ 422.

    Примечание:
        ``httpx`` — транзитивная зависимость
        ``starlette.testclient.TestClient``. При её отсутствии
        тест скипается через :func:`pytest.importorskip`.

    Примечание:
        Маркеры ``@pytest.mark.filterwarnings`` подавляют два
        deprecation-предупреждения, исходящих из кода
        ``starlette.testclient`` при импорте (см. раздел
        «Подавление предупреждений» в docstring модуля). Оба
        предупреждения не относятся к коду DDS и не влияют на
        проверяемый контракт; подавление действует только в
        этом тесте.
    """
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/probe")
    async def probe(ctx: CtxDep) -> dict:
        return {"id": id(ctx)}

    sentinel = SimpleNamespace(marker="context-override-ok")
    app.dependency_overrides[get_context] = lambda: sentinel

    with TestClient(app) as client:
        response = client.get("/probe")

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": id(sentinel)}


@pytest.mark.filterwarnings(r"ignore:Using `httpx` with `starlette\.testclient` is deprecated")
@pytest.mark.filterwarnings(r"ignore:The anyio\.abc\.BlockingPortal alias is deprecated")
def test_dependency_override_for_get_auth_service() -> None:
    """``app.dependency_overrides[get_auth_service]`` подменяет сервис.

    Проверяет, что FastAPI корректно применяет подмену
    dependency-функции ``get_auth_service`` на уровне приложения.
    Используется реальный ``AuthService`` с уникальным
    ``session_ttl`` — маркером, отличающим подставленный экземпляр
    от возможного production-варианта.

    Сценарий аналогичен :func:`test_dependency_override_for_get_context`:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Минимальный ``FastAPI`` без lifespan.               |
    +---+-----------------------------------------------------+
    | 2 | Эндпоинт ``GET /probe`` с параметром ``auth:         |
    |   | AuthDep`` (``Annotated[AuthService,                  |
    |   | Depends(get_auth_service)]``).                       |
    +---+-----------------------------------------------------+
    | 3 | В ``app.dependency_overrides`` — подмена             |
    |   | ``get_auth_service`` → ``lambda: fake_auth``.        |
    +---+-----------------------------------------------------+
    | 4 | ``TestClient`` выполняет запрос.                     |
    +---+-----------------------------------------------------+
    | 5 | Эндпоинт возвращает ``fake_auth.session_ttl``        |
    |   | (уникальное значение 4242).                          |
    +---+-----------------------------------------------------+
    | 6 | Проверка: статус 200, значение ``session_ttl``       |
    |   | совпадает с ожидаемым.                               |
    +---+-----------------------------------------------------+

    Примечание:
        ``AuthDep`` импортируется на уровне модуля — по той же
        причине, что и ``CtxDep`` в предыдущем тесте: forward
        reference в аннотации вложенной функции разрешается через
        ``func.__globals__``.

    Примечание:
        ``httpx`` — транзитивная зависимость
        ``starlette.testclient.TestClient``. При её отсутствии
        тест скипается через :func:`pytest.importorskip`.

    Примечание:
        Маркеры ``@pytest.mark.filterwarnings`` подавляют те же
        два deprecation-предупреждения, что и в предыдущем тесте
        (см. раздел «Подавление предупреждений» в docstring
        модуля). Подавление действует только в этом тесте.
    """
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/probe")
    async def probe(auth: AuthDep) -> dict:
        return {"ttl": auth.session_ttl}

    fake_auth = AuthService(session_ttl_seconds=4242)
    app.dependency_overrides[get_auth_service] = lambda: fake_auth

    with TestClient(app) as client:
        response = client.get("/probe")

    assert response.status_code == 200, response.json()
    assert response.json() == {"ttl": 4242}
