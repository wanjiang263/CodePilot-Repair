from pkg.core import other

def test_other():
    assert other(3) == 6

def test_existing_failure():
    assert other(2) == 99
