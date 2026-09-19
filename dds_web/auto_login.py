"""
Автоматический вход по IP-адресу.

Модуль реализует автологин пользователей DDS по IP-адресу
клиента. Используется в сетях с доверенным доступом, где
IP-адреса жёстко привязаны к MAC-адресам устройств через
статический DHCP binding на роутере.

Принцип работы:
    Middleware :func:`auto_login_middleware` перехватывает
    каждый запрос и, если у клиента нет валидной сессии,
    проверяет IP-адрес клиента по карте ``ip_user_map``. При
    совпадении создаётся сессия без запроса пароля и
    устанавливается session cookie. Клиенты, чей IP отсутствует
    в карте, видят стандартную форму входа ``/login``.

Архитектурные решения:
    - Middleware реализован как module-level функция (не фабрика
      с замыканием). Регистрируется в ``create_app_with_lifespan``
      до старта приложения; зависимости читаются из
      ``request.app.state`` в момент обработки запроса.
    - Cookie-константы (``SESSION_COOKIE_NAME``,
      ``AUTO_LOGIN_OPT_OUT_COOKIE_NAME``) живут в ``auth.py``
      для предотвращения circular imports.
    - Токен сессии инжектится в ``request.scope["state"]`` до
      вызова ``call_next``, чтобы ``require_login`` видел его
      в том же запросе (cookie видна браузеру только на
      следующем запросе).
    - При исключении в route handler сессия откатывается через
      ``auth.logout(token)`` — предотвращает накопление
      orphan-сессий.

Opt-out механизм:
    Пользователь может явно выйти через ``POST /logout``.
    Устанавливается cookie ``dds_auto_login_opt_out`` с TTL
    ``core_config.AUTO_LOGIN_OPT_OUT_TTL_SECONDS`` (8 часов).
    Middleware пропускает автологин при её наличии. Успешный
    вход через ``/login`` удаляет opt-out cookie. Это решает
    проблемы «кнопка Выйти не работает» и «автологин перебивает
    ручной вход на общем устройстве».

Ограничения:
    - Требуется single-worker режим uvicorn: ``_sessions`` —
      in-process структура, не разделяется между воркерами.
    - При развёртывании за обратным прокси (nginx, Caddy)
      необходимо реализовать обработку заголовка
      ``X-Forwarded-For`` с проверкой ``trusted_proxies``.
      В текущей реализации используется ``request.client.host``
      напрямую. Без этого все клиенты будут видны как
      ``127.0.0.1``.

Функции и классы:
    ``AutoLoginConfig``            — конфигурация автологина.
    ``parse_auto_login_config``    — парсинг конфигурации.
    ``validate_auto_login_config`` — валидация (список WARNING).
    ``resolve_auto_login_user``    — определение username по IP.
    ``auto_login_middleware``      — ASGI middleware.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

try:
    from fastapi import Request
    from fastapi.responses import Response
except ImportError:
    raise ImportError(
        "Для работы автологина DDS необходимо установить fastapi: pip install fastapi"
    )

from dds_core.domain.events import AutoLoginPerformed

from .auth import (
    AUTO_LOGIN_OPT_OUT_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthService,
)
from .security import should_use_secure_cookie

# ----------------------------------------------------------------------
# Константы модуля
# ----------------------------------------------------------------------

logger = logging.getLogger("dds.auto_login")
"""Логгер модуля автологина. Отдельное имя ``dds.auto_login``
позволяет изолировать диагностику автологина от других событий
приложения при настройке уровней логирования.
"""

_SKIP_PATHS: frozenset[str] = frozenset({"/login", "/logout", "/favicon.ico"})
"""Пути, для которых автологин не применяется.

- ``/login`` — публичная страница входа; автологин там избыточен.
- ``/logout`` — пользователь явно выходит; если middleware поставит
  новую cookie, logout не сработает.
- ``/favicon.ico`` — браузер запрашивает автоматически при загрузке
  страницы; не требует аутентификации.
"""

_STATIC_PREFIX: str = "/static"
"""Префикс путей статических файлов. Запросы к ``/static/*``
пропускаются middleware, чтобы параллельные загрузки CSS и JS
не создавали зомби-сессии.
"""


# ----------------------------------------------------------------------
# Конфигурация автологина
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AutoLoginConfig:
    """Конфигурация автологина по IP-адресу.

    Атрибуты:
        enabled: Глобальный выключатель. При ``False`` middleware
            не выполняет никаких действий и пропускает все запросы.
        allowed_networks: Кортеж CIDR-сетей, для которых разрешён
            автологин. Пустой кортеж означает «автологин запрещён
            для всех клиентов» (безопасная семантика). Сети одного
            семейства (IPv4 или IPv6) в одном кортеже.
        ip_user_map: Карта «IP-адрес → username». Ключи — строковые
            представления IP-адресов в каноничной форме (результат
            ``str(ipaddress.ip_address(...))``). Значения — имена
            пользователей, зарегистрированные в ``AuthService``.
            Один пользователь может встречаться несколько раз
            (несколько устройств).
    """

    enabled: bool
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    ip_user_map: Mapping[str, str]


def parse_auto_login_config(raw: dict) -> AutoLoginConfig:
    """Парсит конфигурацию автологина из словаря.

    Операции:

    +----+-----------------------------------------------------+
    | №  | Описание                                            |
    +====+=====================================================+
    | 1  | Извлечение флага ``enabled``.                       |
    +----+-----------------------------------------------------+
    | 2  | Валидация ``allowed_networks`` как списка.          |
    |    | Некорректный тип → ``ValueError``.                  |
    +----+-----------------------------------------------------+
    | 3  | Парсинг каждой строки CIDR через                    |
    |    | ``ipaddress.ip_network(strict=False)``.             |
    |    | Некорректный CIDR → ``ValueError`` (fail-fast).     |
    +----+-----------------------------------------------------+
    | 4  | Валидация ``ip_user_map`` как словаря.              |
    |    | Некорректный тип → ``ValueError``.                  |
    +----+-----------------------------------------------------+
    | 5  | Нормализация ключей ``ip_user_map`` через           |
    |    | ``str(ipaddress.ip_address(...))``.                 |
    +----+-----------------------------------------------------+
    | 6  | Формирование ``AutoLoginConfig`` с tuple сетей      |
    |    | и ``MappingProxyType`` для карты IP.                |
    +----+-----------------------------------------------------+

    Примечание:
        Нормализация ключей ``ip_user_map`` приводит IPv6-адреса
        к каноничному виду (например, ``2001:0db8::1`` →
        ``2001:db8::1``). Без этого сравнение со ``str(client_ip)``
        могло бы не сработать.

    Args:
        raw: Словарь ``auth.auto_login`` из ``config.json``.
            Может содержать ключи ``enabled``, ``allowed_networks``,
            ``ip_user_map``. Отсутствующие ключи трактуются как
            значения по умолчанию (``enabled=false``, пустые
            коллекции).

    Returns:
        :class:`AutoLoginConfig` с валидированными и
        нормализованными данными.

    Raises:
        ValueError: Если ``allowed_networks`` не список, если
            ``ip_user_map`` не словарь, или если хотя бы одна
            строка CIDR в ``allowed_networks`` не является
            корректным CIDR. Исключение приводит к остановке
            приложения (fail-fast).
    """
    enabled = bool(raw.get("enabled", False))

    raw_networks = raw.get("allowed_networks", [])
    if not isinstance(raw_networks, list):
        raise ValueError(  # noqa: TRY004
            "auto_login.allowed_networks должен быть списком CIDR-строк."
        )

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for net_str in raw_networks:
        try:
            networks.append(ipaddress.ip_network(net_str, strict=False))
        except ValueError as e:
            raise ValueError(
                f"Некорректный CIDR '{net_str}' в auto_login.allowed_networks: {e}"
            ) from e

    raw_map = raw.get("ip_user_map", {})
    if not isinstance(raw_map, dict):
        raise ValueError(  # noqa: TRY004
            "auto_login.ip_user_map должен быть словарём «IP-адрес → username»."
        )

    normalized_map: dict[str, str] = {}
    for ip_str, username in raw_map.items():
        try:
            normalized_ip = str(ipaddress.ip_address(ip_str))
        except ValueError:
            # Невалидный IP — оставляем как есть, чтобы
            # validate_auto_login_config выдал WARNING.
            normalized_ip = ip_str
        normalized_map[normalized_ip] = str(username)

    return AutoLoginConfig(
        enabled=enabled,
        allowed_networks=tuple(networks),
        ip_user_map=MappingProxyType(normalized_map),
    )


def validate_auto_login_config(
    config: AutoLoginConfig,
    auth: AuthService,
) -> list[str]:
    """Проверяет конфигурацию автологина, возвращает список WARNING.

    Операции:

    +----+-----------------------------------------------------+
    | №  | Описание                                            |
    +====+=====================================================+
    | 1  | Если ``enabled == False`` — возврат пустого         |
    |    | списка (нет смысла проверять отключённый модуль).   |
    +----+-----------------------------------------------------+
    | 2  | Проверка непустоты ``allowed_networks``.            |
    |    | Пустой список при ``enabled=true`` — WARNING.       |
    +----+-----------------------------------------------------+
    | 3  | Проверка непустоты ``ip_user_map``.                 |
    |    | Пустая карта при ``enabled=true`` — WARNING.        |
    +----+-----------------------------------------------------+
    | 4  | Для каждой пары «IP → username»:                    |
    |    | a. Проверка валидности IP.                         |
    |    | b. Проверка существования пользователя             |
    |    |    в ``AuthService``.                              |
    +----+-----------------------------------------------------+
    | 5  | Возврат списка предупреждений.                      |
    +----+-----------------------------------------------------+

    Примечание:
        Функция не останавливает приложение при наличии WARNING.
        Это позволяет запускать DDS с частично некорректной
        конфигурацией автологина (например, если один из
        пользователей ещё не добавлен в ``config.json``).
        Администратор увидит WARNING'и в stdout при старте.

    Args:
        config: Валидированная конфигурация, полученная из
            :func:`parse_auto_login_config`.
        auth: Экземпляр ``AuthService`` с загруженными
            пользователями. Используется для проверки
            существования username.

    Returns:
        Список строк-предупреждений. Пустой список, если
        конфигурация корректна (или модуль отключён).
    """
    warnings: list[str] = []

    if not config.enabled:
        return warnings

    if not config.allowed_networks:
        warnings.append(
            "auto_login.enabled=true, но allowed_networks пуст — "
            "автологин не будет работать ни для одного клиента."
        )

    if not config.ip_user_map:
        warnings.append(
            "auto_login.enabled=true, но ip_user_map пуст — автологин не будет работать."
        )

    for ip_str, username in config.ip_user_map.items():
        try:
            ipaddress.ip_address(ip_str)
        except ValueError:
            warnings.append(f"auto_login.ip_user_map: '{ip_str}' — невалидный IP.")
            continue
        if not auth.user_exists(username):
            warnings.append(
                f"auto_login.ip_user_map: '{ip_str}' → '{username}': пользователь не найден."
            )

    return warnings


# ----------------------------------------------------------------------
# Разрешение IP-адреса в username
# ----------------------------------------------------------------------


def resolve_auto_login_user(
    ip_str: str,
    config: AutoLoginConfig,
    auth: AuthService,
) -> str | None:
    """Определяет username по IP-адресу клиента.

    Операции:

    +----+-----------------------------------------------------+
    | №  | Описание                                            |
    +====+=====================================================+
    | 1  | Парсинг ``ip_str`` через ``ipaddress.ip_address``.  |
    |    | Некорректный IP → ``None``.                         |
    +----+-----------------------------------------------------+
    | 2  | Нормализация IPv4-mapped IPv6-адресов               |
    |    | (``::ffff:192.168.1.42`` → ``192.168.1.42``).       |
    +----+-----------------------------------------------------+
    | 3  | Проверка вхождения в ``allowed_networks``.          |
    |    | Не входит → ``None``.                               |
    +----+-----------------------------------------------------+
    | 4  | Поиск в ``ip_user_map``.                            |
    |    | Не найдено → ``None``.                              |
    +----+-----------------------------------------------------+
    | 5  | Проверка существования пользователя в               |
    |    | ``AuthService``. Не существует → ``None``.          |
    +----+-----------------------------------------------------+
    | 6  | Возврат username.                                   |
    +----+-----------------------------------------------------+

    Args:
        ip_str: Строковое представление IP-адреса клиента,
            полученное из ``request.client.host``.
        config: Валидированная конфигурация автологина.
        auth: Экземпляр ``AuthService`` для проверки
            существования пользователя.

    Returns:
        ``username``, если IP прошёл все проверки. ``None``,
        если хотя бы одна проверка не пройдена.
    """
    try:
        client_ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return None

    # Нормализация IPv4-mapped IPv6-адресов.
    if isinstance(client_ip, ipaddress.IPv6Address) and client_ip.ipv4_mapped:
        client_ip = client_ip.ipv4_mapped

    if not _is_ip_in_allowed_networks(client_ip, config.allowed_networks):
        return None

    username = config.ip_user_map.get(str(client_ip))
    if username is None:
        return None

    if not auth.user_exists(username):
        return None

    return username


def _is_ip_in_allowed_networks(
    client_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    """Проверяет вхождение IP-адреса в одну из разрешённых сетей.

    Операции:

    +----+-----------------------------------------------------+
    | №  | Описание                                            |
    +====+=====================================================+
    | 1  | Если ``networks`` пуст — возврат ``False``          |
    |    | (безопасная семантика: пустой список запрещает всё).|
    +----+-----------------------------------------------------+
    | 2  | Для каждой сети:                                    |
    |    | a. Пропуск сетей другого семейства (IPv4 vs IPv6).  |
    |    | b. Проверка ``client_ip in net``.                   |
    +----+-----------------------------------------------------+
    | 3  | Если ни одна сеть не подошла — возврат ``False``.   |
    +----+-----------------------------------------------------+

    Примечание:
        Пропуск сетей другого семейства предотвращает
        ``TypeError``, который возникает при попытке сравнения
        ``IPv6Address`` с ``IPv4Network`` (или наоборот) в
        Python 3.11+.

    Args:
        client_ip: Разобранный IP-адрес клиента.
        networks: Кортеж разрешённых сетей.

    Returns:
        ``True``, если адрес входит хотя бы в одну сеть.
        ``False`` в противном случае.
    """
    if not networks:
        return False

    for net in networks:
        if client_ip.version != net.version:
            continue
        if client_ip in net:
            return True

    return False


# ----------------------------------------------------------------------
# Middleware автологина
# ----------------------------------------------------------------------


async def auto_login_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """ASGI middleware автоматического входа по IP-адресу.

    Вызывается первым в стеке middleware (внешний слой).
    Читает зависимости из ``request.app.state``, устанавливает
    session cookie при успешном автологине. Пропускает запрос
    без изменений, если автологин не применим.

    Операции:

    +----+-----------------------------------------------------+
    | №  | Описание                                            |
    +====+=====================================================+
    | 1  | Если ``auto_login_config`` отсутствует в             |
    |    | ``app.state`` или ``enabled == False`` → passthrough.|
    +----+-----------------------------------------------------+
    | 2  | Если метод запроса ``OPTIONS`` → passthrough        |
    |    | (CORS preflight).                                   |
    +----+-----------------------------------------------------+
    | 3  | Если путь в ``_SKIP_PATHS`` или под ``/static``      |
    |    | → passthrough.                                      |
    +----+-----------------------------------------------------+
    | 4  | Если присутствует opt-out cookie → passthrough.     |
    +----+-----------------------------------------------------+
    | 5  | Если session cookie валидна → passthrough           |
    |    | (пользователь уже залогинен).                       |
    +----+-----------------------------------------------------+
    | 6  | Если ``request.client is None`` → passthrough.      |
    +----+-----------------------------------------------------+
    | 7  | Резолв username по IP. ``None`` → passthrough.      |
    +----+-----------------------------------------------------+
    | 8  | Создание сессии. ``None`` → passthrough.            |
    +----+-----------------------------------------------------+
    | 9  | Инжект токена в ``request.scope["state"]`` до       |
    |    | ``call_next`` — гарантирует видимость в route       |
    |    | handler'е.                                          |
    +----+-----------------------------------------------------+
    | 10 | ``call_next`` с откатом сессии при исключении.      |
    +----+-----------------------------------------------------+
    | 11 | Установка session cookie в response.                |
    +----+-----------------------------------------------------+
    | 12 | Публикация события ``AutoLoginPerformed``.          |
    +----+-----------------------------------------------------+

    Примечание:
        Все ошибки на шагах 7–9 перехватываются через ``except``.
        Middleware не должна ломать запрос из-за собственного
        сбоя. Логирование на уровне ERROR обеспечивает
        диагностику без прерывания обработки.

    Примечание:
        Откат сессии при исключении в ``call_next`` предотвращает
        накопление orphan-сессий в ``AuthService._sessions``,
        которые не имеют соответствующей cookie у клиента.

    Args:
        request: HTTP-запрос FastAPI/Starlette.
        call_next: Callable, передающий запрос следующему слою
            middleware и возвращающий ``Response``.

    Returns:
        HTTP-ответ. Если автологин сработал — с установленной
        session cookie. Иначе — без изменений.
    """
    # Шаг 1: конфигурация.
    config: AutoLoginConfig | None = getattr(request.app.state, "auto_login_config", None)
    if config is None or not config.enabled:
        return await call_next(request)

    # Шаг 2: пропуск OPTIONS.
    if request.method == "OPTIONS":
        return await call_next(request)

    # Шаг 3: пропуск специальных путей.
    path = request.url.path
    if path in _SKIP_PATHS:
        return await call_next(request)
    if path == _STATIC_PREFIX or path.startswith(_STATIC_PREFIX + "/"):
        return await call_next(request)

    # Шаг 4: opt-out cookie.
    if request.cookies.get(AUTO_LOGIN_OPT_OUT_COOKIE_NAME):
        return await call_next(request)

    # Шаг 5: существующая валидная сессия.
    auth: AuthService = request.app.state.auth_service
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie and auth.validate_session(cookie) is not None:
        return await call_next(request)

    # Шаг 6: IP клиента.
    if request.client is None:
        return await call_next(request)
    ip_str = request.client.host

    # Шаги 7–9: резолв, создание сессии, инжект токена.
    try:
        username = resolve_auto_login_user(ip_str, config, auth)
        if username is None:
            return await call_next(request)

        token = auth.create_session_for_user(username, is_auto=True)
        if token is None:
            return await call_next(request)

        # Инжект токена в scope до call_next: require_login читает
        # его через request.state, что прозрачно обращается к
        # scope["state"].
        request.scope.setdefault("state", {})
        request.scope["state"]["auto_login_token"] = token
    except Exception as e:  # noqa: BLE001
        logger.error("Auto-login failed: %s", e)
        return await call_next(request)

    logger.debug("Auto-login: %s from %s", username, ip_str)

    # Шаг 10: вызов обработчика с откатом при исключении.
    try:
        response = await call_next(request)
    except Exception:
        # Откат созданной сессии — предотвращает накопление
        # orphan-сессий.
        auth.logout(token)
        raise

    # Шаг 11: установка session cookie.
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=should_use_secure_cookie(request),
        max_age=auth.session_ttl,
        path="/",
    )

    # Шаг 12: публикация события аудита.
    request.app.state.event_bus.publish(
        AutoLoginPerformed(
            username=username,
            client_ip=ip_str,
        )
    )

    return response
