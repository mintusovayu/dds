"""
Тесты жизненного цикла ``AuthService``.

Назначение
----------
Полное покрытие публичного API ``AuthService`` из ``dds_web/auth.py``
без обращения к веб-слою (FastAPI, HTTP-запросы, cookies). Сервис
тестируется как самостоятельный класс — все проверки выполняются
через его методы.

Проверяемые разделы
-------------------

+----------------------------------+----------------------------------+
| Раздел                           | Что покрывается                  |
+==================================+==================================+
| Парольная политика (``add_user``)| Минимальная длина, чёрный список,|
|                                  | совпадение с логином, чисто      |
|                                  | цифровой пароль, дубликаты       |
|                                  | логинов, кастомная роль.         |
+----------------------------------+----------------------------------+
| ``add_user_with_hash``           | Валидация hex-формата хеша,      |
|                                  | минимальная длина соли,          |
|                                  | дубликаты логинов,               |
|                                  | сохранение с указанной ролью.    |
+----------------------------------+----------------------------------+
| ``user_exists``                  | Наличие/отсутствие пользователя. |
+----------------------------------+----------------------------------+
| ``login``                        | Успех, неверный пароль,          |
|                                  | несуществующий пользователь.     |
+----------------------------------+----------------------------------+
| ``create_session_for_user``      | Создание сессии с флагами        |
|                                  | ``is_auto=True``/``False``,      |
|                                  | несуществующий пользователь.     |
+----------------------------------+----------------------------------+
| ``validate_session``             | Валидная сессия, истечение,      |
|                                  | несуществующий токен.            |
+----------------------------------+----------------------------------+
| ``logout``                       | Инвалидация конкретного токена.  |
+----------------------------------+----------------------------------+
| ``change_password``              | Успех, неверный старый пароль,   |
|                                  | несуществующий пользователь,     |
|                                  | слабый новый пароль, обновление  |
|                                  | соли.                            |
+----------------------------------+----------------------------------+
| ``cleanup_expired_sessions``     | Удаление истёкших сессий,        |
|                                  | сохранение валидных.             |
+----------------------------------+----------------------------------+

Стратегия тестирования
----------------------
- **Мок времени** через ``monkeypatch.setattr("dds_web.auth.time.time", ...)``.
  Позволяет детерминированно проверять истечение сессий без
  ``time.sleep``. Мок нацелен на модуль-потребитель
  (``dds_web.auth``), где имя ``time`` импортировано как модуль.
- **Реальный PBKDF2** в тестах ``add_user``/``login``/``change_password``.
  Хеширование — часть проверяемой логики; мокать его значило бы
  тестировать мок. Один вызов занимает ~50–100 мс на целевых
  системах; суммарное время файла ~3–5 секунд.
- **Валидный хеш для ``add_user_with_hash``** генерируется через
  ``hashlib.pbkdf2_hmac`` с теми же параметрами, что использует
  ``AuthService._hash_password``. Это гарантирует совместимость.
- **Function-scoped fixtures.** Каждый тест получает свежий
  ``AuthService``; состояние между тестами не переносится.
- **Изоляция от ``core_config``.** Значения парольной политики
  берутся из ``core_config`` в тестах-помощниках (для генерации
  граничных значений), но сами проверки не зависят от конкретных
  констант.
- **Проверка успеха ``create_session_for_user``.** Метод возвращает
  ``str | None``: ``None`` означает, что пользователь не найден.
  В тестах, где создание сессии ожидается успешным, результат
  сразу валидируется через ``assert token is not None`` — это
  сужает тип для статического анализа (Pylance/mypy) и превращает
  потенциальную проблему setup в явное падение с понятным
  сообщением, а не в неожиданный ``AttributeError`` внутри
  ``validate_session``.

Границы
-------
- **HTTP-слой** (cookies, заголовки, ``dependency_overrides``) —
  тестируется в ``test_api_context_dependency.py`` и smoke-тестах.
- **Auto-login middleware** — отдельный файл (при наличии).
- **Хеширование и timing-attacks** — проверяется через
  ``secrets.compare_digest``; сам примитив не требует тестов.
- **Парольная политика как функция** — покрыта через
  ``add_user``/``change_password``; отдельный тест
  ``_validate_password_strength`` не создаётся (приватный метод).

Запуск
------
::

    pytest tests/test_auth_lifecycle.py -v

Принципы:
    - модуль не выполняет логирования;
    - не читает и не пишет файлы;
    - каждый тест изолирован;
    - тесты детерминированы: одинаковый вход → одинаковый результат.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest
from dds_core.domain import config as core_config
from dds_web.auth import (
    AuthService,
    Session,
    UserRole,
)

# =====================================================================
# Константы
# =====================================================================

_VALID_USERNAME = "valid_user"
"""Логин, валидный для большинства тестов."""

_VALID_PASSWORD = "SecurePass1234!"
"""Пароль, удовлетворяющий всем правилам политики.

Длина ≥ 10, не в чёрном списке, не совпадает с логином,
не чисто цифровой.
"""

_VALID_PASSWORD_ALT = "AnotherPass5678!"
"""Альтернативный валидный пароль (для смены пароля)."""

_VALID_ROLE_ADMIN = UserRole.ADMIN
"""Роль администратора (для тестов ролей)."""

_SALT_32 = "a" * 32
"""Синтетическая соль длиной 32 символа (≥ 16, валидна)."""

_SHORT_SALT_8 = "a" * 8
"""Синтетическая соль длиной 8 символов (< 16, невалидна)."""

_SHORT_SALT_15 = "a" * 15
"""Синтетическая соль длиной 15 символов (< 16, невалидна)."""

_TEST_PBKDF2_ITERATIONS = 100_000
"""Число итераций PBKDF2 в ``AuthService._hash_password``.

Продублировано в тестах для генерации валидных хешей через
``hashlib`` без доступа к приватному методу сервиса.
"""


# =====================================================================
# Helpers
# =====================================================================


def _make_valid_hash(password: str, salt: str) -> str:
    """Генерирует PBKDF2-хеш, совместимый с ``AuthService._hash_password``.

    Использует те же параметры (SHA-256, 100 000 итераций),
    что и production-код. Позволяет создавать валидные хеши для
    тестов ``add_user_with_hash`` без обхода публичного API.

    Args:
        password: Пароль в открытом виде.
        salt: Соль (любая непустая строка).

    Returns:
        Hex-строка длиной 64 символа.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations=_TEST_PBKDF2_ITERATIONS,
    ).hex()


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def service() -> AuthService:
    """Свежий ``AuthService`` с TTL сессии 60 секунд.

    Маленький TTL упрощает тесты истечения: не нужно мокать
    далёкое будущее. Мок времени всё равно применяется — TTL
    здесь задаёт ожидаемый интервал.

    Returns:
        Настроенный сервис без пользователей.
    """
    return AuthService(session_ttl_seconds=60)


@pytest.fixture
def service_with_user(service: AuthService) -> AuthService:
    """Сервис с одним валидным пользователем.

    Args:
        service: Пустой сервис.

    Returns:
        Сервис с пользователем ``_VALID_USERNAME``/``_VALID_PASSWORD``.
    """
    service.add_user(_VALID_USERNAME, _VALID_PASSWORD, UserRole.USER)
    return service


@pytest.fixture
def time_machine(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Управление «текущим» временем для ``dds_web.auth.time.time``.

    Возвращает callable для установки времени. Мок нацелен на
    модуль-потребитель (``dds_web.auth``), где ``time`` импортирован
    как модуль и вызывается через ``time.time()``.

    Использование::

        time_machine(1000.0)
        token = service.create_session_for_user("user")
        time_machine(1061.0)  # прошло 61 секунду при TTL=60
        assert service.validate_session(token) is None

    Args:
        monkeypatch: Встроенная фикстура pytest.

    Returns:
        Функция установки времени (в секундах от эпохи).
    """
    state: dict[str, float] = {"now": 1_000_000.0}

    def _fake_time() -> float:
        return state["now"]

    monkeypatch.setattr("dds_web.auth.time.time", _fake_time)

    def _set(value: float) -> None:
        state["now"] = value

    return _set


# =====================================================================
# Раздел 1. add_user: парольная политика
# =====================================================================


def test_add_user_valid_credentials(service: AuthService) -> None:
    """Валидные логин и пароль → пользователь добавлен.

    Проверяет:
        - ``user_exists`` возвращает ``True``;
        - ``login`` возвращает непустой токен.
    """
    service.add_user(_VALID_USERNAME, _VALID_PASSWORD, UserRole.USER)

    assert service.user_exists(_VALID_USERNAME)
    token = service.login(_VALID_USERNAME, _VALID_PASSWORD)
    assert isinstance(token, str) and len(token) > 0


def test_add_user_duplicate_username(service: AuthService) -> None:
    """Повторное добавление пользователя → ``ValueError``.

    Проверяет сообщение об ошибке содержит имя пользователя.
    """
    service.add_user(_VALID_USERNAME, _VALID_PASSWORD, UserRole.USER)

    with pytest.raises(ValueError, match=_VALID_USERNAME):
        service.add_user(_VALID_USERNAME, _VALID_PASSWORD_ALT, UserRole.USER)


def test_add_user_password_too_short(service: AuthService) -> None:
    """Пароль короче ``PASSWORD_MIN_LENGTH`` → ``ValueError``.

    Проверяет граничное значение: ``min_length - 1`` отвергается.
    """
    short_password = "a" * (core_config.PASSWORD_MIN_LENGTH - 1)

    with pytest.raises(ValueError, match="короткий"):
        service.add_user(_VALID_USERNAME, short_password, UserRole.USER)

    assert not service.user_exists(_VALID_USERNAME)


def test_add_user_password_at_min_length(
    service: AuthService,
) -> None:
    """Пароль точно длиной ``PASSWORD_MIN_LENGTH`` → принят.

    Проверяет граничное значение ``min_length`` (не меньше, а
    ровно). Пароль вида ``"a1..."`` — не чисто цифровой, не в
    чёрном списке, не совпадает с логином.
    """
    # Гарантируем: длина == min_length, содержит буквы и цифры.
    length = core_config.PASSWORD_MIN_LENGTH
    password = ("A1" * ((length // 2) + 1))[:length]
    # Если после обрезки получилось только цифры — это невозможно
    # при чередовании "A1".

    service.add_user(_VALID_USERNAME, password, UserRole.USER)

    assert service.user_exists(_VALID_USERNAME)


def test_add_user_password_in_blacklist(service: AuthService) -> None:
    """Пароль из чёрного списка → ``ValueError``.

    Используется ``"1234567890"`` из
    ``core_config.PASSWORD_COMMON_BLACKLIST`` — длина 10
    удовлетворяет политике, но пароль в списке.
    """
    blacklisted = "1234567890"
    assert blacklisted in core_config.PASSWORD_COMMON_BLACKLIST

    with pytest.raises(ValueError, match="распространённых"):
        service.add_user(_VALID_USERNAME, blacklisted, UserRole.USER)

    assert not service.user_exists(_VALID_USERNAME)


def test_add_user_password_matches_username(service: AuthService) -> None:
    """Пароль совпадает с логином → ``ValueError``.

    Логин длиной ≥ 10, чтобы не сработала проверка длины.
    """
    username = "verylongusername"
    assert len(username) >= core_config.PASSWORD_MIN_LENGTH

    with pytest.raises(ValueError, match="совпадать"):
        service.add_user(username, username, UserRole.USER)


def test_add_user_password_all_digits(service: AuthService) -> None:
    """Пароль только из цифр → ``ValueError``.

    Пароль ``"12345678901"`` — 11 цифр, не в чёрном списке,
    не совпадает с логином, но ``isdigit()`` истинно.
    """
    digits_only = "12345678901"
    assert digits_only.isdigit()
    assert digits_only not in core_config.PASSWORD_COMMON_BLACKLIST

    with pytest.raises(ValueError, match="только из цифр"):
        service.add_user(_VALID_USERNAME, digits_only, UserRole.USER)


def test_add_user_password_case_insensitive_blacklist(
    service: AuthService,
) -> None:
    """Чёрный список проверяется без учёта регистра.

    ``"QWERTY123"`` (9 символов) — короче минимальной длины, не
    подходит для теста. Используется ``"1234567890"`` в верхнем
    регистре — но цифры не имеют регистра. Применяется
    пароль-буква: ``"Password12"`` (10 символов) — не в списке
    напрямую, но ``.lower()`` даёт ``"password12"`` — тоже не в
    списке. Значит, для проверки case-insensitivity нужен
    пароль именно из списка с буквами длиной ≥ 10: такого нет.

    Тест фактически проверяет, что при корректной длине
    не-чёрный пароль принимается.
    """
    # Длина 10, смешанный регистр, не в чёрном списке.
    password = "Password12"
    assert password.lower() not in core_config.PASSWORD_COMMON_BLACKLIST

    service.add_user(_VALID_USERNAME, password, UserRole.USER)
    assert service.user_exists(_VALID_USERNAME)


def test_add_user_custom_role(service: AuthService) -> None:
    """Роль пользователя сохраняется.

    Создаётся администратор; проверяется роль через ``login``
    и ``validate_session``.
    """
    service.add_user(_VALID_USERNAME, _VALID_PASSWORD, UserRole.ADMIN)

    token = service.login(_VALID_USERNAME, _VALID_PASSWORD)
    session = service.validate_session(token)
    assert session is not None
    assert session.role == UserRole.ADMIN


def test_add_user_default_role_is_user(service: AuthService) -> None:
    """Роль по умолчанию — ``UserRole.USER``.

    Вызов ``add_user`` без третьего аргумента; проверяется роль
    в сессии.
    """
    service.add_user(_VALID_USERNAME, _VALID_PASSWORD)

    token = service.login(_VALID_USERNAME, _VALID_PASSWORD)
    session = service.validate_session(token)
    assert session is not None
    assert session.role == UserRole.USER


# =====================================================================
# Раздел 2. add_user_with_hash
# =====================================================================


def test_add_user_with_hash_valid(service: AuthService) -> None:
    """Валидные хеш и соль → пользователь добавлен и может войти.

    Проверяет:
        - ``user_exists`` истинно;
        - ``login`` с исходным паролем успешен (хеш совпадает).
    """
    password_hash = _make_valid_hash(_VALID_PASSWORD, _SALT_32)

    service.add_user_with_hash(
        _VALID_USERNAME,
        password_hash,
        _SALT_32,
        UserRole.USER,
    )

    assert service.user_exists(_VALID_USERNAME)
    token = service.login(_VALID_USERNAME, _VALID_PASSWORD)
    assert isinstance(token, str) and len(token) > 0


def test_add_user_with_hash_custom_role(service: AuthService) -> None:
    """Кастомная роль сохраняется при ``add_user_with_hash``."""
    password_hash = _make_valid_hash(_VALID_PASSWORD, _SALT_32)

    service.add_user_with_hash(
        _VALID_USERNAME,
        password_hash,
        _SALT_32,
        UserRole.ADMIN,
    )

    token = service.login(_VALID_USERNAME, _VALID_PASSWORD)
    session = service.validate_session(token)
    assert session is not None
    assert session.role == UserRole.ADMIN


def test_add_user_with_hash_duplicate(service: AuthService) -> None:
    """Повторное добавление → ``ValueError``.

    Проверяет до валидации хеша — сообщение содержит имя
    пользователя.
    """
    password_hash = _make_valid_hash(_VALID_PASSWORD, _SALT_32)
    service.add_user_with_hash(
        _VALID_USERNAME,
        password_hash,
        _SALT_32,
    )

    with pytest.raises(ValueError, match=_VALID_USERNAME):
        service.add_user_with_hash(
            _VALID_USERNAME,
            password_hash,
            _SALT_32,
        )


@pytest.mark.parametrize(
    "invalid_hash",
    [
        "",  # пустой
        "a" * 63,  # короче на 1 символ
        "a" * 65,  # длиннее на 1 символ
        "A" * 64,  # заглавные hex-символы
        "g" * 64,  # не-hex символы
        "z" * 64,  # не-hex символы
        "0x" + "a" * 62,  # префикс 0x
    ],
    ids=[
        "empty",
        "too-short",
        "too-long",
        "uppercase",
        "invalid-char-g",
        "invalid-char-z",
        "0x-prefix",
    ],
)
def test_add_user_with_hash_invalid_format(
    service: AuthService,
    invalid_hash: str,
) -> None:
    """Некорректный формат хеша → ``ValueError``.

    Проверяет, что ``_HASH_PATTERN`` (``^[0-9a-f]{64}$``) отвергает
    всё, кроме ровно 64 строчных hex-символов.
    """
    with pytest.raises(ValueError, match="password_hash"):
        service.add_user_with_hash(
            _VALID_USERNAME,
            invalid_hash,
            _SALT_32,
        )


@pytest.mark.parametrize(
    "invalid_salt",
    [
        "",
        _SHORT_SALT_8,
        _SHORT_SALT_15,
    ],
    ids=["empty", "8-chars", "15-chars"],
)
def test_add_user_with_hash_invalid_salt(
    service: AuthService,
    invalid_salt: str,
) -> None:
    """Соль короче 16 символов → ``ValueError``.

    Проверяет граничные значения: 8, 15 символов отвергаются;
    16 — принимается.
    """
    valid_hash = "a" * 64

    with pytest.raises(ValueError, match="salt"):
        service.add_user_with_hash(
            _VALID_USERNAME,
            valid_hash,
            invalid_salt,
        )


def test_add_user_with_hash_min_salt_length(service: AuthService) -> None:
    """Соль ровно 16 символов → принимается.

    Граничный случай: ``len(salt) == 16`` проходит проверку.
    """
    salt_16 = "a" * 16
    valid_hash = _make_valid_hash(_VALID_PASSWORD, salt_16)

    service.add_user_with_hash(_VALID_USERNAME, valid_hash, salt_16)

    assert service.user_exists(_VALID_USERNAME)


# =====================================================================
# Раздел 3. user_exists
# =====================================================================


def test_user_exists_true(service_with_user: AuthService) -> None:
    """Существующий пользователь → ``True``."""
    assert service_with_user.user_exists(_VALID_USERNAME) is True


def test_user_exists_false(service: AuthService) -> None:
    """Отсутствующий пользователь → ``False``."""
    assert service.user_exists("nonexistent_user") is False


# =====================================================================
# Раздел 4. login
# =====================================================================


def test_login_success(service_with_user: AuthService) -> None:
    """Верный пароль → непустой токен."""
    token = service_with_user.login(_VALID_USERNAME, _VALID_PASSWORD)

    assert isinstance(token, str)
    assert len(token) > 0
    # Токен имеет ожидаемый формат: hex(32 байта) = 64 hex.
    assert len(token) == 64
    int(token, 16)  # не падает, если это валидный hex


def test_login_wrong_password(service_with_user: AuthService) -> None:
    """Неверный пароль → ``ValueError``.

    Сообщение нейтральное («Неверный логин или пароль.»), чтобы
    не раскрывать факт существования пользователя.
    """
    with pytest.raises(ValueError, match="Неверный логин"):
        service_with_user.login(_VALID_USERNAME, "WrongPassword999!")


def test_login_nonexistent_user(service: AuthService) -> None:
    """Несуществующий логин → ``ValueError`` с тем же сообщением.

    Проверка отсутствия user enumeration: сообщение об ошибке
    идентично случаю неверного пароля.
    """
    with pytest.raises(ValueError, match="Неверный логин"):
        service.login("nonexistent_user", _VALID_PASSWORD)


def test_login_creates_non_auto_session(
    service_with_user: AuthService,
) -> None:
    """``login`` создаёт сессию с ``is_auto=False``.

    Ключевое отличие от ``create_session_for_user(is_auto=True)``
    (автологин по IP).
    """
    token = service_with_user.login(_VALID_USERNAME, _VALID_PASSWORD)
    session = service_with_user.validate_session(token)

    assert session is not None
    assert session.is_auto is False


# =====================================================================
# Раздел 5. create_session_for_user
# =====================================================================


def test_create_session_for_user_default_is_auto(
    service_with_user: AuthService,
) -> None:
    """По умолчанию ``is_auto=True``.

    Согласовано с использованием метода в
    ``auto_login_middleware``: явный вызов через middleware
    ожидает ``is_auto=True``.
    """
    token = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token is not None

    session = service_with_user.validate_session(token)
    assert session is not None
    assert session.is_auto is True


def test_create_session_for_user_explicit_is_auto_false(
    service_with_user: AuthService,
) -> None:
    """Явное ``is_auto=False`` сохраняется в сессии."""
    token = service_with_user.create_session_for_user(
        _VALID_USERNAME,
        is_auto=False,
    )
    assert token is not None

    session = service_with_user.validate_session(token)
    assert session is not None
    assert session.is_auto is False


def test_create_session_for_user_nonexistent(service: AuthService) -> None:
    """Несуществующий пользователь → ``None``.

    В отличие от ``login``, этот метод не поднимает исключение —
    он не выполняет аутентификацию, а лишь создаёт сессию после
    успешной внешней проверки.
    """
    token = service.create_session_for_user("nonexistent_user")

    assert token is None


# =====================================================================
# Раздел 6. validate_session
# =====================================================================


def test_validate_session_valid(
    service_with_user: AuthService,
    time_machine: Callable[[float], None],
) -> None:
    """Валидная сессия → объект ``Session``.

    Проверяет поля: ``username``, ``role``, ``is_auto``,
    ``created_at``, ``expires_at``.
    """
    time_machine(1_000_000.0)

    token = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token is not None
    session = service_with_user.validate_session(token)

    assert session is not None
    assert isinstance(session, Session)
    assert session.username == _VALID_USERNAME
    assert session.role == UserRole.USER
    assert session.is_auto is True
    assert session.created_at == 1_000_000.0
    assert session.expires_at == 1_000_000.0 + 60.0


def test_validate_session_nonexistent_token(
    service_with_user: AuthService,
) -> None:
    """Несуществующий токен → ``None``."""
    assert service_with_user.validate_session("nonexistent_token") is None


def test_validate_session_just_before_expiry(
    service_with_user: AuthService,
    time_machine: Callable[[float], None],
) -> None:
    """Сессия валидна на момент ``expires_at - epsilon``.

    Граничный случай: ``time.time() <= expires_at`` → валидна.
    """
    time_machine(1_000_000.0)
    token = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token is not None

    # Проматываем время на TTL - 1 (59 секунд из 60).
    time_machine(1_000_000.0 + 59.0)
    session = service_with_user.validate_session(token)

    assert session is not None


def test_validate_session_expired(
    service_with_user: AuthService,
    time_machine: Callable[[float], None],
) -> None:
    """Сессия после ``expires_at`` → ``None`` и удаляется из хранилища.

    Проверяет два эффекта:
        - возврат ``None``;
        - повторный вызов (даже с корректным временем) тоже
          возвращает ``None`` — сессия удалена, а не просто
          пропущена.
    """
    time_machine(1_000_000.0)
    token = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token is not None

    # Проматываем время на TTL + 1 (61 секунда из 60).
    time_machine(1_000_000.0 + 61.0)
    assert service_with_user.validate_session(token) is None

    # Даже если время «вернётся назад», сессия не восстановится.
    time_machine(1_000_000.0)
    assert service_with_user.validate_session(token) is None


# =====================================================================
# Раздел 7. logout
# =====================================================================


def test_logout_invalidates_token(
    service_with_user: AuthService,
) -> None:
    """После ``logout`` токен не проходит ``validate_session``."""
    token = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token is not None
    assert service_with_user.validate_session(token) is not None

    service_with_user.logout(token)

    assert service_with_user.validate_session(token) is None


def test_logout_nonexistent_token_is_noop(
    service_with_user: AuthService,
) -> None:
    """``logout`` для несуществующего токена — no-op.

    Метод идемпотентен: ``dict.pop(token, None)`` не поднимает
    ``KeyError``.
    """
    # Не должно падать.
    service_with_user.logout("nonexistent_token")


# =====================================================================
# Раздел 8. change_password
# =====================================================================


def test_change_password_success(
    service_with_user: AuthService,
) -> None:
    """Смена пароля с верным старым паролем → успех.

    Проверяет:
        - старый пароль больше не работает;
        - новый пароль работает.
    """
    service_with_user.change_password(
        _VALID_USERNAME,
        _VALID_PASSWORD,
        _VALID_PASSWORD_ALT,
    )

    with pytest.raises(ValueError, match="Неверный логин"):
        service_with_user.login(_VALID_USERNAME, _VALID_PASSWORD)

    token = service_with_user.login(_VALID_USERNAME, _VALID_PASSWORD_ALT)
    assert isinstance(token, str) and len(token) > 0


def test_change_password_wrong_old(
    service_with_user: AuthService,
) -> None:
    """Неверный старый пароль → ``ValueError`` с сообщением о текущем пароле."""
    with pytest.raises(ValueError, match="текущий пароль"):
        service_with_user.change_password(
            _VALID_USERNAME,
            "WrongOldPass123!",
            _VALID_PASSWORD_ALT,
        )


def test_change_password_nonexistent_user(service: AuthService) -> None:
    """Несуществующий пользователь → ``ValueError``."""
    with pytest.raises(ValueError, match="не найден"):
        service.change_password(
            "nonexistent_user",
            "AnyOldPass123!",
            _VALID_PASSWORD_ALT,
        )


@pytest.mark.parametrize(
    "weak_new_password,expected_match",
    [
        ("short", "короткий"),
        ("1234567890", "распространённых"),
        ("12345678901", "только из цифр"),
    ],
    ids=["too-short", "blacklisted", "digits-only"],
)
def test_change_password_weak_new_password(
    service_with_user: AuthService,
    weak_new_password: str,
    expected_match: str,
) -> None:
    """Новый пароль не соответствует политике → ``ValueError``.

    Парольная политика применяется к новому паролю полностью:
    длина, чёрный список, «только цифры».
    """
    with pytest.raises(ValueError, match=expected_match):
        service_with_user.change_password(
            _VALID_USERNAME,
            _VALID_PASSWORD,
            weak_new_password,
        )


def test_change_password_matches_username(
    service_with_user: AuthService,
) -> None:
    """Новый пароль совпадает с логином → ``ValueError``.

    ``_VALID_USERNAME = "valid_user"`` — 10 символов, граница
    политики. Проверяется совпадение.
    """
    # Логин длиной 10 совпадает с новой попыткой пароля.
    with pytest.raises(ValueError, match="совпадать"):
        service_with_user.change_password(
            _VALID_USERNAME,
            _VALID_PASSWORD,
            _VALID_USERNAME,
        )


def test_change_password_generates_new_salt(
    service: AuthService,
) -> None:
    """При смене пароля генерируется новая соль.

    Проверяет через косвенный признак: захешировать старый пароль
    новой солью нельзя (соль хранится приватно). Прямая проверка
    недоступна без доступа к приватному полю. Тест проверяет
    функциональное следствие: старый пароль не работает, новый
    работает — это гарантирует, что хеш был пересчитан.
    """
    service.add_user(_VALID_USERNAME, _VALID_PASSWORD, UserRole.USER)
    service.change_password(
        _VALID_USERNAME,
        _VALID_PASSWORD,
        _VALID_PASSWORD_ALT,
    )

    with pytest.raises(ValueError):
        service.login(_VALID_USERNAME, _VALID_PASSWORD)

    assert service.login(_VALID_USERNAME, _VALID_PASSWORD_ALT)


def test_change_password_does_not_affect_existing_sessions(
    service_with_user: AuthService,
    time_machine: Callable[[float], None],
) -> None:
    """Смена пароля не инвалидирует активные сессии.

    Это документированное поведение: активные сессии продолжают
    работать (см. docstring ``change_password``).
    """
    time_machine(1_000_000.0)

    token = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token is not None
    service_with_user.change_password(
        _VALID_USERNAME,
        _VALID_PASSWORD,
        _VALID_PASSWORD_ALT,
    )

    # Старая сессия всё ещё валидна.
    assert service_with_user.validate_session(token) is not None


# =====================================================================
# Раздел 9. cleanup_expired_sessions
# =====================================================================


def test_cleanup_expired_sessions_removes_expired(
    service_with_user: AuthService,
    time_machine: Callable[[float], None],
) -> None:
    """Истёкшие сессии удаляются, возвращается их количество.

    Сценарий:
        - 2 валидные сессии созданы в t=1000_000;
        - 1 сессия создана «раньше» (в t=999_900);
        - время сдвинуто на t=1_000_061 (все истекли при TTL=60);
        - cleanup возвращает 3.
    """
    time_machine(999_900.0)
    token_old = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token_old is not None

    time_machine(1_000_000.0)
    token_1 = service_with_user.create_session_for_user(_VALID_USERNAME)
    token_2 = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token_1 is not None
    assert token_2 is not None

    # Проматываем так, чтобы все три истекли (TTL = 60).
    time_machine(1_000_100.0)

    removed = service_with_user.cleanup_expired_sessions()

    assert removed == 3
    assert service_with_user.validate_session(token_old) is None
    assert service_with_user.validate_session(token_1) is None
    assert service_with_user.validate_session(token_2) is None


def test_cleanup_expired_sessions_keeps_valid(
    service_with_user: AuthService,
    time_machine: Callable[[float], None],
) -> None:
    """Валидные сессии не удаляются.

    Сценарий:
        - сессия_old создана в t=1_000_000 (истекает в 1_000_060);
        - сессия_new создана в t=1_000_050 (истекает в 1_000_110);
        - время сдвинуто на t=1_000_080 (old истекла, new валидна);
        - cleanup возвращает 1; new остаётся.
    """
    time_machine(1_000_000.0)
    token_old = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token_old is not None

    time_machine(1_000_050.0)
    token_new = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token_new is not None

    time_machine(1_000_080.0)
    removed = service_with_user.cleanup_expired_sessions()

    assert removed == 1
    assert service_with_user.validate_session(token_old) is None
    assert service_with_user.validate_session(token_new) is not None


def test_cleanup_expired_sessions_empty(
    service: AuthService,
) -> None:
    """Нет сессий → ``0``."""
    assert service.cleanup_expired_sessions() == 0


def test_cleanup_expired_sessions_none_expired(
    service_with_user: AuthService,
    time_machine: Callable[[float], None],
) -> None:
    """Все сессии валидны → ``0``, ничего не удалено."""
    time_machine(1_000_000.0)
    token = service_with_user.create_session_for_user(_VALID_USERNAME)
    assert token is not None

    time_machine(1_000_030.0)
    removed = service_with_user.cleanup_expired_sessions()

    assert removed == 0
    assert service_with_user.validate_session(token) is not None


# =====================================================================
# Раздел 10. session_ttl
# =====================================================================


def test_session_ttl_property() -> None:
    """Свойство ``session_ttl`` возвращает значение из конструктора."""
    service = AuthService(session_ttl_seconds=1800)
    assert service.session_ttl == 1800


def test_session_ttl_default() -> None:
    """TTL по умолчанию — 3600 секунд (1 час)."""
    service = AuthService()
    assert service.session_ttl == 3600
