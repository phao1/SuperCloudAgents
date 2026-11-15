"""Keep scenario dependencies out of the kernel and other scenarios."""

import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "sca"


def imports(path: Path) -> list[str]:
    module = "sca." + ".".join(path.relative_to(ROOT).with_suffix("").parts)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    result = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = (
                resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module or ""
            )
            result.append(target)
            result.extend(target + "." + alias.name for alias in node.names)
    return result


def test_layering():
    for path in (ROOT / "kernel").rglob("*.py"):
        assert not any(
            name == "sca.kinds" or name.startswith("sca.kinds.") for name in imports(path)
        ), path
    for scenario in (ROOT / "kinds").iterdir():
        if not scenario.is_dir() or scenario.name.startswith("_"):
            continue
        for path in scenario.rglob("*.py"):
            for name in imports(path):
                if name.startswith("sca.kinds."):
                    assert name.split(".")[2] == scenario.name, (path, name)


def test_workflow_packages_import_nothing_on_init():
    """Temporal's workflow sandbox re-imports a workflow module's packages on every run; an
    import in these __init__ files was reloaded per run (~130 MB each, tests ran out of memory)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src/sca"
    for init in [
        root / "__init__.py",
        root / "kinds/__init__.py",
        root / "kernel/__init__.py",
        *root.glob("kinds/*/__init__.py"),
    ]:
        tree = ast.parse(init.read_text())
        assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))], init
