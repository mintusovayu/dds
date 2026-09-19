"""
Сканер каталога рабочей документации.

Этот модуль реализует интерфейс IScanner доменного слоя,
обеспечивая обнаружение файлов в каталоге РД. Модуль
использует стандартную библиотеку Python для работы
с файловой системой.

Модуль находится в инфраструктурном слое и является единственной
точкой доступа к файловой системе для сканирования. Доменный слой
не знает о файловой системе — он использует только интерфейс IScanner.

Функциональность:
    - Рекурсивный обход каталога.
    - Фильтрация файлов по расширению.
    - Получение метаданных файла (размер, дата изменения).
    - Пропуск недоступных файлов и каталогов.

Принципы:
    - Модуль реализует интерфейс доменного слоя (инверсия зависимостей).
    - Модуль является единственной точкой доступа к файловой системе
      для сканирования.
    - Модуль не содержит бизнес-логики (хеширование, индексирование).
    - Модуль не выполняет логирование. Логирование — ответственность
      application layer.

Реализуемые интерфейсы:
    IScanner — абстракция сканера каталога.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from ..domain import config

# ----------------------------------------------------------------------
# Сканер каталога
# ----------------------------------------------------------------------


class DirectoryScanner:
    """Сканер каталога рабочей документации.

    Реализует интерфейс IScanner. Рекурсивно обходит каталог
    и возвращает список файлов, соответствующих заданным
    расширениям.

    Пример использования:

        scanner = DirectoryScanner()
        files = scanner.scan_directory("/path/to/rd")
        for file_path in files:
            info = scanner.get_file_info(file_path)

    Attributes:
        _extensions: Кортеж расширений файлов для сканирования.
    """

    def __init__(self, extensions: tuple[str, ...] | None = None) -> None:
        """Инициализирует сканер каталога.

        Args:
            extensions: Кортеж расширений файлов для сканирования.
                По умолчанию используется SCAN_FILE_EXTENSIONS
                из config. Расширения должны начинаться с точки.
                Например, (".pdf", ".docx").
        """
        self._extensions = extensions if extensions is not None else config.SCAN_FILE_EXTENSIONS

    def scan_directory(self, directory: str) -> list[str]:
        """Сканирует каталог и возвращает список файлов.

        Рекурсивно обходит каталог и возвращает абсолютные пути
        ко всем файлам, чьи расширения входят в self._extensions.

        Недоступные файлы и каталоги пропускаются (без исключения).
        Символические ссылки не обрабатываются (для предотвращения
        циклов).

        Args:
            directory: Путь к каталогу для сканирования.

        Returns:
            Список абсолютных путей к файлам. Файлы отсортированы
            по алфавиту для детерминированного порядка обработки.
            Пустой список, если каталог пуст или не существует.
        """
        if not os.path.isdir(directory):
            return []

        result: list[str] = []

        for root, dirs, files in os.walk(directory, followlinks=False):
            # Пропустить недоступные каталоги
            dirs[:] = [d for d in dirs if os.access(os.path.join(root, d), os.R_OK)]

            for file_name in files:
                file_path = os.path.join(root, file_name)

                # Проверить расширение
                if not any(file_name.lower().endswith(ext.lower()) for ext in self._extensions):
                    continue

                # Проверить доступность файла
                if not os.access(file_path, os.R_OK):
                    continue

                # Пропустить символические ссылки
                if os.path.islink(file_path):
                    continue

                result.append(os.path.abspath(file_path))

        result.sort()
        return result

    def get_file_info(self, file_path: str) -> dict:
        """Возвращает информацию о файле.

        Args:
            file_path: Путь к файлу.

        Returns:
            Словарь с информацией о файле:
                "file_path" (str) — абсолютный путь к файлу.
                "file_name" (str) — имя файла.
                "file_size" (int) — размер файла в байтах.
                "last_modified" (str) — дата и время последнего
                    изменения файла в формате ISO 8601 (UTC).
                    Например, "2024-01-15T10:30:00+00:00".

        Raises:
            FileNotFoundError: Если файл не найден.
            OSError: Если файл недоступен.
        """
        stat = os.stat(file_path)
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)

        return {
            "file_path": os.path.abspath(file_path),
            "file_name": os.path.basename(file_path),
            "file_size": stat.st_size,
            "last_modified": mtime.isoformat(),
        }

    def get_relative_path(self, file_path: str, base_directory: str) -> str:
        """Возвращает относительный путь файла от базового каталога.

        Используется для записи file_path в таблицу documents.
        Относительный путь позволяет перемещать каталог РД
        без изменения данных в БД.

        Args:
            file_path: Абсолютный путь к файлу.
            base_directory: Базовый каталог (каталог РД).

        Returns:
            Относительный путь файла от базового каталога.
            Например, "раздел_01/чертёж_001.pdf".
            Если файл не находится внутри базового каталога,
            возвращается абсолютный путь.
        """
        try:
            return os.path.relpath(file_path, base_directory)
        except ValueError:
            # Файл на другом диске (Windows)
            return os.path.abspath(file_path)
