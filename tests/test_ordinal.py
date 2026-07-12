from no_human.core.ordinal import ordinal


def test_suffix_1_2_3():
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(4) == "4th"


def test_teens_exception():
    assert ordinal(11) == "11th"
    assert ordinal(12) == "12th"
    assert ordinal(13) == "13th"


def test_21_to_23():
    assert ordinal(21) == "21st"
    assert ordinal(22) == "22nd"
    assert ordinal(23) == "23rd"


def test_large_numbers():
    assert ordinal(100) == "100th"
    assert ordinal(111) == "111th"
    assert ordinal(112) == "112th"
    assert ordinal(113) == "113th"
    assert ordinal(121) == "121st"


def test_zero():
    assert ordinal(0) == "0th"


def test_negative_numbers():
    assert ordinal(-1) == "-1st"
    assert ordinal(-11) == "-11th"
    assert ordinal(-21) == "-21st"
