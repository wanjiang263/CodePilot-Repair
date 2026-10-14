def test_chain():
    try:
        raise KeyError('origin')
    except KeyError as exc:
        raise ValueError('outer') from exc
