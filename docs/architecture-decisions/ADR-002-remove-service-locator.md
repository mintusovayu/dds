# ADR-002: Remove Service Locator

| Поле | Значение |
|---|---|
| Дата | 2025-01-15 |
| Автор | mintusovayu |
| Статус | accepted |
| Supersedes | — |
| Superseded-by | — |
| Связанные ADR | ADR-001 (Temporary stderr Lock) |
| Фаза внедрения | 2 |

---

## Контекст

В `dds_web/api.py` и `dds_web/auth.py` использовался паттерн
**Service Locator** для доступа к долгоживущим сервисам:

```python
# dds_web/api.py
_context: APIContext | None = None

def set_context(ctx: APIContext | None) -> None:
    global _context
    _context = ctx

def get_context() -> APIContext:
    if _context is None:
        raise HTTPException(status_code=503, ...)
    return _context

CtxDep = Annotated[APIContext, Depends(get_context)]
```

```python
# dds_web/auth.py
_auth_service: AuthService | None = None

def set_auth_service(service: AuthService | None) -> None:
    global _auth_service
    _auth_service = service

def get_auth_service() -> AuthService:
    if _auth_service is None:
        raise HTTPException(status_code=503, ...)
    return _auth_service

AuthDep = Annotated[AuthService, Depends(get_auth_service)]
```

`lifespan.py` устанавливал значения при старте
(`set_context(api_context)`, `set_auth_service(auth_service)`) и
сбрасывал в `None` при shutdown. Все эндпоинты и зависимости
аутентификации читали сервисы через эти глобальные функции.

**Проблемы Service Locator в DDS:**

1. **Скрытые зависимости.** Из сигнатуры эндпоинта
   `async def search(..., ctx: CtxDep)` не видно, какие конкретно
   компоненты используются: `CtxDep` лишь указывает, что будет
   вызвана `get_context()`, но что она вернёт — определяется
   глобальным состоянием, изменяемым извне.

2. **Порядок инициализации критичен и неявен.** Если эндпоинт
   вызван до `lifespan` startup (например, в unit-тесте без
   `TestClient` с `with`-контекстом), глобал равен `None`, и
   пользователь получает 503. Причина — в порядке выполнения
   lifespan, который не виден из места вызова.

3. **Тестирование через `dependency_overrides` невозможно.**
   FastAPI предоставляет штатный механизм подмены зависимостей
   (`app.dependency_overrides[get_context] = lambda: fake`), но
   он работает только через функции-зависимости, читающие
   аргументы из контекста запроса. Глобальная `get_context()`
   без аргументов обходит этот механизм: подменить её —
   значит переопределить саму функцию, что не разделяется
   между тестами изолированно (модульный глобал общий для всех
   тестов в одном процессе).

4. **Небезопасность при параллельных тестах.** Если pytest
   запущен с `pytest-xdist` в режиме `--dist=loadscope` и
   несколько модулей используют `set_context` / `set_auth_service`,
   состояние глобалов разделяется между тестами одного воркера,
   что приводит к трудно диагностируемым взаимным влияниям.

5. **Иллюзия доступности.** `get_context()` без аргументов
   выглядит как чистая функция, но зависит от глобального
   состояния, изменяемого во времени. Это классический
   anti-pattern, отмеченный ещё в «Design Patterns» (GoF) и
   многократно — в книгах по архитектуре (Clean Architecture,
   Мартин).

**Признаки проблемы в коде.** Функции `set_context` и
`set_auth_service` вызывались из одного места (`lifespan.py`)
в двух фазах (startup — установить, shutdown — сбросить).
Единственная причина существования `set_*` — инициализация
глобалов. Никакой другой код их не вызывал: это Service
Locator без потребителей set-части, что подтверждает
искусственность конструкции.

**Контекст среды.** DDS работает в single-worker режиме uvicorn
(см. `docs/deployment.md`), поэтому проблема гонок за глобалы
не проявляется в production. Однако тестируемость и явность
зависимостей страдают независимо от режима запуска.

---

## Альтернативы

### Альтернатива 1: Оставить Service Locator как есть

**Описание.** Не менять подход: `_context` / `_auth_service` +
`set_*` / `get_*` остаются.

**Плюсы.**
- Никаких изменений в коде.
- Все существующие вызовы `CtxDep` / `AuthDep` работают.

**Минусы.**
- Скрытые зависимости сохраняются: `get_context()` без
  аргументов не отражает, откуда берётся контекст.
- `dependency_overrides` неприменим — тестирование через
  подмену невозможно без хаков (monkey-patch глобала).
- Глобальное состояние разделяется между тестами.
- Противоречит принципу «явное лучше неявного» (PEP 20) и
  практике Clean Architecture.

**Итог.** Отклонена. Проблемы реальны и не компенсируются
простотой сохранения.

### Альтернатива 2: `app.state` через FastAPI dependency (принята)

**Описание.** Значения `api_context` и `auth_service` сохраняются
в `app.state` при старте (уже реализовано). Функции-зависимости
принимают `Request` и читают значения из `request.app.state`:

```python
def get_context(request: Request) -> APIContext:
    ctx = getattr(request.app.state, "api_context", None)
    if ctx is None:
        raise HTTPException(503, "Приложение не инициализировано.")
    return ctx
```

FastAPI подставляет `Request` автоматически. `CtxDep` и `AuthDep`
сохраняют тип `Annotated[..., Depends(get_context)]`.

**Плюсы.**
- **Явные зависимости.** `get_context(request)` показывает, что
  значение берётся из контекста запроса. `request.app.state` —
  документированный стандарт FastAPI/Starlette.
- **Совместимость с `dependency_overrides`.** Тесты подменяют
  `get_context` через `app.dependency_overrides[get_context] = ...`
  — изолированно для каждого `TestClient`.
- **Нет глобального состояния в модуле.** Глобалы удалены;
  единственный источник истины — `app.state`.
- **Идемпотентность.** `get_context` может вызываться многократно
  в рамках одного запроса без побочных эффектов.
- **Идиоматичность FastAPI.** FastAPI/Starlette предлагают
  `request.app.state` именно для этого сценария; подход
  соответствует экосистемным практикам.

**Минусы.**
- **`Request` в сигнатуре.** Функция теряет вид «чистой»:
  приходится принимать `Request`, хотя используется только
  `request.app.state`.
- **Небольшое снижение явности типа.** `app.state` —
  динамическое пространство имён (`Starlette` использует
  `State`), атрибуты не типизированы. Внутри `get_context`
  это компенсируется `getattr(..., None)` и явной проверкой.

**Итог.** Принята.

### Альтернатива 3: Прямые обращения к `request.app.state` в эндпоинтах

**Описание.** Убрать `get_context` / `get_auth_service` полностью:
эндпоинты принимают `request: Request` и обращаются к
`request.app.state.api_context` напрямую. При отсутствии —
`HTTPException(503)` в каждом обработчике.

**Плюсы.**
- Нет промежуточной функции-зависимости.
- Все данные видны в одном месте.

**Минусы.**
- **Дублирование проверки.** Каждый эндпоинт (≈ 20 в `api.py`,
  ≈ 2 в `pages.py`) содержит `if request.app.state.X is None:
  raise HTTPException(503, ...)`. Логика валидации
  распространяется по коду.
- **Шум в сигнатурах.** `request: Request` появляется везде,
  даже там, где нужен только `APIContext` или `AuthService`.
- **Потеря типизации.** `CtxDep = Annotated[APIContext,
  Depends(get_context)]` даёт статический тип. `request: Request`
  требует явного `cast` или `getattr` в каждом эндпоинте.
- **Потеря единой точки для `dependency_overrides`.** Подмена
  одной функции `get_context` заменяется подменой всего
  `app.state` (более инвазивно).

**Итог.** Отклонена. Дублирование и потеря типа не оправданы.

### Альтернатива 4: `contextvars.ContextVar` для хранения сервисов

**Описание.** Хранить `APIContext` и `AuthService` в
`contextvars.ContextVar`. `lifespan` устанавливает значения,
функции-зависимости читают их без параметров.

**Плюсы.**
- Корректно работает в асинхронном коде: `ContextVar` изолирован
  на уровне задачи.
- Функции-зависимости без `Request`.

**Минусы.**
- **Ручное управление жизненным циклом.** Нужно явно устанавливать
  и сбрасывать `ContextVar` в lifespan и, возможно, в middleware,
  чтобы гарантировать корректное значение в каждом запросе.
- **Дублирование функциональности.** FastAPI уже предоставляет
  `request.app.state` для хранения долгоживущих сервисов;
  `ContextVar` в данном случае — более низкоуровневый
  механизм без преимуществ.
- **Не интегрируется с `dependency_overrides`.** Подмена
  `get_context` через override не изменит значение
  `ContextVar` — тесты должны явно устанавливать его сами.
- **Сложнее отлаживать.** Значение `ContextVar` не отображается
  в стандартных отладочных снимках ASGI-запроса.

**Итог.** Отклонена. Избыточна для задачи; `app.state` —
идиоматичный инструмент.

### Альтернатива 5: `Depends(get_settings)` через Pydantic Settings

**Описание.** Использовать `pydantic_settings.BaseSettings` для
всех настроек и зависимостей. FastAPI-dependency возвращает
объект `Settings`; `APIContext` — часть `Settings`.

**Плюсы.**
- Единая точка конфигурации (env, файлы, секреты).
- Валидация типов из коробки.

**Минусы.**
- **Концептуальная несовместимость.** `APIContext` — не настройки,
  а **контейнер сервисов** (SearchEngine, ScanOrchestrator,
  EventBus и др.). Хранить их в `BaseSettings` — смешение
  ответственностей.
- **Тяжёлые зависимости.** `SearchEngine` несериализуем, не
  читается из env/файлов; его создание требует БД, FTS-бэкенда,
  конфигурации. Это не «настройка».
- **Изменение архитектуры.** Для внедрения потребовалось бы
  переопределить всё приложение как pydantic-settings-обёртку —
  несоразмерно задаче.

**Итог.** Отклонена. Неверный инструмент для задачи.

---

## Решение

Применяется **Альтернатива 2**: FastAPI dependency-функции
`get_context(request)` и `get_auth_service(request)`, читающие
значения из `request.app.state`.

### Ключевые детали реализации

- **Хранение.** Значения сохраняются в `lifespan` startup:
  `app.state.api_context = api_context`,
  `app.state.auth_service = auth_service` (это уже было
  реализовано ранее; изменение Фазы 2 только удаляет
  дублирующие вызовы `set_context` / `set_auth_service`).
- **Чтение.** `get_context(request)` и `get_auth_service(request)`
  используют `getattr(request.app.state, "<name>", None)`. При
  `None` — `HTTPException(503)`.
- **Удаление глобалов.** `_context`, `_auth_service`,
  `set_context`, `set_auth_service` — удалены. Импорты в
  `lifespan.py` также удалены, вызовы `set_*` убраны.
- **Типизированные алиасы.** `CtxDep = Annotated[APIContext,
  Depends(get_context)]`, `AuthDep = Annotated[AuthService,
  Depends(get_auth_service)]` — сохранены, эндпоинты не меняли
  сигнатур.
- **Тестовое покрытие.** `tests/test_api_context_dependency.py`
  проверяет: возврат значения из `app.state`; 503 при
  отсутствии/`None`; интеграцию через `dependency_overrides`
  в `TestClient`.

### Инварианты

- **`get_context` и `get_auth_service` идемпотентны.** Могут
  вызываться многократно в рамках одного запроса; результат
  стабилен, побочных эффектов нет.
- **Единственная точка изменения поведения при отсутствии
  сервиса.** Проверка `is None → HTTPException(503)` — внутри
  DI-функций, а не в эндпоинтах.
- **`dependency_overrides` работает штатно.** Тесты могут
  подменять `get_context` / `get_auth_service` изолированно
  для каждого `TestClient` без влияния на другие тесты.
- **Значения `app.state` доступны только после startup.**
  Если эндпоинт вызван до старта lifespan, DI-функция
  возвращает 503 — это осознанное поведение (лучше явный 503,
  чем `AttributeError`).

---

## Последствия

### Положительные

- Устранены модульные глобалы в presentation layer. Зависимости
  читаются через стандартный механизм FastAPI.
- `app.dependency_overrides` работает штатно: тесты подменяют
  `get_context` / `get_auth_service` изолированно.
- `CtxDep` / `AuthDep` в сигнатурах эндпоинтов не менялись —
  потребители не затронуты.
- Согласованность с `dds_web/auto_login.py`, который уже
  использовал `request.app.state` для доступа к `auth_service`,
  `auto_login_config`, `event_bus`.
- Упрощён `lifespan.py`: удалены 2 вызова в startup и 2 в
  shutdown.

### Отрицательные

- `get_context` / `get_auth_service` принимают `Request`, что
  делает их менее «чистыми» на вид. Компенсируется явностью
  источника (`request.app.state`) и стандартностью для FastAPI.
- `app.state` — динамическое пространство имён без статической
  типизации атрибутов. Внутри DI-функций это компенсируется
  `getattr(..., None)` и явной проверкой.

### Нейтральные

- API эндпоинтов не изменился (сигнатуры с `CtxDep` / `AuthDep`
  сохранены).
- Добавлен один тестовый файл (`tests/test_api_context_dependency.py`,
  8 тестов).
- В `DEPRECATIONS.yaml` появятся две записи: `set_context` и
  `set_auth_service` с `removed_in_phase: 2`.

### Известные ограничения

- **`app.state` доступен только после startup.** Если внешний
  код (например, тест) вызывает эндпоинт до выполнения lifespan,
  DI-функция возвращает 503. Это правильное поведение: явный
  отказ лучше, чем неинициализированный контекст.
- **`Request` в сигнатуре DI-функции.** Принимается как
  неизбежная плата за интеграцию с `app.state`. Альтернативы
  4 и 5 (ContextVar, Pydantic Settings) не дают преимуществ.
- **Значения `app.state` не сериализуемы.** `APIContext` содержит
  `SearchEngine`, `EventBus` и другие тяжёлые объекты с
  внутренним состоянием. Это осознанное решение: `app.state` —
  место для таких объектов, а не для конфигурации.

---

## Ссылки

- **Реализация:**
  - `dds_web/api.py` — функция `get_context(request)`, тип `CtxDep`.
  - `dds_web/auth.py` — функция `get_auth_service(request)`,
    тип `AuthDep`.
  - `dds_web/lifespan.py` — сохранение в `app.state`,
    удаление `set_context` / `set_auth_service`.
- **Тесты:** `tests/test_api_context_dependency.py` (8 тестов:
  6 unit + 2 интеграционных).
- **Связанные ADR:** ADR-001 (Temporary stderr Lock) — не
  затрагивается.
- **Внешние материалы:**
  - FastAPI documentation, «Dependencies with yield» и
    «Testing dependencies with overrides»:
    <https://fastapi.tiangolo.com/advanced/testing-dependencies/>
  - Starlette documentation, `Request.app.state`:
    <https://www.starlette.io/requests/>
  - Martin Fowler, «Inversion of Control Containers and the
    Dependency Injection pattern» (2004) — критика Service Locator:
    <https://martinfowler.com/articles/injection.html>
```

---
