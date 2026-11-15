import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sca.kinds.analysis.spec import AnalysisSpec
from sca.kinds.analysis.verify import SCRIPT


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        text=True,
    ).strip()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "mismatch",
        "missing-image",
        "data",
        "missing-results",
        "does-not-generate",
        "script-fails",
        "reference-image",
    ],
)
def test_clean_checkout_verification(tmp_path, failure):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "data").mkdir()
    (root / "data/a.csv").write_text("value\n1\n3\n")
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    base = git(root, "rev-parse", "HEAD")
    script = "from pathlib import Path\np=Path('output'); p.mkdir(exist_ok=True)\n(p/'report.md').write_text('Mean: 2\\n')\n(p/'results.json').write_text('{\"mean\":2}')\n"
    (root / "analysis.py").write_text(script)
    subprocess.run([sys.executable, "analysis.py"], cwd=root, check=True)
    if failure == "mismatch":
        (root / "output/results.json").write_text('{"mean":99}')
    elif failure == "missing-image":
        (root / "output/report.md").write_text("![plot](figures/missing.png)")
    elif failure == "reference-image":
        (root / "output/report.md").write_text("![plot][figure]\n\n[figure]: figures/missing.png")
    elif failure == "data":
        (root / "data/a.csv").write_text("tampered")
    elif failure == "missing-results":
        (root / "output/results.json").unlink()
    elif failure == "does-not-generate":
        (root / "analysis.py").write_text("pass\n")
    elif failure == "script-fails":
        (root / "analysis.py").write_text('raise RuntimeError("broken")\n')
    git(root, "add", ".")
    git(root, "commit", "-m", "analysis")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            SCRIPT.replace("pathlib.Path('/workspace')", f"pathlib.Path({str(root)!r})"),
            base,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (result.returncode == 0) is (failure is None), result.stdout + result.stderr
    if failure == "mismatch":
        assert "committed/results.json" in result.stdout
        assert "reproduced/results.json" in result.stdout
        assert '-{"mean":99}' in result.stdout


def test_spec_defaults_and_validation():
    spec = AnalysisSpec(question="Mean?", inputs=[uuid4()])
    assert spec.image == "sca-sandbox-analysis:latest"
    assert isinstance(spec.model_dump()["inputs"][0], str)
    for invalid in [{"inputs": []}, {"inputs": ["../../secret"]}, {"max_cost_usd": float("nan")}]:
        with pytest.raises(ValidationError):
            AnalysisSpec.model_validate({"question": "Mean?", "inputs": [str(uuid4())], **invalid})
