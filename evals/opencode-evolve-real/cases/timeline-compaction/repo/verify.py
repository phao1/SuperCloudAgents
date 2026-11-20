def probes():
    def run(bounds, segments, expected):
        from app import Segment, compact
        try:
            output = compact((Segment(*args) for args in segments), *bounds)
        except ValueError as error:
            output = str(error)
        assert output == expected

    examples = [
        ((0, 4), [(1, 3, "x")], [(1, 3, "x")]),
        ((0, 4), [(-1, 5, "x")], [(0, 4, "x")]),
        ((0, 8), [(0, 8, "x"), (1, 4, "y", 4)], [(0, 1, "x"), (1, 4, "y"), (4, 8, "x")]),
        ((0, 8), [(0, 8, "x", 2), (1, 4, "y", 1)], [(0, 8, "x")]),
        ((0, 8), [(1, 4, "y", 2), (0, 8, "x", 2)], [(0, 8, "x")]),
        ((0, 8), [(0, 8, "x"), (1, 4, None, 4)], [(0, 1, "x"), (4, 8, "x")]),
        ((0, 5), [(0, 5, "x"), (3, 4, "", 4)], [(0, 3, "x"), (3, 4, ""), (4, 5, "x")]),
        ((0, 3, 3), [(0, 1, "x", 9), (1, 3, "x", 8)], [(0, 3, "x")]),
        ((0, 5, 2), [(0, 5, "x"), (3, 4, "y", 4)], [(0, 3, "x")]),
        ((0, 3), [(0, 1, "x"), (2, 3, "x")], [(0, 1, "x"), (2, 3, "x")]),
        ((0, 0), [(2, 4, "x")], []),
        ((0, 0), [(2, 4, "x"), (2, 1, "bad")], "segment 1"),
        ((0, 3), [(False, 2, "invalid")], "segment 0"),
        ((0, 3), [(0, 2, 3)], "segment 0"),
        ((0, 3), [(0, 2, "value", True)], "segment 0"),
        ((3, 2), [], "window"),
        ((0, 3, 0), [], "window"),
        ((-10**12, 10**12), [(-10**12, 10**12, "wide")], [(-10**12, 10**12, "wide")]),
    ]
    return [lambda args=args: run(*args) for args in examples]

if __name__ == "__main__":
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO
    from unittest import FunctionTestCase, TestResult, TestSuite

    suite = TestSuite(FunctionTestCase(probe) for probe in probes())
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        result = suite.run(TestResult())
    failed = len(result.failures) + len(result.errors)
    print(f"{failed} failures / {result.testsRun} checks")
    raise SystemExit(bool(failed))
