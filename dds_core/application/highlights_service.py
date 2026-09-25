"""
Сервис поиска совпадений на странице для подсветки.

Принимает :class:`WordIndex` (построенный инфраструктурным слоем)
и список терминов, возвращает список :class:`PageHighlight` в
нормализованных координатах ``[0..1]``.

Ключевая особенность: поиск ведётся в **нормализованном**
пространстве (``WordIndex.by_normalized`` / ``by_line``), а
координаты возвращаются от **оригинальных** слов. Это решает
проблему, когда сниппет FTS5 денормализован и не совпадает с
оригинальным текстом PDF (например, документ содержит ``EC-423-1``
латиницей, а сниппет после обратной замены показывает ``ЕС-423-1``).

Поиск термина:

- Одиночное слово ищется по ``WordIndex.by_normalized`` — за O(1)
  возвращаются все вхождения слова на странице.
- Фраза ищется по ``WordIndex.by_line`` — окно из последовательных
  слов внутри одной строки. Слова окна должны быть **смежными**
  по ``word_no``: PyMuPDF может пропускать токены, ставшие пустыми
  после ``.strip()``, и разрыв в нумерации означает, что слова в
  исходном PDF не были соседними.

Диагностика и коррекция системы координат:

Метод :meth:`HighlightsService.search_highlights` поддерживает
опциональную коррекцию координат для страниц PDF с аномальной
системой координат (bottom-left с перепутанными осями). При
``apply_transform=None`` выполняется авто-диагностика через
:func:`~dds_core.application.coordinate_diagnostics.diagnose_word_index`;
при обнаружении аномалии координаты bbox трансформируются в
стандартную top-left систему через
:func:`~dds_core.application.coordinate_diagnostics.transform_bbox`.

Пороги диагностики передаются в конструктор сервиса как
read-only параметры (со значениями по умолчанию из
:mod:`dds_core.domain.config`). Это обеспечивает инверсию
зависимостей: сервис не читает глобальную конфигурацию внутри
метода поиска, а получает её извне.

Принципы:
- Не зависит от конкретной PDF-библиотеки: работает только с
  :class:`WordIndex`, построенным инфраструктурным слоем.
- Чистый функционал: детерминированный, без побочных эффектов.
- Потокобезопасен: конфигурация диагностики устанавливается в
  ``__init__`` и не изменяется в дальнейшем; методы не хранят
  изменяемого состояния между вызовами.
- Инверсия зависимостей: использует :func:`normalize_text` из
  domain layer, модели из domain layer, диагностику из
  :mod:`dds_core.application.coordinate_diagnostics`, значения
  по умолчанию — из :mod:`dds_core.domain.config`.

Классы:
    ``HighlightsService`` — поиск совпадений для подсветки с
    опциональной диагностикой и коррекцией системы координат.
"""

from __future__ import annotations

from ..domain import config as core_config
from ..domain.models import PageHighlight, WordEntry, WordIndex
from ..domain.text_normalization import normalize_text
from .coordinate_diagnostics import diagnose_word_index, transform_bbox


class HighlightsService:
    """Сервис поиска совпадений на странице для подсветки.

    Реализует двухуровневый поиск:

    1. **Одиночные слова** — прямой поиск в ``by_normalized``.
       Все вхождения слова на странице возвращаются одним списком.

    2. **Фразы** — поиск окна последовательных слов внутри одной
       строки. Дополнительно проверяется смежность ``word_no``,
       чтобы не склеивать слова через разрыв (пустой токен PyMuPDF).

    Дополнительно поддерживает **диагностику и коррекцию системы
    координат** страницы. При ``apply_transform=None`` в
    :meth:`search_highlights` выполняется авто-диагностика;
    при обнаружении аномалии координаты bbox трансформируются
    перед нормализацией. Пороги диагностики и глобальный
    переключатель передаются в конструктор.

    Пример использования::

        service = HighlightsService()
        highlights, flip, confidence = service.search_highlights(
            index,
            ["EC-423-1", "гидрошпонка", "корпус крупного дробления"],
        )

    Attributes:
        ``_diagnostics_enabled``: Глобальный переключатель
            авто-диагностики. Значение по умолчанию —
            ``core_config.COORD_DIAGNOSTICS_ENABLED``.
        ``_oob_threshold``: Порог доли слов за границами страницы
            (признак OOB). Значение по умолчанию —
            ``core_config.COORD_TEXT_OOB_THRESHOLD``.
        ``_dominance_threshold``: Порог доли «вертикальных» слов
            (признак Dominance). Значение по умолчанию —
            ``core_config.COORD_TEXT_DOMINANCE_THRESHOLD``.
        ``_aspect_t``: Порог отношения сторон для классификации
            слова. Значение по умолчанию — ``core_config.COORD_ASPECT_T``.

    Примечание:
        Все атрибуты — read-only: устанавливаются один раз в
        ``__init__`` и не изменяются. Экземпляр может использоваться
        из нескольких потоков одновременно.
    """

    def __init__(
        self,
        coord_diagnostics_enabled: bool | None = None,
        coord_text_oob_threshold: float | None = None,
        coord_text_dominance_threshold: float | None = None,
        coord_aspect_t: float | None = None,
    ) -> None:
        """Инициализирует сервис.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Определение ``_diagnostics_enabled``: если         |
        |   | ``coord_diagnostics_enabled`` не задан, значение   |
        |   | берётся из ``core_config.COORD_DIAGNOSTICS_ENABLED``.|
        +---+-----------------------------------------------------+
        | 2 | Определение ``_oob_threshold``: если               |
        |   | ``coord_text_oob_threshold`` не задан, значение    |
        |   | берётся из ``core_config.COORD_TEXT_OOB_THRESHOLD``.|
        +---+-----------------------------------------------------+
        | 3 | Определение ``_dominance_threshold``: если         |
        |   | ``coord_text_dominance_threshold`` не задан,       |
        |   | значение берётся из                                 |
        |   | ``core_config.COORD_TEXT_DOMINANCE_THRESHOLD``.     |
        +---+-----------------------------------------------------+
        | 4 | Определение ``_aspect_t``: если ``coord_aspect_t`` |
        |   | не задан, значение берётся из                       |
        |   | ``core_config.COORD_ASPECT_T``.                     |
        +---+-----------------------------------------------------+

        Args:
            coord_diagnostics_enabled: Глобальный переключатель
                авто-диагностики. Если ``None``, используется
                значение из ``config.COORD_DIAGNOSTICS_ENABLED``.
                При ``False`` авто-диагностика не выполняется;
                флаг трансформации определяется исключительно
                параметром ``apply_transform`` метода
                :meth:`search_highlights`.
            coord_text_oob_threshold: Порог доли слов за границами
                страницы (признак OOB). Если ``None``, используется
                ``config.COORD_TEXT_OOB_THRESHOLD``.
            coord_text_dominance_threshold: Порог доли «вертикальных»
                слов (признак Dominance). Если ``None``, используется
                ``config.COORD_TEXT_DOMINANCE_THRESHOLD``.
            coord_aspect_t: Порог отношения сторон для классификации
                слова. Если ``None``, используется
                ``config.COORD_ASPECT_T``.
        """
        self._diagnostics_enabled = (
            coord_diagnostics_enabled
            if coord_diagnostics_enabled is not None
            else core_config.COORD_DIAGNOSTICS_ENABLED
        )
        self._oob_threshold = (
            coord_text_oob_threshold
            if coord_text_oob_threshold is not None
            else core_config.COORD_TEXT_OOB_THRESHOLD
        )
        self._dominance_threshold = (
            coord_text_dominance_threshold
            if coord_text_dominance_threshold is not None
            else core_config.COORD_TEXT_DOMINANCE_THRESHOLD
        )
        self._aspect_t = (
            coord_aspect_t if coord_aspect_t is not None else core_config.COORD_ASPECT_T
        )

    def search_highlights(
        self,
        index: WordIndex,
        terms: list[str],
        apply_transform: bool | None = None,
    ) -> tuple[list[PageHighlight], bool, str]:
        """Ищет все совпадения для списка терминов на странице.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Guard: если ``page_width`` или ``page_height``     |
        |    | не положительны — возврат ``([], False, "none")``  |
        |    | (защита от деления на ноль при повреждённом PDF).  |
        +----+----------------------------------------------------+
        | 2  | Определение флага ``flip`` и уровня ``confidence``:|
        |    | a. Если ``apply_transform is None`` и диагностика  |
        |    |    включена — вызов ``diagnose_word_index``.       |
        |    | b. Если ``apply_transform is None`` и диагностика  |
        |    |    отключена — ``(False, "none")``.                |
        |    | c. Если ``apply_transform`` задан явно —           |
        |    |    ``(apply_transform, "manual")``.                |
        +----+----------------------------------------------------+
        | 3  | Для каждого термина:                               |
        |    | a. ``.strip()`` и проверка непустоты.              |
        |    | b. Нормализация через ``normalize_text``.          |
        |    | c. Разбиение на слова по пробелам.                 |
        |    | d. Поиск в индексе (``_search_term``).             |
        |    | e. Группировка записей по ``k = len(words)``.      |
        |    | f. Объединение bbox каждой группы в один           |
        |    |       охватывающий прямоугольник.                  |
        |    | g. Если ``flip`` истинно — трансформация bbox      |
        |    |       через ``transform_bbox``.                    |
        |    | h. Нормализация координат делением на              |
        |    |       ``page_width`` / ``page_height``.            |
        +----+----------------------------------------------------+
        | 4  | Возврат ``(highlights, flip, confidence)``.        |
        +----+----------------------------------------------------+

        Режимы ``apply_transform``:

        +-------------------+-----------------------------------+
        | Значение          | Поведение                         |
        +===================+===================================+
        | ``None``          | Авто-диагностика. При обнаружении |
        |                   | аномалии трансформация применяется|
        |                   | автоматически. ``confidence`` —   |
        |                   | результат диагностики.            |
        +-------------------+-----------------------------------+
        | ``True``          | Принудительная трансформация.     |
        |                   | ``confidence = "manual"``.        |
        +-------------------+-----------------------------------+
        | ``False``         | Трансформация не применяется.     |
        |                   | ``confidence = "manual"``.        |
        +-------------------+-----------------------------------+

        Примечание:
            Порядок highlights в ответе не определён. Клиент
            не должен полагаться на конкретный порядок; при
            необходимости сортировать по ``y`` / ``x``.

        Args:
            index: :class:`WordIndex` — нормализованный индекс
                слов страницы.
            terms: Список терминов. Каждый термин может быть
                одним словом или фразой. Регистр и раскладка
                не важны — нормализация выполняется внутри.
            apply_transform: Управление трансформацией координат.
                ``None`` — авто-диагностика; ``True`` — применить
                принудительно; ``False`` — не применять.

        Returns:
            Кортеж ``(highlights, flip, confidence)``:

            - ``highlights`` — список :class:`PageHighlight`
              в нормализованных координатах ``[0..1]``. Пустой
              список, если ни один термин не найден или страница
              имеет нулевые размеры.
            - ``flip`` — ``True``, если трансформация была
              применена к координатам.
            - ``confidence`` — уровень уверенности диагностики:

              - ``"high"`` — сработали оба признака аномалии.
              - ``"medium"`` — сработал ровно один признак.
              - ``"none"`` — аномалия не обнаружена, либо
                диагностика не выполнена (пустая страница,
                ``rotation != 0``, диагностика отключена).
              - ``"manual"`` — флаг задан явно через
                ``apply_transform``.
        """
        # Guard: защита от деления на ноль при повреждённом PDF.
        if index.page_width <= 0 or index.page_height <= 0:
            return [], False, "none"

        # Определение флага трансформации и уровня уверенности.
        if apply_transform is None:
            if self._diagnostics_enabled:
                flip, confidence = diagnose_word_index(
                    index,
                    text_oob_threshold=self._oob_threshold,
                    text_dominance_threshold=self._dominance_threshold,
                    aspect_t=self._aspect_t,
                )
            else:
                flip, confidence = False, "none"
        else:
            flip = apply_transform
            confidence = "manual"

        highlights: list[PageHighlight] = []

        for term in terms:
            term_stripped = term.strip() if term else ""
            if not term_stripped:
                continue

            # Нормализация: lower + замена кириллицы на латиницу.
            # Результат совпадает с форматом, в котором хранятся
            # ключи by_normalized и by_line.
            normalized_term = normalize_text(term_stripped)
            words = normalized_term.split()
            if not words:
                continue

            entries = self._search_term(words, index)
            if not entries:
                continue

            # Группировка: каждая группа — одно вхождение термина.
            # Длина groups гарантированно кратна len(words), так как
            # _search_term возвращает только полные окна.
            group_size = len(words)
            for i in range(0, len(entries), group_size):
                group = entries[i : i + group_size]
                if len(group) != group_size:
                    # Защита от неожиданного нарушения инварианта.
                    break

                x0 = min(e.x0 for e in group)
                y0 = min(e.y0 for e in group)
                x1 = max(e.x1 for e in group)
                y1 = max(e.y1 for e in group)

                # Трансформация координат при необходимости.
                if flip:
                    x0, y0, x1, y1 = transform_bbox(
                        (x0, y0, x1, y1),
                        index.page_height,
                    )

                highlights.append(
                    PageHighlight(
                        x=x0 / index.page_width,
                        y=y0 / index.page_height,
                        w=(x1 - x0) / index.page_width,
                        h=(y1 - y0) / index.page_height,
                        term=term_stripped,
                    )
                )

        return highlights, flip, confidence

    def _search_term(
        self,
        words: list[str],
        index: WordIndex,
    ) -> list[WordEntry]:
        """Ищет все вхождения термина в индексе страницы.

        Стратегия зависит от длины термина:

        - **Одиночное слово** — прямое обращение к словарю
          ``by_normalized``. Возвращаются все вхождения слова
          на странице.
        - **Фраза** — скользящее окно по каждой строке. Окно
          должно совпадать с термином покомпонентно и состоять
          из слов с непрерывными ``word_no``.

        Операции:

        +----+----------------------------------------------------+
        | №  | Описание                                           |
        +====+====================================================+
        | 1  | Если ``words`` пуст → возврат пустого списка.      |
        +----+----------------------------------------------------+
        | 2  | Если ``len(words) == 1`` → возврат копии списка    |
        |    | из ``by_normalized.get(words[0], [])``.            |
        +----+----------------------------------------------------+
        | 3  | Иначе: для каждой строки ``by_line``:              |
        |    | a. Пропуск строк короче размера окна.              |
        |    | b. Для каждого ``start`` позиции окна:             |
        |    |    - проверка нормализованных форм всех слов;      |
        |    |    - проверка смежности ``word_no``;               |
        |    |    - при успехе — добавление окна в ``matches``.   |
        +----+----------------------------------------------------+
        | 4  | Разворачивание ``matches`` в плоский список        |
        |    | ``WordEntry`` с сохранением порядка внутри окон.   |
        +----+----------------------------------------------------+

        Примечание:
            Возвращаемый список содержит **только полные окна**
            по ``len(words)`` элементов. Это гарантирует, что
            вызывающий код может разбить его на группы шагом
            ``len(words)`` без проверок.

        Args:
            words: Нормализованные слова термина, полученные
                через ``normalize_text(term).split()``.
            index: Индекс слов страницы.

        Returns:
            Плоский список ``WordEntry``. Пустой список, если
            совпадений нет.
        """
        if not words:
            return []

        # Одиночное слово — прямое обращение к словарю.
        if len(words) == 1:
            return list(index.by_normalized.get(words[0], []))

        window_size = len(words)
        matches: list[list[WordEntry]] = []

        for line_words in index.by_line.values():
            n = len(line_words)
            if n < window_size:
                continue

            for start in range(n - window_size + 1):
                window = line_words[start : start + window_size]

                # Проверка нормализованной формы: каждое слово
                # окна должно совпасть с соответствующим словом
                # термина.
                if not all(window[k].normalized == words[k] for k in range(window_size)):
                    continue

                # Проверка непрерывности word_no: слова должны
                # быть смежными в исходной строке PDF. Разрыв
                # означает, что между ними был пропущенный токен
                # (например, табуляция или пустая ячейка таблицы),
                # и фраза не является реальной.
                if not self._is_adjacent_word_numbers(window):
                    continue

                matches.append(window)

        # Разворачиваем в плоский список.
        flat: list[WordEntry] = []
        for group in matches:
            flat.extend(group)
        return flat

    def _is_adjacent_word_numbers(
        self,
        window: list[WordEntry],
    ) -> bool:
        """Проверяет непрерывность ``word_no`` внутри окна.

        PyMuPDF возвращает слова с полем ``word_no`` — позицией
        внутри строки. При ``.strip()`` пустые токены (например,
        отдельные пробелы или табуляции) отбрасываются, но
        нумерация оставшихся слов сохраняет исходные индексы.
        Если в строке был пропущенный токен между двумя словами
        фразы, их ``word_no`` различаются более чем на 1.

        Эта проверка нужна для устранения ложных подсветок:
        без неё слова ``"A"`` (word_no=0) и ``"B"`` (word_no=2)
        могли бы быть приняты за фразу ``"A B"``, хотя в
        исходном PDF между ними был разрыв.

        Операции:

        +---+-----------------------------------------------------+
        | № | Описание                                            |
        +===+=====================================================+
        | 1 | Если ``len(window) <= 1`` → возврат ``True``        |
        |   | (проверять нечего).                                 |
        +---+-----------------------------------------------------+
        | 2 | Для каждой пары соседних элементов окна:            |
        |   | проверка ``window[k+1].word_no == window[k].word_no |
        |   | + 1``.                                              |
        +---+-----------------------------------------------------+
        | 3 | Возврат ``True`` при отсутствии разрывов.           |
        +---+-----------------------------------------------------+

        Args:
            window: Последовательность ``WordEntry`` из одной
                строки, отсортированная по ``word_no``.

        Returns:
            ``True``, если все элементы окна имеют непрерывные
            ``word_no`` (шаг ровно 1). ``False`` при наличии
            разрыва.
        """
        return all(window[k + 1].word_no == window[k].word_no + 1 for k in range(len(window) - 1))
