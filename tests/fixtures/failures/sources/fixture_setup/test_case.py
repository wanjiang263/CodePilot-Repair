import pytest

@pytest.fixture
def resource():
    raise ValueError('controlled setup failure')

def test_resource(resource):
    pass
