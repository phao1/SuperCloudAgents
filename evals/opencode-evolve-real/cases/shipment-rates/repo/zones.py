from dataclasses import dataclass


@dataclass(frozen=True)
class Zone:
    name: str
    base: int
    block: int
    fragile: int


METRO = Zone("metro", 100, 25, 75)
LOCAL = Zone("local", 120, 30, 75)
REGIONAL = Zone("regional", 175, 40, 100)
REMOTE = Zone("remote", 250, 65, 125)


def find(postcode: str) -> Zone:
    if postcode.startswith("AA"):
        return LOCAL
    if postcode.startswith("B"):
        return REGIONAL
    return REMOTE
