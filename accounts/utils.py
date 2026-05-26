import uuid


def generate_username() -> str:
    return uuid.uuid4().hex[:20]
