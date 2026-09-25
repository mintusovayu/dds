"""
Извлечение текстового слоя через PyMuPDF.

Этот модуль реализует интерфейсы доменного слоя для извлечения
текстового слоя из PDF-документов через библиотеку PyMuPDF (fitz),
а также для рендера страниц и построения нормализованных индексов
слов.

Модуль находится в инфраструктурном слое и является единственной
точкой зависимости от PyMuPDF. Доменный слой не знает о PyMuPDF —
он использует только интерфейсы из
``dds_core/domain/interfaces.py``.

Реализуемые интерфейсы:

+----------------------------------+----------------------------------+
| Интерфейс                        | Реализующий класс                |
+==================================+==================================+
| ``ITextDocument``                | ``PyMuPDFTextDocument``          |
+----------------------------------+----------------------------------+
| ``ITextExtractor``               | ``PyMuPDFTextExtractor``         |
+----------------------------------+----------------------------------+

Архитектурные решения:

+----------------------------------+----------------------------------+
| Решение                          | Обоснование                      |
+==================================+==================================+
| ``PyMuPDFTextDocument``          | Инкапсулирует все детали работы  |
| инкапсулирует работу             | с документом PyMuPDF. Доменный   |
| с документом                     | слой (``TextIndexer``,           |
|                                  | ``api.py``) не знает о           |
|                                  | ``fitz.Document``,               |
|                                  | ``doc.load_page()``,             |
|                                  | ``doc.close()``.                 |
+----------------------------------+----------------------------------+
| Обработка ошибок на уровне       | Повреждённая страница не         |
| страницы                         | прерывает индексирование         |
|                                  | всего документа.                 |
+----------------------------------+----------------------------------+
| Перехват ошибок PyMuPDF в stderr | Ошибки, выводимые PyMuPDF в      |
| при открытии и извлечении        | stderr (например, «syntax        |
|                                  | error: invalid key in dict»),    |
|                                  | преобразуются в исключение,      |
|                                  | чтобы вызывающий код мог         |
|                                  | корректно обработать файл        |
|                                  | как ошибочный и опубликовать     |
|                                  | событие ``FileProcessingFailed``.|
+----------------------------------+----------------------------------+
| Модульный ``threading.Lock``     | Подмена fd 2 через ``os.dup2``   |
| вокруг ``_capture_stderr``       | — операция над процессным        |
|                                  | ресурсом. При параллельных       |
|                                  | вызовах из ``scan_executor``     |
|                                  | (например, одновременные         |
|                                  | запросы ``/render`` и            |
|                                  | ``/highlights``) два потока      |
|                                  | подменяют fd друг другу. Lock    |
|                                  | сериализует последовательность   |
|                                  | ``dup → dup2 → func → dup2 →     |
|                                  | read``. Временная мера;          |
|                                  | устраняется в Фазе 4 изоляцией   |
|                                  | PyMuPDF в отдельные процессы     |
|                                  | (``ProcessTaskRunner``). См.     |
|                                  | ADR-001.                         |
+----------------------------------+----------------------------------+
| Автоматическое уменьшение DPI    | Защита от OOM при рендере        |
| при превышении лимита пикселей   | больших форматов (A0, A1) на     |
|                                  | высоком DPI.                     |
+----------------------------------+----------------------------------+
| Индекс слов строится с           | Поиск подсветки устойчив         |
| нормализованными ключами         | к смешению кириллицы и           |
|                                  | латиницы в PDF и сниппетах FTS5. |
+----------------------------------+----------------------------------+

Использование ``normalize_text`` из domain-слоя:

Модуль импортирует :func:`normalize_text` из
``dds_core.domain.text_normalization``. Правило нормализации —
**доменное**: оно определяет семантику поиска в DDS и должно быть
единым контрактом для infrastructure (индексация, построение
``WordIndex``), application (сниппеты) и presentation (отображение).

Ранее нормализация находилась в
``dds_core/application/text_normalizer.py``, и infrastructure была
вынуждена импортировать функцию из application-слоя — это нарушало
направление зависимостей. Перенос модуля в domain (Фаза 3,
ADR-003) устраняет компромисс: infrastructure теперь зависит от
domain, что соответствует слоистой архитектуре.

Отсутствие поля ``block_no`` в предыдущих версиях:

До Фазы 3 модель ``WordEntry`` не содержала поля ``block_no``,
и группировка по ``by_line`` выполнялась через параллельный
список ``block_line_pairs`` с инвариантом «индекс i в обоих
списках соответствует одному слову». Инвариант был хрупким:
любая фильтрация в цикле сломала бы соответствие. В Фазе 3
поле ``block_no`` добавлено в :class:`WordEntry`, инвариант
устранён — ``build_word_index`` строит ``by_line`` напрямую через
``entry.block_no``.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Self

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from ..domain import config as core_config
from ..domain.models import WordEntry, WordIndex
from ..domain.text_normalization import normalize_text

# ----------------------------------------------------------------------
# Модульная блокировка для перехвата stderr
# ----------------------------------------------------------------------

_stderr_lock: threading.Lock = threading.Lock()
"""Блокировка, сериализующая операции подмены fd 2.

PyMuPDF пишет диагностику в файловый дескриптор 2 (fd 2) —
**процессный** ресурс, не потоковый. :func:`_capture_stderr`
временно подменяет fd 2 через :func:`os.dup2`, выполняет функцию
и восстанавливает оригинальный дескриптор. При параллельном
вызове из нескольких потоков (например, ``scan_executor``
обрабатывает одновременно ``POST /render`` и
``POST /highlights``) последовательности ``dup → dup2 → func →
dup2 → read`` перекрываются, что приводит к перепутанному
выводу и потере диагностики.

Блокировка сериализует критические секции: одновременно
``_capture_stderr`` может выполняться не более чем в одном
потоке процесса.

Примечание:
    Мера временная. В Фазе 4 PyMuPDF-операции изолируются в
    отдельные процессы (``ProcessTaskRunner``), где каждый
    воркер владеет своим fd 2 и блокировка становится ненужной.
    Решение и альтернативы зафиксированы в ADR-001.
"""


def _capture_stderr(func, *args, **kwargs):
    """Вызывает функцию, перехватывая низкоуровневый stderr (fd 2).

    PyMuPDF пишет сообщения об ошибках напрямую в файловый дескриптор 2,
    минуя ``sys.stderr``. Эта функция временно подменяет fd 2 на канал,
    выполняет переданную функцию, восстанавливает stderr и возвращает
    кортеж ``(результат, перехваченный_вывод)``.

    Потокобезопасность:
        Подмена fd 2 — операция над **процессным** ресурсом, а не
        потоковым. Все операции функции (``os.dup`` → ``os.dup2``
        → вызов ``func`` → ``os.dup2`` → ``os.read`` → ``os.close``)
        выполняются под модульной блокировкой :data:`_stderr_lock`.
        Это гарантирует, что одновременно в процессе выполняется
        не более одного вызова ``_capture_stderr``. Без блокировки
        параллельные вызовы из ``scan_executor`` (например,
        одновременные ``POST /render`` и ``POST /highlights``)
        перекрывали бы fd 2 друг другу.

    Временный характер меры:
        Блокировка сериализует PyMuPDF-операции в рамках процесса
        и снижает throughput рендера/подсветки при конкурентных
        запросах. В Фазе 4 PyMuPDF-операции изолируются в
        отдельные процессы, где блокировка становится ненужной.
        См. ADR-001.

    Операции (все под ``_stderr_lock``):

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Захват ``_stderr_lock``.                            |
    +---+-----------------------------------------------------+
    | 2 | Сохранение текущего fd 2 через ``os.dup(2)``.       |
    +---+-----------------------------------------------------+
    | 3 | Создание канала (pipe).                             |
    +---+-----------------------------------------------------+
    | 4 | Подмена fd 2 на записывающий конец канала через     |
    |   | ``os.dup2(write_fd, 2)``.                           |
    +---+-----------------------------------------------------+
    | 5 | Вызов ``func(*args, **kwargs)``.                    |
    +---+-----------------------------------------------------+
    | 6 | Восстановление fd 2 из сохранённого.                |
    +---+-----------------------------------------------------+
    | 7 | Чтение перехваченного вывода из канала.             |
    +---+-----------------------------------------------------+
    | 8 | Возврат результата и строки вывода.                 |
    +---+-----------------------------------------------------+
    | 9 | Освобождение ``_stderr_lock`` (``finally``).        |
    +---+-----------------------------------------------------+
    """
    with _stderr_lock:
        saved_fd = os.dup(2)
        read_fd, write_fd = os.pipe()
        os.dup2(write_fd, 2)
        os.close(write_fd)
        try:
            result = func(*args, **kwargs)
        finally:
            os.dup2(saved_fd, 2)
            os.close(saved_fd)
            try:
                data = os.read(read_fd, 65536).decode("utf-8", errors="replace")
            except OSError:
                data = ""
            finally:
                os.close(read_fd)
        return result, data


class PyMuPDFTextDocument:
    """Обёртка документа PyMuPDF, реализующая ``ITextDocument``.

    Инкапсулирует все операции с документом PyMuPDF:
    получение количества страниц, извлечение текста страницы,
    рендер страницы в PNG, построение нормализованного индекса
    слов страницы, закрытие документа. Доменный слой и веб-слой
    взаимодействуют только с интерфейсом ``ITextDocument``,
    не зная о внутренней структуре PyMuPDF.

    Жизненный цикл:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | ``PyMuPDFTextExtractor.open_document(path)``        |
    |   | → создаёт и возвращает ``PyMuPDFTextDocument``.     |
    +---+-----------------------------------------------------+
    | 2 | ``PyMuPDFTextDocument.page_count()``                |
    |   | → количество страниц.                               |
    +---+-----------------------------------------------------+
    | 3 | ``PyMuPDFTextDocument.get_page_text(i)``            |
    |   | → текст страницы. Вызывается для каждой страницы.   |
    +---+-----------------------------------------------------+
    | 4 | ``PyMuPDFTextDocument.render_page(i, dpi)``         |
    |   | → PNG-рендер страницы (при предпросмотре).          |
    +---+-----------------------------------------------------+
    | 5 | ``PyMuPDFTextDocument.build_word_index(i)``         |
    |   | → индекс слов для подсветки (при предпросмотре).    |
    +---+-----------------------------------------------------+
    | 6 | ``PyMuPDFTextDocument.close()``                     |
    |   | → освобождение ресурсов.                            |
    +---+-----------------------------------------------------+

    Потокобезопасность:
    Экземпляр документа не является потокобезопасным.
    Каждый документ должен обрабатываться одним потоком.
    При параллельном сканировании через ``ScanPipeline``
    каждый воркер работает с собственным экземпляром документа.

    Дополнительно: методы ``get_page_text``, ``render_page`` и
    ``build_word_index`` вызывают :func:`_capture_stderr`, который
    сериализует операции подмены fd 2 через модульную
    :data:`_stderr_lock`. Это ограничивает одновременное
    выполнение этих методов в рамках процесса.

    Пример использования::

        extractor = PyMuPDFTextExtractor()
        doc = extractor.open_document("/path/to/document.pdf")
        try:
            count = doc.page_count()
            for i in range(count):
                text = doc.get_page_text(i)
            png = doc.render_page(0, dpi=300)
            index = doc.build_word_index(0)
        finally:
            doc.close()

    Attributes:

    +---------------------+------------------------------------------+
    | Атрибут             | Описание                                 |
    +=====================+==========================================+
    | ``_doc``            | Внутренний объект ``fitz.Document``.     |
    +---------------------+------------------------------------------+
    | ``_closed``         | Флаг закрытия документа.                 |
    +---------------------+------------------------------------------+
    """

    def __init__(self, doc: Any) -> None:
        """Инициализирует обёртку документа."""
        self._doc = doc
        self._closed = False

    def page_count(self) -> int:
        """Возвращает количество страниц в документе."""
        if self._closed:
            raise RuntimeError("Документ закрыт.")
        try:
            return len(self._doc)
        except TypeError:
            raise ValueError(
                f"Объект {type(self._doc).__name__} не является документом PyMuPDF."
            ) from None

    def get_page_text(self, page_index: int) -> str:
        """Извлекает текстовое содержимое страницы по индексу.

        Перехватывает низкоуровневый stderr PyMuPDF и при обнаружении
        ошибки выбрасывает ``RuntimeError``, чтобы вызывающий код
        мог пометить документ как ошибочный.

        Args:
            page_index: Индекс страницы (0-based).

        Returns:
            Текстовое содержимое страницы или пустая строка.

        Raises:
            RuntimeError: Если документ закрыт или PyMuPDF сообщил
                об ошибке в stderr.
            IndexError: Если ``page_index`` вне допустимого диапазона.
        """
        if self._closed:
            raise RuntimeError("Документ закрыт.")

        try:
            page = self._doc.load_page(page_index)
            # Перехватываем stderr на уровне файлового дескриптора
            text, err_output = _capture_stderr(page.get_text, "text")

            if "error" in err_output.lower():
                raise RuntimeError(
                    f"MuPDF error while extracting text from page {page_index}: "
                    f"{err_output.strip()}"
                )

            return text.strip() if text else ""
        except IndexError:
            raise
        except RuntimeError:
            raise
        except (ValueError, MemoryError, OSError):
            return ""
        except Exception:
            return ""

    def render_page(self, page_index: int, dpi: int) -> bytes:
        """Возвращает PNG-рендер страницы в заданном DPI.

        Автоматическое уменьшение DPI:
        Если произведение ``page.rect.width * page.rect.height *
        (dpi/72)^2`` превышает ``config.MAX_RENDER_PIXELS``, DPI
        понижается пропорционально для защиты от OOM и от
        непропорционально долгой генерации PNG. Если даже при
        минимальном DPI (72) лимит превышен, метод выбрасывает
        ``RuntimeError``.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Проверка, что документ не закрыт.                   |
        +---+-----------------------------------------------------+
        | 2 | Загрузка страницы по индексу.                       |
        +---+-----------------------------------------------------+
        | 3 | Расчёт эффективного DPI с учётом лимита пикселей.   |
        +---+-----------------------------------------------------+
        | 4 | Проверка, что при 72 DPI страница укладывается в    |
        |   | лимит. Иначе — ``RuntimeError``.                    |
        +---+-----------------------------------------------------+
        | 5 | Генерация pixmap с эффективным DPI (``alpha=False``).|
        +---+-----------------------------------------------------+
        | 6 | Перехват stderr PyMuPDF; при ошибке — исключение.   |
        +---+-----------------------------------------------------+
        | 7 | Возврат PNG-байтов.                                 |
        +---+-----------------------------------------------------+

        Args:
            page_index: Индекс страницы (0-based).
            dpi: Разрешение рендера в точках на дюйм.

        Returns:
            Байты PNG-изображения страницы (RGB, без альфы).

        Raises:
            RuntimeError: Если документ закрыт, страница слишком
                большая даже при минимальном DPI, или PyMuPDF
                сообщил об ошибке в stderr.
            IndexError: Если ``page_index`` вне допустимого диапазона.
        """
        if self._closed:
            raise RuntimeError("Документ закрыт.")

        try:
            page = self._doc.load_page(page_index)
        except IndexError:
            raise
        except Exception as e:
            raise RuntimeError(f"Не удалось загрузить страницу {page_index}: {e}") from e

        # Расчёт эффективного DPI с учётом MAX_RENDER_PIXELS.
        page_w_pt = float(page.rect.width)
        page_h_pt = float(page.rect.height)
        scale = dpi / 72.0
        pixels = (page_w_pt * scale) * (page_h_pt * scale)

        effective_dpi = dpi
        if pixels > core_config.MAX_RENDER_PIXELS:
            factor = (core_config.MAX_RENDER_PIXELS / pixels) ** 0.5
            effective_dpi = int(dpi * factor)

            if effective_dpi < 72:
                pixels_at_72 = page_w_pt * page_h_pt  # scale=1
                raise RuntimeError(
                    f"Страница слишком большая: {pixels_at_72:.0f} пикселей "
                    f"при минимальном DPI 72 превышает лимит "
                    f"{core_config.MAX_RENDER_PIXELS}."
                )

        # Генерация pixmap с перехватом stderr.
        pixmap, err_output = _capture_stderr(
            page.get_pixmap,
            dpi=effective_dpi,
            alpha=False,
        )
        if err_output and "error" in err_output.lower():
            raise RuntimeError(
                f"MuPDF error while rendering page {page_index}: {err_output.strip()}"
            )

        return pixmap.tobytes("png")

    def build_word_index(self, page_index: int) -> WordIndex:
        """Строит нормализованный индекс слов страницы.

        Извлекает слова страницы через ``page.get_text("words")``
        и формирует :class:`WordIndex` для поиска совпадений
        и подсветки. Каждое слово представлено :class:`WordEntry`
        с координатами bbox, оригинальной и нормализованной формами,
        номерами блока, строки и позиции в строке.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Проверка, что документ не закрыт.                  |
        +----+----------------------------------------------------+
        | 2  | Загрузка страницы по индексу.                      |
        +----+----------------------------------------------------+
        | 3  | Извлечение слов через ``page.get_text("words")``   |
        |    | с перехватом stderr.                               |
        +----+----------------------------------------------------+
        | 4  | Для каждого слова:                                 |
        |    | a. ``.strip()``, пропуск пустых токенов.           |
        |    | b. Нормализация через ``normalize_text``.          |
        |    | c. Формирование :class:`WordEntry` со всеми        |
        |    |    полями, включая ``block_no``.                    |
        +----+----------------------------------------------------+
        | 5  | Заполнение ``by_normalized`` — группировка по      |
        |    | нормализованной форме.                             |
        +----+----------------------------------------------------+
        | 6  | Заполнение ``by_line`` — группировка по            |
        |    | ``(entry.block_no, entry.line_no)``.               |
        +----+----------------------------------------------------+
        | 7  | Сортировка списков внутри словарей.                |
        +----+----------------------------------------------------+
        | 8  | Возврат :class:`WordIndex`.                        |
        +----+----------------------------------------------------+

        Примечание:
            Возвращаемый :class:`WordIndex` сохраняется в
            ``WordIndexCache`` по ссылке. Мутация запрещена.

        Args:
            page_index: Индекс страницы (0-based).

        Returns:
            :class:`WordIndex` с нормализованными словами страницы
            и метаданными (``page_width``, ``page_height``,
            ``rotation``).

        Raises:
            RuntimeError: Если документ закрыт или PyMuPDF сообщил
                об ошибке в stderr.
            IndexError: Если ``page_index`` вне допустимого диапазона.
        """
        if self._closed:
            raise RuntimeError("Документ закрыт.")

        try:
            page = self._doc.load_page(page_index)
        except IndexError:
            raise
        except Exception as e:
            raise RuntimeError(f"Не удалось загрузить страницу {page_index}: {e}") from e

        # Извлечение слов с перехватом stderr.
        raw_words, err_output = _capture_stderr(page.get_text, "words")
        if err_output and "error" in err_output.lower():
            raise RuntimeError(
                f"MuPDF error while extracting words from page {page_index}: {err_output.strip()}"
            )

        # Формирование списка WordEntry.
        # Кортеж PyMuPDF: (x0, y0, x1, y1, text, block_no, line_no, word_no).
        entries: list[WordEntry] = []
        for item in raw_words:
            x0, y0, x1, y1, text, block_no, line_no, word_no = item
            text_stripped = text.strip()
            if not text_stripped:
                continue
            entries.append(
                WordEntry(
                    x0=float(x0),
                    y0=float(y0),
                    x1=float(x1),
                    y1=float(y1),
                    original=text_stripped,
                    normalized=normalize_text(text_stripped),
                    block_no=int(block_no),
                    line_no=int(line_no),
                    word_no=int(word_no),
                )
            )

        # by_normalized: группировка по нормализованной форме.
        by_normalized: dict[str, list[WordEntry]] = {}
        for entry in entries:
            by_normalized.setdefault(entry.normalized, []).append(entry)
        for lst in by_normalized.values():
            lst.sort(key=lambda e: (e.line_no, e.word_no))

        # by_line: группировка по (block_no, line_no).
        # Ключ формируется напрямую из полей WordEntry: block_no
        # добавлен в модель в Фазе 3 (ADR-003), параллельный список
        # block_line_pairs более не используется.
        by_line: dict[tuple[int, int], list[WordEntry]] = {}
        for entry in entries:
            key = (entry.block_no, entry.line_no)
            by_line.setdefault(key, []).append(entry)
        for lst in by_line.values():
            lst.sort(key=lambda e: e.word_no)

        return WordIndex(
            by_normalized=by_normalized,
            by_line=by_line,
            page_width=float(page.rect.width),
            page_height=float(page.rect.height),
            rotation=int(page.rotation),
        )

    def close(self) -> None:
        """Закрывает документ и освобождает ресурсы."""
        if self._closed:
            return
        self._closed = True
        try:
            self._doc.close()
        except Exception:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class PyMuPDFTextExtractor:
    """Извлечение текстового слоя через PyMuPDF."""

    def open_document(self, file_path: str) -> PyMuPDFTextDocument:
        """Открывает PDF-документ для извлечения текста.

        Перехватывает низкоуровневый stderr PyMuPDF и при обнаружении
        ошибки выбрасывает ``RuntimeError``.

        Args:
            file_path: Абсолютный путь к файлу документа.

        Returns:
            Объект ``PyMuPDFTextDocument``.

        Raises:
            RuntimeError: Если PyMuPDF сообщил об ошибке.
            FileNotFoundError: Если файл не найден.
            OSError: Если файл недоступен для чтения.
            Exception: Если файл повреждён или не может быть открыт.
        """
        # Перехватываем stderr на уровне файлового дескриптора
        doc, err_output = _capture_stderr(fitz.open, file_path)

        if "error" in err_output.lower():
            raise RuntimeError(f"MuPDF error while opening '{file_path}': {err_output.strip()}")

        return PyMuPDFTextDocument(doc)
