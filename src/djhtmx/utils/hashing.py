import mmh3
from uuid6 import uuid7


def generate_id():
    return f"hx-{uuid7().hex}"


def compact_hash(value: str) -> str:
    """Return a SHA1 using a base with 64+ symbols"""
    # this returns a signed 32 bit number, we convert it to unsigned with `& 0xffffffff`
    hashed_value = mmh3.hash(value) & 0xFFFFFFFF

    # Convert the integer to the custom base
    base_len = len(_BASE)
    encoded = []
    while hashed_value > 0:
        hashed_value, rem = divmod(hashed_value, base_len)
        encoded.append(_BASE[rem])

    return "".join(encoded)


# The order of the base is random so that it doesn't match anything out there.
# The symbols are chosen to avoid extra encoding in the URL and HTML, and
# allowed in plain CSS selectors.
_BASE = "ZmBeUHhTgusXNW_Y1b05KPiFcQJD86joqnIRE7Lfkrdp3AOMCvltSwzVG9yxa42"
