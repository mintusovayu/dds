"""
Точка входа Deep Doc Search.

Этот скрипт запускает веб-сервер DDS, используя FastAPI-приложение
с настроенным lifespan-обработчиком (см. ``dds_web/lifespan.py``).
Управление жизненным циклом (инициализация компонентов, загрузка
модулей, graceful shutdown) выполняется внутри lifespan, что
обеспечивает корректное завершение всех ресурсов.

Скрипт выполняет только:
- Чтение аргументов командной строки.
- Загрузку конфигурации для получения параметров хоста и порта.
- Создание приложения через фабрику ``create_app_with_lifespan``.
- Запуск сервера uvicorn.

Обработка сигналов (SIGINT, SIGTERM) выполняется самим uvicorn,
который корректно останавливает сервер и вызывает lifespan shutdown.

Принципы:
- Точка входа максимально тонкая: без бизнес-логики.
- Все компоненты инициализируются в lifespan.
- Graceful shutdown гарантируется механизмом lifespan.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> None:
    """Главная функция: запуск веб-сервера DDS.

    Операции:

    +----+----------------------------------------------------+
    | №  | Описание                                           |
    +====+====================================================+
    | 1  | Парсинг аргументов командной строки (``--config``).|
    +----+----------------------------------------------------+
    | 2  | Чтение конфигурации для получения ``host`` и       |
    |    | ``port``.                                          |
    +----+----------------------------------------------------+
    | 3  | Импорт ``create_app_with_lifespan`` из              |
    |    | ``dds_web.lifespan``.                              |
    +----+----------------------------------------------------+
    | 4  | Создание приложения через фабрику.                 |
    +----+----------------------------------------------------+
    | 5  | Настройка ``uvicorn.Config`` с параметрами из      |
    |    | конфигурации.                                      |
    +----+----------------------------------------------------+
    | 6  | Создание ``uvicorn.Server`` и запуск ``run()``.    |
    +----+----------------------------------------------------+
    | 7  | Обработка ``KeyboardInterrupt`` и прочих ошибок.   |
    +----+----------------------------------------------------+

    Обработка сигналов (SIGINT, SIGTERM) не выполняется явно:
    uvicorn сам перехватывает их, останавливает сервер и запускает
    lifespan shutdown, где происходит корректное завершение всех
    компонентов.

    Raises:
        SystemExit: Если конфигурация не найдена, повреждена,
            или если uvicorn не установлен.
    """
    parser = argparse.ArgumentParser(
        description="Deep Doc Search — система поиска и анализа рабочей документации."
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("DDS_CONFIG", "config.json"),
        help="Путь к файлу конфигурации (по умолчанию: config.json).",
    )
    args = parser.parse_args()

    # Чтение конфигурации для получения host/port.
    # Полная загрузка конфигурации выполняется в lifespan,
    # здесь же нужны только параметры сетевого подключения.
    try:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Ошибка: файл конфигурации не найден: {args.config}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Ошибка: файл конфигурации повреждён: {e}")
        sys.exit(1)

    dds_config = config.get("dds", {})
    host = dds_config.get("host", "0.0.0.0")
    port = dds_config.get("port", 8000)

    # Импорт фабрики приложения.
    from dds_web.lifespan import create_app_with_lifespan

    # Создание приложения с lifespan.
    print(f"INFO:     Чтение конфигурации: {args.config}")
    app = create_app_with_lifespan(args.config)

    # Настройка uvicorn.
    try:
        import uvicorn
    except ImportError:
        print("Ошибка: для запуска сервера необходимо установить uvicorn.")
        print("Установите: pip install uvicorn")
        sys.exit(1)

    server_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        lifespan="on",  # Включает обработку lifespan
    )
    server = uvicorn.Server(server_config)

    print(f"INFO:     Запуск сервера: http://{host}:{port}")

    try:
        server.run()
    except KeyboardInterrupt:
        print("\nINFO:     Остановка сервера (KeyboardInterrupt)...")
    except Exception as e:
        print(f"\nОшибка сервера: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
