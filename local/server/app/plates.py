"""Нормализация, коррекция и сравнение автомобильных номеров.

Все номера внутри системы хранятся в «латинском» виде: кириллические буквы,
допустимые в российских номерах (АВЕКМНОРСТУХ), заменяются на визуально
идентичные латинские (ABEKMHOPCTYX). OCR-модель тоже выдаёт латиницу, поэтому
сравнение идёт в одном алфавите. Для отображения в интерфейсе номер
переводится обратно в кириллицу.
"""

from __future__ import annotations

import re

# Буквы, разрешённые в российских номерах (латинские двойники).
RU_LETTERS = set("ABEKMHOPCTYX")

CYR_TO_LAT = str.maketrans(
    {
        "А": "A", "В": "B", "Е": "E", "Ё": "E", "К": "K", "М": "M", "Н": "H",
        "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    }
)
LAT_TO_CYR = str.maketrans(
    {
        "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
        "O": "О", "P": "Р", "C": "С", "T": "Т", "Y": "У", "X": "Х",
    }
)

# Типичные ошибки OCR: какой символ мог быть на месте буквы/цифры.
DIGIT_AS_LETTER = {"0": "O", "8": "B", "4": "A", "7": "T"}
LETTER_AS_LETTER = {"D": "O", "Q": "O", "U": "Y", "V": "Y", "W": "M", "N": "H"}
LETTER_AS_DIGIT = {
    "O": "0", "D": "0", "Q": "0", "U": "0", "B": "8", "I": "1", "L": "1",
    "J": "1", "Z": "2", "S": "5", "G": "6", "A": "4", "T": "7",
}

# Шаблоны российских номеров: L — буква, D — цифра, R — регион (2–3 цифры).
# Значение — «штраф» шаблона: редкие форматы требуют более чистого чтения,
# чтобы испорченный частный номер не превращался в мотоциклетный и т.п.
RU_TEMPLATES = {
    "LDDDLL": 0.0,   # А123ВС 77 / 777 — легковые
    "LLDDD": 0.5,    # АВ123 77 — такси / маршрутки
    "LLDDDD": 1.0,   # АВ1234 77 — прицепы
    "DDDDLL": 1.0,   # 1234АВ 77 — мотоциклы
    "LDDDD": 1.0,    # А1234 77 — полиция
}

_RU_PLATE_RE = re.compile(r"^(?:[ABEKMHOPCTYX]\d{3}[ABEKMHOPCTYX]{2}|[ABEKMHOPCTYX]{2}\d{3,4}|\d{4}[ABEKMHOPCTYX]{2}|[ABEKMHOPCTYX]\d{4})\d{2,3}$")


def normalize(text: str) -> str:
    """Приводит строку к каноническому виду: верхний регистр, латиница, без пробелов."""
    text = (text or "").upper().translate(CYR_TO_LAT)
    return re.sub(r"[^A-Z0-9]", "", text)


def is_ru_plate(plate: str) -> bool:
    return bool(_RU_PLATE_RE.match(plate))


def _apply_template(raw: str, template: str) -> tuple[str, int] | None:
    """Пытается подогнать строку под шаблон. Возвращает (номер, число исправлений)."""
    fixes = 0
    out = []
    for ch, kind in zip(raw, template):
        if kind == "L":
            if ch in RU_LETTERS:
                out.append(ch)
                continue
            fixed = DIGIT_AS_LETTER.get(ch) or LETTER_AS_LETTER.get(ch)
        else:
            if ch.isdigit():
                out.append(ch)
                continue
            fixed = LETTER_AS_DIGIT.get(ch)
        if fixed is None:
            return None
        out.append(fixed)
        fixes += 1
    return "".join(out), fixes


def correct_ru(raw: str, max_fixes: int = 2) -> str | None:
    """Исправляет типичные ошибки OCR с учётом позиций букв/цифр в российском номере.

    Возвращает исправленный номер или None, если строка не похожа на российский номер.
    """
    raw = normalize(raw)
    best: tuple[str, float] | None = None
    for base, penalty in RU_TEMPLATES.items():
        for region_len in (2, 3):
            template = base + "D" * region_len
            if len(raw) != len(template):
                continue
            res = _apply_template(raw, template)
            if res is None:
                continue
            score = res[1] + (penalty if res[1] else 0)
            if score <= max_fixes and (best is None or score < best[1]):
                best = (res[0], score)
    return best[0] if best else None


def clean_reading(raw: str, plate_format: str = "ru", min_len: int = 4) -> str | None:
    """Готовит результат OCR к сравнению. None — чтение отбрасывается."""
    text = normalize(raw)
    if len(text) < min_len:
        return None
    if plate_format == "ru":
        return correct_ru(text)
    if plate_format == "ru_or_any":
        return correct_ru(text) or text
    return text


def skeleton(plate: str) -> str:
    """Ключ, нечувствительный к путанице похожих символов (0/O, 8/B, 1/I ...)."""
    return plate.translate(str.maketrans("0DQ8B1IL5S2Z6G", "OOOBBIIISSZZGG"))


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_plate(reading: str, allowed: list[str], max_distance: int = 0) -> str | None:
    """Ищет прочитанный номер в списке разрешённых.

    Совпадение: точное, по «скелету» (путаница похожих символов) или —
    если max_distance > 0 — с не более чем max_distance ошибками.
    Возвращает найденный номер из списка или None.
    """
    if reading in allowed:
        return reading
    sk = skeleton(reading)
    for plate in allowed:
        if skeleton(plate) == sk:
            return plate
    if max_distance > 0:
        best, best_d = None, max_distance + 1
        for plate in allowed:
            d = levenshtein(reading, plate)
            if d < best_d:
                best, best_d = plate, d
            elif d == best_d:
                best = None  # неоднозначно — не открываем
        if best is not None and best_d <= max_distance:
            return best
    return None


def display(plate: str) -> str:
    """Человекочитаемый вид: «А123ВС 77» для российских номеров."""
    if not plate:
        return ""
    for base in RU_TEMPLATES:
        body_len = len(base)
        if len(plate) - body_len not in (2, 3):
            continue
        body, region = plate[:body_len], plate[body_len:]
        ok = region.isdigit() and all(
            (c in RU_LETTERS) if k == "L" else c.isdigit() for c, k in zip(body, base)
        )
        if ok:
            return f"{body.translate(LAT_TO_CYR)} {region}"
    return plate
