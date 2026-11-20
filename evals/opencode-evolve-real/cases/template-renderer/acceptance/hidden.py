"""Hidden acceptance for this standalone fixture."""

import signal
import subprocess
import sys
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
    'SPEC.md': '84a4c815e3a93ec3206dad30885bc9d416057110256d1d4ed295115b8baf1d00',
    'verify.py': 'c610fd7859af47f66b8afdc9f921fe874284016f42ccb60184d0754dc1f2174f',
}


def check_feature():
    from copy import deepcopy

    from app import TemplateError, render

    variants = ["", "⛄", "\n", "\r\n", "\t", "α\nö", " {{", "x}}"]
    for index, text in enumerate(variants):
        data = {"label": text, "outer": [{"items": [None, 0, True, -index - 1]}, {"items": []}]}
        before = deepcopy(data)
        template = (
            "{{! ignored }}{{#each outer as label}}{{#each this.items as label}}"
            "<{{root.label}}/{{label}}/{{#if this}}y{{:else}}n{{/if}}>"
            "{{:else}}[{{label.missing}}]{{/each}}{{/each}}\r\n{{label}}"
        )
        expected = f"<{text}//n><{text}/0/n><{text}/true/y><{text}/{-index - 1}/y>[]\r\n{text}"
        check(render(template, data), expected)
        check(data, before)
        check(render("{{{{name}}{{{{" + "{{{{}}", data), "{{name}}{{{{}}")

    errors = [
        ("{{", "unclosed-tag"), ("{{}}", "syntax"), ("{{ x.0 }}", "syntax"),
        ("{{ñ}}", "syntax"), ("{{x\nx}}", "syntax"), ("{{!\r}}", "syntax"),
        ("{{#each x as root}}", "syntax"), ("{{#each x as this}}", "syntax"),
        ("{{#if x y}}", "syntax"), ("{{/if x}}", "syntax"),
        ("{{:else}}", "unexpected-block"), ("{{/each}}", "unexpected-block"),
        ("{{#if x}}", "open-block"), ("{{x}}", "not-a-scalar"),
        ("{{#each item as i}}{{/each}}", "not-a-list"),
    ]
    for prefix, line, column in [("", 1, 1), ("🧵\t", 1, 3), ("\r\n界", 2, 2),
                                 ("a\n\n ", 3, 2), ("xyz", 1, 4)]:
        for source, code in errors:
            error = raises(TemplateError, render, prefix + source, {"x": {}, "item": False})
            assert (error.code, error.line, error.column) == (code, line, column)
    for value in (None, False, 0, "", [], {}, 1, [0], {"a": None}, "0"):
        check(render("{{#if \tx }}T{{:else}}F{{/if}}", {"x": value}), "T" if value else "F")
    check(render("{{#each missing as x}}{{a}}{{:else}}{{this.name}}{{/each}}", {"name": "É"}), "É")
    check(render("{{#if ok}}{{:else}}{{obj}}{{/if}}", {"ok": 1, "obj": {}}), "")
    check(render("{{root.root}}{{root.this}}{{missing.path}}", {"root": 3, "this": 4}), "34")
    check(render(
        "{{#each rows as x}}"
        "{{#each x.items as x}}<{{this}}>{{/each}}"
        "{{x.name}}/{{this.name}}"
        "{{/each}}|{{this.x}}",
        {"x": "root", "rows": [{"name": "outer", "items": [5, "Y"]}]},
    ), "<5><Y>outer/outer|root")
    check(render("{{value}}", {"value": "{{unknown}}"}), "{{unknown}}")
    error = raises(TemplateError, render, "{{x}}{{#each y as z}}{{/each}}", {"x": [], "y": 1})
    check((error.code, error.line, error.column), ("not-a-scalar", 1, 1))
    error = raises(TemplateError, render, "{{obj}}{{#if x}}{{#each y as q}}", {"obj": {}})
    check((error.code, error.line, error.column), ("open-block", 1, 17))
    for source, col in [("{{#if x}}{{:else}}{{:else}}{{/if}}", 19),
                        ("{{#each x as y}}{{/if}}", 17)]:
        error = raises(TemplateError, render, source, {})
        check((error.code, error.column), ("unexpected-block", col))


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
