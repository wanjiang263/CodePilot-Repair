import pytest

@pytest.fixture
def resource():
    yield 1
    raise ValueError('controlled teardown failure')

def test_resource(resource):
    assert resource == 1
