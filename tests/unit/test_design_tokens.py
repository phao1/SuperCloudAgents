import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sca.intents.base import IntentSource
from sca.intents.compiler import compile_sources, write_context
from sca.intents.token_check import violations
from sca.intents.tokens import DesignTokensAdapter, resolve_tokens

TOKENS = {
    "palette": {
        "$type": "color",
        "accent": {"$value": {"colorSpace": "srgb", "components": [1, 0, 0]}},
        "primary": {"$value": "{palette.accent}"},
    },
    "space": {"$type": "dimension", "$root": {"$value": {"value": 16, "unit": "px"}}},
    "family": {"$type": "fontFamily", "$value": ["Inter", "sans-serif"]},
    "weight": {"$type": "fontWeight", "$value": "semi-bold"},
    "leading": {"$type": "number", "$value": 1.2},
    "body": {
        "$type": "typography",
        "$value": {
            "fontFamily": "{family}",
            "fontWeight": "{weight}",
            "fontSize": "{space.$root}",
            "lineHeight": "{leading}",
            "letterSpacing": {"value": 0.02, "unit": "rem"},
        },
    },
    "body-copy": {"$value": "{body}"},
}


async def test_uploaded_dtcg_generates_notes_css_and_rule(tmp_path):
    objects = AsyncMock()
    objects.get.return_value = json.dumps(TOKENS).encode()
    source = IntentSource(
        type="design_tokens", name="ui", file_id="abc", params={"css_variables": True}
    )
    bundle = await compile_sources([source], tmp_path, objects, "acme")
    objects.get.assert_awaited_once_with("tenants/acme/files/abc")
    assert bundle.checks[0].needs_base_sha
    assert ".sca/context/ui-tokens.md" in bundle.prompt_notes[0]
    notes = bundle.context_files["ui-tokens.md"].decode()
    assert "palette.primary: --palette-primary: color(srgb 1 0 0)" in notes
    css = bundle.context_files["ui-tokens.css"].decode()
    assert '--body-copy-font-family: "Inter", sans-serif;' in css
    assert "--body-copy-font-size: 16px;" in css
    assert "--body-font-weight: 600;" in css
    assert "--body-line-height: 1.2;" in css
    assert "--body-letter-spacing: 0.02rem;" in css
    literals = json.loads(bundle.context_files["ui-token-literals.json"])
    assert literals["#ff0000"] == ["var(--palette-accent)", "var(--palette-primary)"]
    assert "var(--body-font-size)" in literals["16px"]


def test_tokens_studio_set_order_and_typography(tmp_path):
    values = {
        "global": {
            "color": {
                "accent": {"type": "color", "value": "#000"},
                "base": {"type": "color", "value": "#fff"},
            },
            "spacing": {"small": {"type": "spacing", "value": "8px"}},
            "font": {
                "family": {"type": "fontFamilies", "value": "Inter, sans-serif"},
                "weight": {"type": "fontWeights", "value": "Bold"},
            },
            "heading": {
                "type": "typography",
                "value": {
                    "fontFamily": "{font.family}",
                    "fontWeight": "{font.weight}",
                    "fontSize": "24px",
                    "letterSpacing": "0",
                    "lineHeight": "120%",
                },
            },
        },
        "light": {"color": {"accent": {"type": "color", "value": "{color.base}"}}},
        "$metadata": {"tokenSetOrder": ["global", "light"]},
        "$themes": [],
    }
    assert resolve_tokens(values, ["global"])["color.accent"].value == "#000"
    (tmp_path / "tokens.json").write_text(json.dumps(values))
    result = DesignTokensAdapter().compile(
        IntentSource(
            type="design_tokens", name="ui", path="tokens.json", params={"css_variables": True}
        ),
        tmp_path,
    )
    css = result.context_files["ui-tokens.css"].decode()
    assert "--color-accent: #fff;" in css
    assert "--spacing-small: 8px;" in css
    assert "--heading-line-height: 120%;" in css
    assert "--heading-font-weight: 700;" in css


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"a": {"$value": "{b}"}, "b": {"$value": "{a}"}}, "cyclic"),
        ({"a": {"$type": "typography", "$value": {"fontFamily": "{a}"}}}, "cyclic"),
        ({"a": {"$value": "{missing}"}}, "unknown design token alias"),
    ],
)
def test_invalid_token_aliases(values, message):
    with pytest.raises(ValueError, match=message):
        resolve_tokens(values)


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        ("a.css", "a { color: #f00; padding: 16.00px 1px; }", ["#f00", "16.00px"]),
        ("b.scss", "a { color: rgb(255, 0, 0); margin: 16PX; }", ["rgb(255, 0, 0)", "16PX"]),
        (
            "ui.tsx",
            """<div style={{ backgroundColor: '#F00', fontSize: 16 }} />""",
            ["#F00", "16px"],
        ),
        ("ui.tsx", """<svg width={16} fill="#f00" />""", ["16px", "#f00"]),
        (
            "ui.tsx",
            """<svg fill={ "#f00" } data-width="16px" style={{lineHeight: 16}} />""",
            ["#f00"],
        ),
        (
            "a.css",
            """/* width: 16px; */ .test#f00 { --theme: #f00; width: 8px;
            content: "#f00"; color: var(--primary, #f00); background: url("/16px#f00"); }""",
            [],
        ),
        ("b.scss", "// color: #f00;\na { padding: $space; }", []),
    ],
)
def test_rule_detects_design_literals(path, content, expected):
    literals = {"#ff0000": ["var(--primary)"], "16px": ["var(--space)"]}
    assert [
        item.split("hard-coded ")[1].split(". Use")[0]
        for item in violations(path, content, literals)
    ] == expected


def test_generated_check_reads_only_changed_committed_styles(tmp_path):
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
            text=True,
        ).strip()

    def commit() -> str:
        git("add", ".")
        git("commit", "-qm", "fixture")
        return git("rev-parse", "HEAD")

    git("init", "-q")
    (tmp_path / "existing.css").write_text("a { color: #f00; }")
    base = commit()
    (tmp_path / "新的样式.scss").write_text("a { width: 16px; color: #f00; }")
    (tmp_path / "skip.txt").write_text("a { color: #f00; }")
    commit()
    token_path = Path("input.json")
    (tmp_path / token_path).write_text(json.dumps(TOKENS))
    bundle = DesignTokensAdapter().compile(
        IntentSource(type="design_tokens", name="ui", path=str(token_path)), tmp_path
    )
    write_context(tmp_path, bundle)
    assert shlex.split(bundle.checks[0].command)[-1] == "$1"
    check = [sys.executable, *shlex.split(bundle.checks[0].command)[1:-1], base]
    failed = subprocess.run(check, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert failed.returncode == 1
    assert "新的样式.scss:1: hard-coded 16px" in failed.stdout
    assert "existing.css" not in failed.stdout
    assert "skip.txt" not in failed.stdout
    (tmp_path / "新的样式.scss").write_text("a { width: var(--space-root); }")
    commit()
    subprocess.run(check, cwd=tmp_path, check=True)
