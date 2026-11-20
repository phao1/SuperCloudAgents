"""Hidden acceptance for this standalone fixture."""

import signal
import subprocess
import sys
from datetime import UTC
from hashlib import sha256
from pathlib import Path

checks = 0


def check(actual, expected):
    global checks
    expected_type = type(expected)
    assert isinstance(actual, expected_type) and actual == expected, (actual, expected)
    if expected_type in (bool, int):
        assert type(actual) is expected_type, (actual, expected)
    checks += 1


def raises(error_type, call, *args, **kwargs):
    global checks
    try:
        call(*args, **kwargs)
    except error_type as error:
        checks += 1
        return error
    raise AssertionError(f"{call!r} did not raise {error_type.__name__}")


def verify():
    for name, digest in PROTECTED.items():
        assert sha256(Path(name).read_bytes()).hexdigest() == digest, name
    result = subprocess.run([sys.executable, "-B", "verify.py"], check=False, timeout=12)
    if result.returncode != 0:
        print("Public verification failed.", file=sys.stderr)
        raise SystemExit(1)

PROTECTED = {
    'SPEC.md': '5cd92d4860333c3c41e665e7fe034bd2c50af5da5da44f4f40622165c9e1ac10',
    'verify.py': 'bdb842a40c5cadaef3c6b09742642a4600034e9258e9ee75a7bfee6f03968c6d',
}


def check_feature():
    from datetime import datetime, timedelta, timezone

    from app import Cron, CronError

    def date(year=2024, month=2, day=26, **kwargs):
        return datetime(year, month, day, tzinfo=UTC, **kwargs)

    samples = [
        ("0 0 * * 1-7/3", lambda day: day.weekday() in (0, 3, 6)),
        ("0 0 29 * mon", lambda day: day.day == 29 or day.weekday() == 0),
        ("0 0 * FEB *", lambda day: day.month == 2),
        ("0 0 */1 * MON", lambda day: True),
        ("0 0 29 FEB */1", lambda day: day.month == 2),
        ("0 0 29 FEB *", lambda day: day.month == 2 and day.day == 29),
        ("0 0 * * 0,7,SuN", lambda day: day.weekday() == 6),
    ]
    for expression, includes in samples:
        cron = Cron(expression)
        for year in (2023, 2024, 2000, 2099):
            start = date(year)
            days = [start + timedelta(days=offset) for offset in range(8)]
            check(cron.between(start, start + timedelta(days=8)), [day for day in days if includes(day)])
            for day in days[::3]:
                check(cron.matches(day), includes(day))

    for month, alias in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".lower().split(), 1):
        day = date(month=month, day=1)
        cron = Cron(f"59 23 * {alias} *")
        check(cron.matches(day.replace(hour=23, minute=59)), True)
    for weekday, alias in enumerate("MON TUE WED THU FRI SAT SUN".lower().split()):
        monday = date(2024, 2, 26)
        check(Cron(f"0 0 * * {alias}").between(monday, monday + timedelta(days=7)),
              [monday + timedelta(days=weekday)])

    start = date()
    for low, high, step in [(0, 59, 9), (3, 59, 7), (5, 23, 6), (0, 59, 60), (59, 59, 1)]:
        raw = f"{low:02d}-{high}/{step}" if high != 59 else f"{low}/{step}"
        cron = Cron(f" \t{raw},01\t0 * fEb-MaR *  ")
        chosen = set(range(low, high + 1, step)) | {1}
        for seconds in (0, 1, 60, 61, 3599):
            lower = start + timedelta(seconds=seconds)
            upper = start + timedelta(hours=1)
            expected = [start.replace(minute=i) for i in sorted(chosen) if i * 60 >= seconds]
            check(cron.between(lower, upper), expected)
    check(Cron("* * * * *").between(start, start), [])
    check(Cron("* * * * *").between(start.replace(microsecond=1), start + timedelta(minutes=1)), [])
    check(Cron("* * * * *").matches(start.replace(microsecond=1)), False)
    for expression, field in [
        ("* * * *", -1), ("* * * * * *", -1), ("", -1), (None, -1),
        ("*,/2 * * * *", 0), ("٠ * * * *", 0), ("*/61 * * * *", 0),
        ("0 *,-1 * * *", 1), ("0 24 * * *", 1), ("0 3/24 * * *\n", 4), ("0 0 0 * *", 2),
        ("0 0 1-0 * *", 2), ("0 0 * APR-JAN *", 3), ("0 0 * JAN/13 *", 3),
        ("0 0 * JAN–MAR *", 3), ("0 0 * * MON/0", 4), ("0 0 * * SAT-", 4),
        ("0 0 * * 6/9", 4), ("0 0 * * 8", 4),
    ]:
        error = raises(CronError, Cron, expression)
        assert error.field == field
    cron = Cron("* * * * *")
    for bad in (date(1969), start.replace(tzinfo=None), start.astimezone(timezone(timedelta(hours=2)))):
        assert str(raises(ValueError, cron.matches, bad)) == "utc datetime required"
        assert str(raises(ValueError, cron.between, start, bad)) == "utc datetime required"
    assert str(raises(ValueError, cron.between, start.replace(tzinfo=None), start)) == "utc datetime required"
    for bad in (start - timedelta(seconds=1), start + timedelta(days=32, microseconds=1)):
        assert str(raises(ValueError, cron.between, start, bad)) == "invalid window"
    check(Cron("59 23 31 DEC *").between(date(2099, 12, 1), date(2099, 12, 31, hour=23, minute=59)), [])


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
