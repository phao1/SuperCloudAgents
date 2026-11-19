from pathlib import Path

from app import retry_delays, total_delay

assert retry_delays(5, base=3, limit=8) == [3, 6, 8, 8, 8]
assert retry_delays(2, base=7, limit=2) == [2, 2]
assert retry_delays(0) == []
assert total_delay(iter([3, 6, 8])) == 17
assert Path("verify.py").read_text() == (
    "from app import retry_delays\n\nassert retry_delays(3) == [1, 2, 4]\n"
)
for args in [(-1, 1, 1), (1, 0, 1), (1, 1, 0)]:
    try:
        retry_delays(*args)
    except ValueError:
        pass
    else:
        raise AssertionError(args)
