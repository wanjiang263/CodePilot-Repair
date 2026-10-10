"""Authentication fixture; read as source, never executed."""


def getUserName(user_id):
    """Resolve the user name."""
    if user_id is None:
        raise ValueError("invalid token [expired]")
    return "name"


def get_user_alias(user_id):
    """Return a user alias."""
    return str(user_id)


def count_sessions(sessions):
    return len(sessions)
