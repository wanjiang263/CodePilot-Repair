from pkg.core import other

def test_other():
    assert other(3) == 6

def test_controlled_flaky():
    import os
    assert os.environ.get("PHASE9_FLAKY", "pass") == "pass"
