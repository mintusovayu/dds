"""
Утилиты асинхронного выполнения блокирующих операций.

Модуль предоставляет общую корутину для запуска блокирующих функций
в выделенном исполнителе (по умолчанию — потоковом пуле). Это
устраняет дублирование кода в компонентах, которым требуется
выполнять синхронные операции (обращение к БД, файловой системе,
CPU-интенсивные задачи) без блокировки event loop.

Архитектурная роль:
    - Находится в application layer, не содержит бизнес-логики.
    - Инкапсулирует взаимодействие с `asyncio.loop.run_in_executor`.
    - Способствует инверсии зависимостей: компоненты зависят от
      абстракции исполнителя, а не от конкретного пула.

Принципы:
    - Код не зависит от конкретной реализации исполнителя.
    - Функция асинхронна и должна вызываться из event loop.
    - Не перехватывает исключения, пробрасывая их вызывающему коду.

Пример использования:
    ```python
    from dds_core.application.async_utils import run_blocking_in_executor

    result = await run_blocking_in_executor(
        executor=scan_executor,
        func=some_blocking_function,
        arg1,
        arg2,
    )
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Any, TypeVar

T = TypeVar("T")


async def run_blocking_in_executor[T](
    executor: Executor | None,
    func: Callable[..., T],
    *args: Any,
) -> T:
    """Выполняет блокирующую функцию в указанном исполнителе.

    Функция оборачивает вызов loop.run_in_executor(), позволяя
    запускать синхронные операции в отдельном потоке или процессе,
    не блокируя текущий event loop. Если executor равен None,
    используется исполнитель по умолчанию (asyncio сам выберет
    подходящий пул, обычно ThreadPoolExecutor).

    Args:
    executor: Исполнитель (ThreadPoolExecutor или
    ProcessPoolExecutor), в котором будет выполнена
    функция. Если None — используется исполнитель
    по умолчанию.
    func: Блокирующая функция для выполнения.
    *args: Позиционные аргументы, передаваемые в func.

    Returns:
    Результат, возвращаемый функцией func.

    Raises:
    Любое исключение, возникшее внутри func, пробрасывается
    вызывающему коду без изменений.

    Note:
    Функция должна вызываться только из асинхронного контекста
    (внутри корутины, где доступен running event loop).

    Example:

    python
    loop = asyncio.get_running_loop()
    result = await run_blocking_in_executor(
        executor=scan_executor,
        func=some_blocking_operation,
        "arg1",
        42,
    )
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func, *args)
