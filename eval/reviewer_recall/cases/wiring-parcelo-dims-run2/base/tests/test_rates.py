from parcelo.rates import quote


def test_quote_within_one_zone():
    assert quote(2.0, "EC1A 1BB", "WC2N 5DU") == 5.40


def test_quote_cross_zone_standard():
    assert quote(2.0, "EC1A 1BB", "SW1A 2AA") == 6.90


def test_quote_cross_zone_c_standard():
    assert quote(2.0, "EC1A 1BB", "AB10 1AA") == 8.40


def test_quote_express_within_one_zone():
    assert quote(2.0, "EC1A 1BB", "WC2N 5DU", express=True) == 8.10
