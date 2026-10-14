import pytest

@pytest.mark.parametrize('value', [1, 2])
def test_many(value):
    assert value == 0
