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
| Автоматическое уменьшение DPI    | Защита от OOM при рендере        |
| при превышении лимита пикселей   | больших форматов (A0, A1) на     |
|                                  | высоком DPI.                     |
+----------------------------------+----------------------------------+
| Индекс слов строится с           | Поиск подсветки устойчив         |
| нормализованными ключами         | к смешению кириллицы и           |
|                                  | латиницы в PDF и сниппетах FTS5. |
+----------------------------------+----------------------------------+
| Параллельные списки              | ``entries`` и ``block_line_pairs`` |
| ``entries``/``block_line_pairs`` | заполняются синхронно в одном    |
| в ``build_word_index``           | цикле — это инвариант,           |
|                                  | обеспечивающий корректную        |
|                                  | группировку по ``(block_no,      |
|                                  | line_no)`` при отсутствии поля   |
|                                  | ``block_no`` в модели            |
|                                  | ``WordEntry``.                   |
+----------------------------------+----------------------------------+

Архитектурный компромисс (infrastructure → application):

Модуль импортирует функцию :func:`normalize_text` из
``dds_core.application.text_normalizer``. Формально это нарушает
направление зависимостей (infrastructure зависит от application),
но компромисс осознан:

- ``normalize_text`` — чистая функция без побочных эффектов и
  внешних зависимостей.
- Её назначение — быть **единым контрактом нормализации** между
  инфраструктурой (индексация, построение ``WordIndex``),
  application (поиск, сниппеты) и presentation (подсветка).
- Дублирование функции в домене или инфраструктуре привело бы
  к риску рассинхронизации правил нормализации.
"""

from __future__ import annotations

import os
from typing import Any, Self

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore[no-redef]

from ..application.text_normalizer import normalize_text
from ..domain import config as core_config
from ..domain.models import WordEntry, WordIndex


def _capture_stderr(func, *args, **kwargs):
    """Вызывает функцию, перехватывая низкоуровневый stderr (fd 2).

    PyMuPDF пишет сообщения об ошибках напрямую в файловый дескриптор 2,
    минуя ``sys.stderr``. Эта функция временно подменяет fd 2 на канал,
    выполняет переданную функцию, восстанавливает stderr и возвращает
    кортеж ``(результат, перехваченный_вывод)``.

    Операции:

    +---+-----------------------------------------------------+
    | № | Описание                                            |
    +===+=====================================================+
    | 1 | Сохранение текущего fd 2 через ``os.dup(2)``.       |
    +---+-----------------------------------------------------+
    | 2 | Создание канала (pipe).                            |
    +---+-----------------------------------------------------+
    | 3 | Подмена fd 2 на записывающий конец канала через     |
    |   | ``os.dup2(write_fd, 2)``.                          |
    +---+-----------------------------------------------------+
    | 4 | Вызов ``func(*args, **kwargs)``.                    |
    +---+-----------------------------------------------------+
    | 5 | Восстановление fd 2 из сохранённого.                |
    +---+-----------------------------------------------------+
    | 6 | Чтение перехваченного вывода из канала.             |
    +---+-----------------------------------------------------+
    | 7 | Возврат результата и строки вывода.                 |
    +---+-----------------------------------------------------+
    """
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
            raise ValueError(f"Объект {type(self._doc).__name__} не является документом PyMuPDF.")

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
        except Exception:  # noqa: BLE001
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
        с координатами bbox, оригинальной и нормализованной формами.

        Инвариант синхронности списков:
        Списки ``entries`` и ``block_line_pairs`` заполняются
        параллельно в одном цикле: на каждой итерации, добавляющей
        запись в ``entries``, добавляется соответствующая пара
        ``(block_no, line_no)`` в ``block_line_pairs``. Инвариант
        ``entries[i] ↔ block_line_pairs[i]`` гарантирует корректную
        группировку по ключу ``(block_no, line_no)`` при построении
        ``by_line``.

        Причина такого решения:
        Модель :class:`WordEntry` (см. ``dds_core/domain/models.py``)
        не содержит поля ``block_no`` — оно было исключено при
        упрощении алгоритма поиска фраз (фразы ищутся только внутри
        одной строки). Однако ``by_line`` требует ключ
        ``(block_no, line_no)``, потому что PyMuPDF нумерует строки
        отдельно в каждом блоке, начиная с 0. Параллельный список
        решает эту задачу без расширения модели.

        **Важно для будущих правок:** любое изменение цикла, которое
        добавляет или пропускает элемент в одном из списков без
        синхронного изменения другого, нарушит инвариант. Если
        потребуется фильтрация после ``entries.append()`` — либо
        фильтровать до добавления, либо переносить ``block_no``
        в модель ``WordEntry``.

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
        |    | c. Формирование :class:`WordEntry`.                |
        |    | d. **Синхронное** добавление ``(block_no,          |
        |    |    line_no)`` в ``block_line_pairs``.              |
        +----+----------------------------------------------------+
        | 5  | Заполнение ``by_normalized`` — группировка по      |
        |    | нормализованной форме.                             |
        +----+----------------------------------------------------+
        | 6  | Заполнение ``by_line`` — группировка по            |
        |    | ``(block_no, line_no)`` через ``zip`` двух         |
        |    | синхронных списков.                                 |
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
        #
        # ИНВАРИАНТ: entries и block_line_pairs заполняются синхронно.
        # Индекс i в обоих списках соответствует одному и тому же
        # слову страницы. Это позволяет построить by_line с ключом
        # (block_no, line_no), не добавляя block_no в модель WordEntry.
        entries: list[WordEntry] = []
        block_line_pairs: list[tuple[int, int]] = []
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
                    line_no=int(line_no),
                    word_no=int(word_no),
                )
            )
            block_line_pairs.append((int(block_no), int(line_no)))

        # by_normalized: группировка по нормализованной форме.
        by_normalized: dict[str, list[WordEntry]] = {}
        for entry in entries:
            by_normalized.setdefault(entry.normalized, []).append(entry)
        for lst in by_normalized.values():
            lst.sort(key=lambda e: (e.line_no, e.word_no))

        # by_line: группировка по (block_no, line_no).
        # Использует параллельный список block_line_pairs —
        # индекс i в entries и block_line_pairs совпадают.
        by_line: dict[tuple[int, int], list[WordEntry]] = {}
        for entry, (block_no, line_no) in zip(entries, block_line_pairs):
            key = (block_no, line_no)
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
        except Exception:  # noqa: BLE001, S110
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
