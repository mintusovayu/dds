# Развёртывание Deep Doc Search (DDS)

## Требования к системе

### Минимальные требования

| Компонент | Требование |
|---|---|
| ОС | Linux (Ubuntu 22.04+, Debian 12+), macOS 13+, Windows 10+ |
| Python | 3.12 или выше |
| RAM | **8 ГБ** (каждый процесс извлечения текста потребляет ~50–150 МБ; 6 процессов ≈ 300–900 МБ) |
| Диск | 500 МБ для системы + место для БД + место для логов |
| CPU | 2 ядра (рекомендуется 6+ ядер для полного использования `ProcessPoolExecutor`) |

### Размер базы данных

Размер БД зависит от количества документов и объёма текстового слоя:

| Документов | Примерный размер БД |
|---|---|
| 1 000 | ~200 МБ |
| 10 000 | ~2 ГБ |
| 50 000 | ~10 ГБ |

## Установка

### Шаг 1: Клонирование репозитория

```bash
git clone https://github.com/your-org/dds.git
cd dds/python
```

### Шаг 2: Создание виртуального окружения

```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows
```

### Шаг 3: Установка зависимостей

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### requirements.txt

```text
# Ядро
pymupdf>=1.23.0
markupsafe>=2.1.0

# Веб-интерфейс
fastapi>=0.100.0
uvicorn[standard]>=0.23.0
jinja2>=3.1.0
pydantic>=2.0.0

# Тесты
pytest>=7.0.0
```

## Настройка конфигурации

### Создание config.json

```bash
cp config.json.example config.json
```

### Редактирование config.json

```json
{
    "_comment": "Конфигурация Deep Doc Search (DDS).",
    "dds": {
        "db_path": "/var/lib/dds/dds_database.db",
        "rd_directory": "/mnt/shared/rd_documents",
        "modules_directory": "dds_modules",
        "host": "0.0.0.0",
        "port": 8000
    },
    "modules": {
        "dds_stamp_extractor": {
            "templates_dir": "dds_modules/dds_stamp_extractor/data/templates",
            "debug_enabled": false
        }
    },
    "auth": {
        "_auth_warning": "Пароли в открытом виде допустимы только для локальной разработки. Слабые пароли отклоняются при запуске.",
        "users": [
            {
                "username": "admin",
                "password": "CHANGE_ME_TO_STRONG_PASSWORD",
                "role": "admin"
            }
        ],
        "session_ttl_seconds": 3600
    }
}
```

**Важно:** Замените `CHANGE_ME_TO_STRONG_PASSWORD` на надёжный пароль.

### Параметры конфигурации

| Параметр | Описание | По умолчанию |
|---|---|---|
| `dds.db_path` | Путь к файлу базы данных SQLite | `dds_database.db` |
| `dds.rd_directory` | Путь к каталогу рабочей документации | — |
| `dds.modules_directory` | Путь к каталогу модулей | `dds_modules` |
| `dds.host` | Адрес веб-сервера | `0.0.0.0` |
| `dds.port` | Порт веб-сервера | `8000` |
| `auth.users` | Список пользователей | — |
| `auth.session_ttl_seconds` | Время жизни сессии | `3600` |

### Дополнительные параметры ядра

Эти параметры задаются в `dds_core/domain/config.py` и могут быть
изменены при необходимости без правки `config.json`:

| Параметр | Описание | Значение по умолчанию |
|---|---|---|
| `SCAN_COMMIT_INTERVAL` | Количество документов в одной транзакции при батчевой записи (с `SAVEPOINT`) | `500` |
| `DEFAULT_LOG_LEVEL` | Уровень логирования событий | `"WARNING"` |
| `DEFAULT_LOG_FILE_PATH` | Путь к файлу лога | `"dds.log"` |
| `LOG_MAX_FILE_SIZE_MB` | Максимальный размер лог-файла до ротации | `10` |
| `LOG_BACKUP_COUNT` | Количество резервных лог-файлов | `5` |
| `API_EXECUTOR_MAX_WORKERS` | Потоков в пуле для веб-запросов | `4` |
| `SCAN_EXECUTOR_MAX_WORKERS` | Потоков в пуле для операций сканирования | `6` |
| `SCAN_EXTRACT_WORKERS` | **Процессов** для извлечения текста (`ProcessPoolExecutor`) | **`6`** |
| `SLOW_QUERY_THRESHOLD_MS` | Порог медленного SQL-запроса | `1000.0` |
| `EXPLAIN_SLOW_QUERIES` | Включать `EXPLAIN QUERY PLAN` | `True` |
| `EVENT_BUS_QUEUE_SIZE` | Размер очереди шины событий | `1000` |
| `SUBSCRIBER_QUEUE_SIZE` | Размер очереди подписчиков | `100` |
| `INDEX_STATUS_CACHE_TTL_SECONDS` | Время жизни кэша состояния индексации | `300` |
| `INDEX_STATUS_MIN_REFRESH_INTERVAL_SECONDS` | Мин. интервал принудительного пересчёта | `30` |
| `SESSION_CLEANUP_INTERVAL_SECONDS` | Интервал фоновой очистки истёкших сессий | `300` |
| `PASSWORD_MIN_LENGTH` | Минимальная длина пароля | `10` |
| `OPERATION_TIMEOUTS` | Реестр таймаутов блокирующих операций | см. `config.py` |

> **Примечание:** Для production рекомендуется оставить
> `DEFAULT_LOG_LEVEL = "WARNING"`, чтобы снизить объём логов.
> Разделение пулов потоков (`api_executor` и `scan_executor`) и
> пула процессов (`extract_executor`) предотвращает конкуренцию
> между веб-запросами, фоновым сканированием и извлечением текста.
> Каждый процесс `extract_executor` потребляет ~50–150 МБ памяти;
> при 6 процессах дополнительно требуется 300–900 МБ. На системах
> с объёмом памяти менее 8 ГБ рекомендуется уменьшить
> `SCAN_EXTRACT_WORKERS`.

## Запуск в development-режиме

```bash
# Активировать виртуальное окружение
source .venv/bin/activate

# Запустить DDS
python run.py

# Или с указанием конфигурации
python run.py --config /path/to/config.json
```

DDS будет доступен по адресу: `http://localhost:8000`

### Особенности запуска

- **Lifespan-обработчик:** Инициализация компонентов, включая шину
  событий, подписчик логирования и пулы потоков/процессов, выполняется
  внутри lifespan-обработчика FastAPI сразу после старта uvicorn.
- **Файловое логирование:** При старте настраивается
  `RotatingFileHandler` для записи логов в файл с ротацией.
- **Сброс зависших записей:** При старте автоматически вызывается
  `reset_stale_running_scans()`, который помечает записи со
  статусом `running` (оставшиеся после аварийного завершения)
  как `interrupted`.
- **Парольная политика:** При загрузке пользователей из `config.json`
  выполняется проверка надёжности паролей. Слабые пароли отклоняются.
- **Фоновая очистка сессий:** При старте создаётся асинхронная задача,
  которая каждые `SESSION_CLEANUP_INTERVAL_SECONDS` секунд удаляет
  истёкшие сессии.
- **Пул процессов извлечения текста:** На Linux используется контекст
  `fork`, на других ОС — `spawn`. Это гарантирует корректную работу
  с ресурсами и совместимость.

## Запуск в production-режиме

### Вариант 1: systemd (Linux)

#### Создание сервиса

```bash
sudo tee /etc/systemd/system/dds.service > /dev/null << 'EOF'
[Unit]
Description=Deep Doc Search — система поиска по рабочей документации
After=network.target

[Service]
Type=simple
User=dds
Group=dds
WorkingDirectory=/opt/dds/python
Environment="DDS_CONFIG=/opt/dds/config.json"
ExecStart=/opt/dds/python/.venv/bin/python run.py
Restart=always
RestartSec=10

# Безопасность
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/dds /var/log/dds

# Ресурсы
LimitNOFILE=65536
MemoryMax=3G

[Install]
WantedBy=multi-user.target
EOF
```

#### Создание пользователя и каталогов

```bash
# Создать пользователя
sudo useradd --system --home-dir /opt/dds --shell /usr/sbin/nologin dds

# Создать каталоги
sudo mkdir -p /opt/dds
sudo mkdir -p /var/lib/dds
sudo mkdir -p /var/log/dds

# Скопировать файлы
sudo cp -r . /opt/dds/python/
sudo cp config.json /opt/dds/config.json

# Установить права
sudo chown -R dds:dds /opt/dds
sudo chown -R dds:dds /var/lib/dds
sudo chown -R dds:dds /var/log/dds
```

#### Запуск сервиса

```bash
sudo systemctl daemon-reload
sudo systemctl enable dds
sudo systemctl start dds

# Проверить статус
sudo systemctl status dds
```

### Вариант 2: Docker

#### Dockerfile

```dockerfile
FROM python:3.12-slim

# Установить системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Создать каталог приложения
WORKDIR /app

# Скопировать зависимости и установить их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Скопировать код приложения
COPY dds_core/ dds_core/
COPY dds_web/ dds_web/
COPY dds_modules/ dds_modules/
COPY run.py .
COPY config.json.example config.json

# Создать каталоги для БД и логов
RUN mkdir -p /data/db /data/logs

# Переменные окружения
ENV DDS_CONFIG=/app/config.json
ENV PYTHONUNBUFFERED=1

# Порт
EXPOSE 8000

# Запуск
CMD ["python", "run.py"]
```

#### docker-compose.yml

```yaml
version: "3.8"

services:
  dds:
    build: .
    container_name: dds
    ports:
      - "8000:8000"
    volumes:
      # Каталог РД (только чтение)
      - /mnt/shared/rd_documents:/data/rd:ro
      # Каталог БД (чтение/запись)
      - dds_data:/data/db
      # Каталог логов (чтение/запись)
      - dds_logs:/data/logs
      # Конфигурация
      - ./config.production.json:/app/config.json:ro
    environment:
      - DDS_CONFIG=/app/config.json
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 3G
        reservations:
          memory: 1G
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/scan/status')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

volumes:
  dds_data:
  dds_logs:
```

## Обновление системы

### Обновление без Docker

```bash
# Остановить сервис
sudo systemctl stop dds

# Создать резервную копию БД
sudo cp /var/lib/dds/dds_database.db /var/lib/dds/dds_database.db.backup.$(date +%Y%m%d)

# Обновить код
cd /opt/dds/python
sudo -u dds git pull
sudo -u dds .venv/bin/pip install -r requirements.txt

# Запустить сервис
sudo systemctl start dds
```

## Резервное копирование

### Ручное резервное копирование

```bash
# Остановить DDS (для консистентности БД)
sudo systemctl stop dds

# Скопировать БД
sudo cp /var/lib/dds/dds_database.db /backup/dds_database_$(date +%Y%m%d).db

# Запустить DDS
sudo systemctl start dds
```

### Горячее резервное копирование (без остановки)

SQLite в режиме WAL позволяет создавать резервные копии без остановки:

```bash
sqlite3 /var/lib/dds/dds_database.db ".backup '/backup/dds_database_hot.db'"
```

## Мониторинг и логирование

### Файловое логирование с ротацией

При старте приложения настраивается файловое логирование через
`RotatingFileHandler`. Это обеспечивает сохранение истории логов
без переполнения диска.

| Параметр | Описание | Значение |
|---|---|---|
| Путь | `DEFAULT_LOG_FILE_PATH` | `dds.log` |
| Макс. размер | `LOG_MAX_FILE_SIZE_MB` | 10 МБ |
| Резервные копии | `LOG_BACKUP_COUNT` | 5 файлов |
| Формат | `%(asctime)s \| %(levelname)s \| %(name)s \| %(message)s` | — |

### Фильтрация событий

Подписчик логирования (`LoggingSubscriber`) фильтрует события
по уровню `DEFAULT_LOG_LEVEL` (по умолчанию `WARNING`).
События с уровнем ниже `WARNING` (например, `DEBUG`, `INFO`)
не передаются в очередь подписчика, что снижает нагрузку на
шину событий и потребление памяти.

### Проверка статуса через API

```bash
# Статус сканирования (живой прогресс из памяти конвейера)
curl http://localhost:8000/api/scan/status

# Кэшированное состояние индексации
curl http://localhost:8000/api/index/status

# Принудительный пересчёт состояния индексации
curl "http://localhost:8000/api/index/status?refresh=true"

# Диагностическая информация (только для администраторов)
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/diagnostics
```

### Прогресс сканирования в реальном времени (SSE)

```bash
curl -N http://localhost:8000/api/scan/progress
```

Эндпоинт возвращает начальное состояние при подключении,
затем подписывается на события `scan.progress` и `scan.completed`.

## Таймауты операций

Все блокирующие операции имеют таймауты, заданные в
`dds_core/domain/config.py` (`OPERATION_TIMEOUTS`).
При срабатывании таймаута в лог записывается событие
`operation.timed_out` на уровне WARNING.

Для изменения таймаутов отредактируйте файл
`dds_core/domain/config.py` и перезапустите DDS.

Реестр таймаутов:

| Дескриптор | Таймаут, с | Операция |
|---|---|---|
| `scan.extract` | 300 | Извлечение текста одного документа в `ProcessPoolExecutor` |
| `scan.directory_scan` | 600 | Обход каталога РД (`os.walk`) |
| `scan.db_write_batch` | 60 | Запись батча документов в БД |
| `scan.cache_load` | 30 | Загрузка кэша таблицы `documents` в память |
| `scan.cancel_grace` | 5 | Ожидание завершения конвейера при отмене |
| `index.refresh` | 600 | Полный пересчёт состояния индексации |
| `search.query` | 30 | FTS5-запрос поиска |
| `render.page` | 30 | Генерация PNG-рендера страницы (`ITextDocument.render_page`) в `scan_executor` |
| `render.highlights` | 30 | Построение `WordIndex` и поиск совпадений в `scan_executor` |
| `document.page_load` | 30 | Загрузка текста страницы документа |
| `document.delete` | 30 | Удаление документа из индекса |
| `auth.login` | 10 | Хеширование пароля (PBKDF2, 100 000 итераций) |
| `sse.subscription_wait` | 60 | Ожидание события в SSE-подписке (heartbeat) |

Значение `0` отключает таймаут для соответствующей операции.

## Устранение неполадок

### Зависшие записи сканирования

**Симптом:** После аварийного завершения (сбой питания, `SIGKILL`)
в таблице `scan_state` остаётся запись со статусом `running`,
что блокирует запуск нового сканирования.

**Решение:**
Ручное вмешательство не требуется. При следующем запуске DDS
автоматически вызывает `reset_stale_running_scans()`, который
помечает все записи со статусом `running` как `interrupted`.

### Тяжёлые запросы состояния индексации

**Симптом:** Страница `/scan` или API `/api/index/status`
отвечают медленно.

**Причина:**
Определение состояния индексации требует сканирования каталога
и хеширования изменённых файлов.

**Решение:**
По умолчанию используется кэшированное состояние
(`get_cached_index_status`), которое не выполняет тяжёлых
операций. Принудительный пересчёт выполняется только:
1. По нажатию кнопки «Обновить состояние» на странице `/scan`.
2. Один раз после завершения сканирования (через SSE).
3. При явном указании `?refresh=true` в API.

### Ошибка "database is locked"

SQLite в режиме WAL поддерживает параллельное чтение, но только
одну запись одновременно.

**Решение:**
1. Проверить, что нет других процессов, пишущих в БД.
2. Убедиться, что используется пул соединений (`ConnectionPool`)
   с безопасным возвратом соединений (обработка ошибок `rollback`).
3. Увеличить `DB_BUSY_TIMEOUT_MS` в `config.py`.

### Медленные запросы `db.query_slow`

Если в логах появляются события `db.query_slow`, это указывает на
запросы, превышающие порог `SLOW_QUERY_THRESHOLD_MS` (1000 мс).

**Действия:**
1. Проверить план выполнения запроса (`EXPLAIN QUERY PLAN`),
   который автоматически включается в событие `DatabaseQuerySlow`.
2. Убедиться, что состояние индексации запрашивается через кэш,
   а не через принудительный пересчёт.

### Потребление памяти процессами извлечения

При использовании `ProcessPoolExecutor` каждый дочерний процесс
потребляет 50–150 МБ. При 6 процессах суммарное дополнительное
потребление может достигать 900 МБ. Если система испытывает
нехватку памяти, уменьшите `SCAN_EXTRACT_WORKERS` в
`dds_core/domain/config.py` (например, до 4 или 2).

## Безопасность

### Парольная политика

При запуске DDS выполняет проверку надёжности паролей, указанных
в `config.json`. Пароль отклоняется (пользователь не добавляется),
если нарушено хотя бы одно правило:

| Правило | Описание |
|---|---|
| Минимальная длина | Не менее `PASSWORD_MIN_LENGTH` (10) символов |
| Чёрный список | Пароль не входит в список распространённых паролей |
| Совпадение с логином | Пароль не совпадает с именем пользователя |
| Чисто цифровой | Пароль не состоит только из цифр |

### Безопасность редиректов

Все редиректы (например, после входа через параметр `next`)
проверяются единой функцией `is_safe_redirect_path()` из
`dds_web/security.py`. Это предотвращает open redirect-атаки,
разрешая только относительные пути без схемы URL.

### Безопасность cookie

Флаг `secure` у сессионных cookie устанавливается автоматически
через `should_use_secure_cookie()`, если приложение работает:
- Напрямую по HTTPS.
- За обратным прокси с заголовком `X-Forwarded-Proto: https`.

### Рекомендации

1. **Смените пароль администратора** в `config.json` перед запуском.
2. **Используйте HTTPS** через обратный прокси (nginx, Caddy).
3. **Ограничьте доступ** к порту 8000 через файрвол.
4. **Регулярно обновляйте** зависимости.

### Пример nginx-конфигурации для HTTPS

```nginx
server {
    listen 443 ssl http2;
    server_name dds.example.com;

    ssl_certificate /etc/letsencrypt/live/dds.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dds.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name dds.example.com;
    return 301 https://$server_name$request_uri;
}
```

---

## Сводка по файлу

| Раздел | Содержание |
|---|---|
| Требования к системе | Обновлены: RAM 8 ГБ, CPU 6+ ядер, память процессов |
| Установка | Без изменений |
| Настройка конфигурации | Обновлена таблица дополнительных параметров: `SCAN_EXTRACT_WORKERS` теперь процессы, добавлено примечание о памяти, добавлен `OPERATION_TIMEOUTS` |
| Запуск в development | Добавлено упоминание `ProcessPoolExecutor` и выбора `fork`/`spawn` |
| Запуск в production | Увеличено `MemoryMax` в systemd до 3G; в Docker `memory: 3G` с reservations |
| Обновление системы | Без изменений |
| Резервное копирование | Без изменений |
| Мониторинг | Без существенных изменений |
| Таймауты операций | **Новый раздел**: реестр `OPERATION_TIMEOUTS`, таблица дескрипторов, инструкция по изменению |
| Устранение неполадок | Добавлен раздел «Потребление памяти процессами извлечения» |
| Безопасность | Без изменений |
| Пример nginx | Без изменений |

```
