from app.plates import clean_reading, correct_ru, display, match_plate, normalize


def test_normalize_cyrillic():
    assert normalize("а 123 вс 77") == "A123BC77"
    assert normalize("Х777ХХ-799") == "X777XX799"


def test_correct_ru_fixes_positional_confusions():
    assert correct_ru("A123BC77") == "A123BC77"
    assert correct_ru("A12JBC77") == "A121BC77"
    assert correct_ru("0123B877") == "O123BB77"    # 0 на месте буквы -> O, 8 -> B
    assert correct_ru("AI23BCT7") == "A123BC77"    # I -> 1, T -> 7 на местах цифр
    assert correct_ru("E001KX199") == "E001KX199"
    assert correct_ru("AB12377") == "AB12377"      # такси


def test_correct_ru_rejects_garbage():
    assert correct_ru("HELLO") is None
    assert correct_ru("ABCDEFGH") is None
    assert correct_ru("F123GZ77") is None          # слишком много правок


def test_clean_reading_formats():
    assert clean_reading("A123BC77", "ru") == "A123BC77"
    assert clean_reading("B1234AB", "ru") is None
    assert clean_reading("B1234AB", "ru_or_any") == "B1234AB"
    assert clean_reading("B1", "any") is None


def test_match_plate():
    allowed = ["A123BC77", "O001OO99"]
    assert match_plate("A123BC77", allowed) == "A123BC77"
    assert match_plate("0001OO99", allowed) == "O001OO99"   # путаница 0/O
    assert match_plate("A123BC78", allowed) is None
    assert match_plate("A123BC78", allowed, max_distance=1) == "A123BC77"


def test_match_plate_ambiguous_fuzzy():
    allowed = ["A123BC77", "A123BC79"]
    assert match_plate("A123BC78", allowed, max_distance=1) is None


def test_display():
    assert display("A123BC77") == "А123ВС 77"
    assert display("A123BC777") == "А123ВС 777"
    assert display("AB12377") == "АВ123 77"
    assert display("XYZ123") == "XYZ123"
