def probes():
    def run(amount, rows, expected):
        from app import Line, allocate
        lines = [Line(*values) for values in rows]
        try:
            output = list(allocate(amount, lines).items())
        except ValueError as error:
            output = str(error)
        assert output == expected

    examples = [
        (0, [], []),
        (1, [], []),
        (1, [("b", 1, 3), ("a", 1, 3)], [("b", 1), ("a", 0)]),
        (14, [("a", 1, 3), ("b", 1, 10), ("c", 1, 10)], [("a", 3), ("b", 6), ("c", 5)]),
        (60, [("a", 1, 3), ("b", 1, 10), ("c", 1, 10)], [("a", 3), ("b", 10), ("c", 10)]),
        (9, [("a", 0, 300), ("b", 1, 11)], [("a", 0), ("b", 9)]),
        (15, [("a", 8, 1), ("b", 2, 3), ("c", 1, 100)], [("a", 1), ("b", 3), ("c", 11)]),
        (3, [("a", 1, 0), ("b", 2, 0)], [("a", 0), ("b", 0)]),
        (0, [("a", 1, 0), ("a", 0, 1)], "allocation"),
        (0, [("", 1, 0)], "allocation"),
        (False, [], "allocation"),
        (1, [("x", True, 7)], "allocation"),
        (1, [("x", 1, False)], "allocation"),
        (5, [("x", -1, 8)], "allocation"),
        (5, [("x", 1, -8)], "allocation"),
        (2**55 + 1, [("x", 1, 2**55), ("y", 1, 2**55)], [("x", 2**54 + 1), ("y", 2**54)]),
        (7, [("é", 1, 9), ("é", 1, 9)], [("é", 4), ("é", 3)]),
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
