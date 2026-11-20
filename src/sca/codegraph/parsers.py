"""Language adapters for the repository index."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ModuleImport:
    module: str
    names: tuple[str, ...] = ()


@dataclass
class ParsedFile:
    imports: list[ModuleImport] = field(default_factory=list)
    defined: set[str] = field(default_factory=set)
    referenced: set[str] = field(default_factory=set)
    signatures: list[str] = field(default_factory=list)


class SourceParser(Protocol):
    def parse(self, source: bytes) -> ParsedFile: ...


def _dotted_name(node: ast.expr) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return ""


def _target_names(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(item) for item in node.elts))
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return set()


class _SignatureLiterals(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        # Literal values can include credentials or arbitrary prompt text.
        if node.value is None or node.value is Ellipsis:
            return node
        return ast.Constant(value=Ellipsis)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    node.args.defaults = [ast.Constant(value=Ellipsis) for _ in node.args.defaults]
    node.args.kw_defaults = [
        ast.Constant(value=Ellipsis) if default is not None else None
        for default in node.args.kw_defaults
    ]
    literals = _SignatureLiterals()
    arguments = ast.unparse(literals.visit(node.args))
    returns = " -> " + ast.unparse(literals.visit(node.returns)) if node.returns else ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    parameters = ", ".join(ast.unparse(literals.visit(param)) for param in node.type_params)
    name = f"{node.name}[{parameters}]" if parameters else node.name
    return f"{prefix} {name}({arguments}){returns}"


class PythonParser:
    def parse(self, source: bytes) -> ParsedFile:
        tree = ast.parse(source)
        parsed = ParsedFile()
        functions = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parsed.defined.add(node.name)
                if not isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                    functions.append(node)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    parsed.defined.update(_target_names(target))
            elif isinstance(node, (ast.AnnAssign, ast.TypeAlias)):
                parsed.defined.update(_target_names(
                    node.target if isinstance(node, ast.AnnAssign) else node.name
                ))

        bindings: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parsed.imports.append(ModuleImport(alias.name))
                    root = alias.name.split(".", 1)[0]
                    bindings.setdefault(alias.asname or root, set()).add(
                        alias.name if alias.asname else root
                    )
            elif isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                names = tuple(alias.name for alias in node.names)
                parsed.imports.append(ModuleImport(module, names))
                for alias in node.names:
                    if alias.name != "*":
                        target = module + ("" if module.endswith(".") else ".") + alias.name
                        parsed.referenced.add(target)
                        bindings.setdefault(alias.asname or alias.name, set()).add(target)

        for node in ast.walk(tree):
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                name = _dotted_name(node)
                root, _, attrs = name.partition(".")
                if root in parsed.defined:
                    parsed.referenced.add(root)
                for target in bindings.get(root, ()):
                    parsed.referenced.add(f"{target}.{attrs}" if attrs else target)
        parsed.signatures = [_signature(node) for node in functions]
        return parsed


JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
_JS_LITERAL_OR_COMMENT = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\\n])*'"
    r'|"(?:\\.|[^"\\\n])*"'
)
_JS_STATEMENT = r"(?:^|(?<=;))[^\S\n]*"
_JS_BINDINGS = r"[\w\s{},*$]{1,512}?"
_JS_MODULE = r"""(?P<quote>['"])(?P<module>[^'"\r\n]+)(?P=quote)"""
_JS_IMPORT = re.compile(
    _JS_STATEMENT + rf"import\s{{0,32}}(?:(?P<bindings>{_JS_BINDINGS})\s{{1,32}}from\s*)?"
    + _JS_MODULE,
    re.MULTILINE,
)
_JS_EXPORT = re.compile(
    _JS_STATEMENT + r"export\s+(?:type\s+)?"
    + r"(?P<bindings>\{[\w\s,$]*\}|\*(?:\s+as\s+[\w$]+)?)"
    + rf"(?:\s+from\s*{_JS_MODULE})?",
    re.MULTILINE,
)
_JS_DECLARATION = re.compile(
    _JS_STATEMENT + r"export\s+"
    + r"(?:(?P<default>default)\b|(?:declare\s+)?(?:async\s+)?"
    + r"(?:function|class|const|let|var|type|interface|enum)\s+(?P<name>[\w$]+))",
    re.MULTILINE,
)
_JS_REQUIRE = re.compile(
    _JS_STATEMENT + rf"(?:const|let|var)\s+{_JS_BINDINGS}\s*=\s*require\(\s*{_JS_MODULE}\s*\)",
    re.MULTILINE,
)
_JS_COMMON_EXPORT = re.compile(
    _JS_STATEMENT + r"(?:(?:module\.)?exports\.(?P<name>[\w$]+)|module\.exports)\s*=",
    re.MULTILINE,
)


def _without_comments(match: re.Match[str]) -> str:
    value = match.group()
    if value.startswith(("/", "`")):
        return re.sub(r"[^\n]", " ", value)
    return value


def _js_names(bindings: str, *, exported: bool = False) -> tuple[str, ...]:
    body = bindings.strip().removeprefix("type ").strip()
    names = []
    if "{" in body:
        prefix, group = body.split("{", 1)
        if prefix.strip(" ,"):
            names.append("default")
        for item in group.rstrip("}").split(","):
            tokens = item.strip().removeprefix("type ").split()
            if tokens:
                names.append(tokens[-1] if exported else tokens[0])
    elif body.startswith("*"):
        if not exported:
            names.append("*")
        elif " as " in body:
            names.append(body.rsplit(None, 1)[-1])
    elif body:
        names.append("default")
    return tuple(names)


class JavaScriptParser:
    def parse(self, source: bytes) -> ParsedFile:
        text = _JS_LITERAL_OR_COMMENT.sub(_without_comments, source.decode("utf-8-sig"))
        parsed = ParsedFile()
        for match in _JS_IMPORT.finditer(text):
            parsed.imports.append(ModuleImport(
                match["module"], _js_names(match["bindings"] or "")
            ))
        for match in _JS_REQUIRE.finditer(text):
            parsed.imports.append(ModuleImport(match["module"]))
        for match in _JS_EXPORT.finditer(text):
            bindings = match["bindings"]
            parsed.defined.update(_js_names(bindings, exported=True))
            if match["module"]:
                parsed.imports.append(ModuleImport(match["module"], _js_names(bindings)))
        for match in _JS_DECLARATION.finditer(text):
            parsed.defined.add(match["name"] or "default")
        for match in _JS_COMMON_EXPORT.finditer(text):
            parsed.defined.add(match["name"] or "default")
        return parsed


_JAVASCRIPT = JavaScriptParser()
DEFAULT_PARSERS: dict[str, SourceParser] = {
    ".py": PythonParser(),
    **dict.fromkeys(JS_SUFFIXES, _JAVASCRIPT),
}
