def probes():
    def run(paths, rules, expected):
        from app import Entry, included

        entries = [Entry(path.rstrip("/"), path.endswith("/")) for path in paths]
        assert included(entries, rules) == expected

    examples = [
        (["a", "b"], [""], ["a", "b"]),
        (["a.tmp", "a", "b/a.tmp"], ["*.tmp"], ["a"]),
        (["build/", "build/keep.txt"], ["build/", "!build/keep.txt"], []),
        (["build/keep.txt", "build/no", "build/"], ["build/*", "!build/keep.txt"], ["build/keep.txt", "build"]),
        (["a/build/keep", "build/keep"], ["/build/"], ["a/build/keep"]),
        (["foo/a.tmp", "x/foo/a.tmp", "foo/sub/a.tmp"], ["foo/*.tmp"], ["x/foo/a.tmp", "foo/sub/a.tmp"]),
        (["foo/a.tmp", "foo/x/y.tmp", "foo/x/keep"], ["foo/**/*.tmp"], ["foo/x/keep"]),
        (["gone/child", "empty", "gone/"], ["gone/**", "!gone/"], ["empty", "gone"]),
        (["#idea", "!ok", "ok"], ["# comment", r"\#idea", r"\!ok"], ["ok"]),
        (["a*", "a?", "ab"], [r"a\*", r"a\?"], ["ab"]),
        (["10", "[10]", "1"], ["[10]"], ["10", "1"]),
        (["x ", "x", "a/x "], [r"x\ "], ["x"]),
        (["a/雪", "a/好人"], ["a/?"], ["a/好人"]),
        (["d", "x/d", "d/", "x/d/"], ["d/"], ["d", "x/d"]),
        (["x", "x", "a"], ["**", "!x"], ["x", "x"]),
        (["a", "b", "x/a"], ["/a", "!a"], ["a", "b", "x/a"]),
        (["src/a/.hide", "src/.hide", "src/a"], ["src/**/.*"], ["src/a"]),
        (["a/\nbc", "a/x", "a/\n"], ["a/?"], ["a/\nbc"]),
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
