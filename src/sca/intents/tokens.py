"""Resolve DTCG and Tokens Studio values used in coding intents."""

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sca.intents.base import IntentBundle, IntentSource, source_bytes
from sca.intents.rules import design_token_check
from sca.intents.token_check import COLOR_FUNCTION, HEX, LENGTH, NUMBER, literal_key

_ALIAS = re.compile(r"\{([^{}]+)\}")
_LEGACY_TYPES = {
    "spacing": "dimension",
    "sizing": "dimension",
    "borderRadius": "dimension",
    "borderWidth": "dimension",
    "fontSizes": "dimension",
    "fontFamilies": "fontFamily",
    "fontWeights": "fontWeight",
    "letterSpacing": "dimension",
    "lineHeights": "lineHeight",
    "paragraphSpacing": "dimension",
    "paragraphIndent": "dimension",
}
_TYPOGRAPHY = {
    "fontFamily": "fontFamily",
    "fontSize": "dimension",
    "fontWeight": "fontWeight",
    "letterSpacing": "dimension",
    "lineHeight": "lineHeight",
}
_WEIGHTS = {
    "thin": 100,
    "hairline": 100,
    "extralight": 200,
    "ultralight": 200,
    "light": 300,
    "normal": 400,
    "regular": 400,
    "book": 400,
    "medium": 500,
    "semibold": 600,
    "demibold": 600,
    "bold": 700,
    "extrabold": 800,
    "ultrabold": 800,
    "black": 900,
    "heavy": 900,
    "extrablack": 950,
    "ultrablack": 950,
}
_COLOR_SPACES = {
    "srgb",
    "srgb-linear",
    "display-p3",
    "a98-rgb",
    "prophoto-rgb",
    "rec2020",
    "xyz-d65",
    "xyz-d50",
    "hsl",
    "hwb",
    "lab",
    "lch",
    "oklab",
    "oklch",
}


class TokenParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    css_variables: bool = False
    token_sets: list[str] | None = Field(default=None, min_length=1, max_length=64)


@dataclass(frozen=True)
class Token:
    kind: str | None
    value: Any


def _is_token(node: dict) -> bool:
    return "$value" in node or "value" in node and isinstance(node.get("type"), str)


def _merge_sets(target: dict, layer: dict) -> None:
    for key, value in layer.items():
        old = target.get(key)
        if (
            isinstance(old, dict)
            and isinstance(value, dict)
            and not (_is_token(old) or _is_token(value))
        ):
            _merge_sets(old, value)
        else:
            target[key] = deepcopy(value)


def _flatten(group: dict, path: tuple[str, ...] = (), inherited: str | None = None) -> dict:
    declared = group.get("$type", inherited)
    tokens = {}
    for name, child in group.items():
        if (name.startswith("$") and name != "$root") or not isinstance(child, dict):
            continue
        here = (*path, name)
        value_key = "$value" if "$value" in child else "value"
        if _is_token(child):
            kind = child.get("$type", child.get("type", declared))
            if kind is not None and not isinstance(kind, str):
                raise ValueError("invalid type for design token " + ".".join(here))
            tokens[".".join(here)] = Token(_LEGACY_TYPES.get(kind, kind), child[value_key])
        else:
            tokens.update(_flatten(child, here, declared))
    return tokens


def resolve_tokens(document: dict, token_sets: list[str] | None = None) -> dict[str, Token]:
    metadata = document.get("$metadata")
    order = (
        token_sets
        if token_sets is not None
        else (metadata.get("tokenSetOrder") if isinstance(metadata, dict) else None)
    )
    if order:
        if not isinstance(order, list) or not all(isinstance(name, str) for name in order):
            raise ValueError("invalid Tokens Studio token set order")
        merged: dict = {}
        for name in order:
            if name not in document:
                raise ValueError("Tokens Studio token set was not found")
            layer = document[name]
            if not isinstance(layer, dict):
                raise TypeError(f"invalid Tokens Studio token set: {name}")
            _merge_sets(merged, layer)
        document = merged
    raw = _flatten(document)
    resolved: dict[str, Token] = {}
    active: set[str] = set()

    def value_of(value: Any) -> Any:
        if isinstance(value, str) and (match := _ALIAS.fullmatch(value)):
            return resolve(match[1]).value
        if isinstance(value, dict):
            return {key: value_of(part) for key, part in value.items()}
        if isinstance(value, list):
            return [value_of(part) for part in value]
        return value

    def resolve(name: str) -> Token:
        if name in resolved:
            return resolved[name]
        if name in active:
            raise ValueError(f"cyclic design token alias at {name}")
        if name not in raw:
            raise ValueError(f"unknown design token alias: {name}")
        active.add(name)
        token = raw[name]
        kind = token.kind
        if isinstance(token.value, str) and (match := _ALIAS.fullmatch(token.value)):
            target = resolve(match[1])
            kind = kind or target.kind
        resolved[name] = Token(kind, value_of(token.value))
        active.remove(name)
        return resolved[name]

    return {name: resolve(name) for name in raw}


def _number(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(NUMBER, value.strip()):
        return value.strip()
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return format(Decimal(str(value)), "f")
    raise ValueError("expected a finite number")


def _dimension(value: Any) -> str:
    if isinstance(value, dict) and value.get("unit") in ("px", "rem"):
        return _number(value.get("value")) + value["unit"]
    if isinstance(value, str) and re.fullmatch(LENGTH, value.strip(), re.IGNORECASE):
        return value.strip()
    return _number(value) + "px"


def _color(value: Any) -> str:
    if isinstance(value, str):
        value = value.strip()
        # Legacy exports use CSS literals. Exclude CSS declaration syntax.
        if re.fullmatch(
            rf"(?:{HEX}|[a-zA-Z]+|{COLOR_FUNCTION})", value, re.IGNORECASE
        ) and not re.search(r"[;{}\"'\\]", value):
            return value
        raise ValueError("expected a CSS color literal")
    data = value if isinstance(value, dict) else {}
    space, components = data.get("colorSpace"), data.get("components")
    if (
        not isinstance(space, str)
        or space not in _COLOR_SPACES
        or not isinstance(components, list)
        or len(components) != 3
    ):
        raise ValueError("invalid DTCG color space or components")
    parts = [part if part == "none" else _number(part) for part in components]
    alpha = _number(data.get("alpha", 1))
    suffix = "" if Decimal(alpha) == 1 else " / " + alpha
    if space in {"hsl", "hwb"}:
        parts[1:] = [part + "%" if part != "none" else part for part in parts[1:]]
    if space in {"hsl", "hwb", "lab", "lch", "oklab", "oklch"}:
        return space + "(" + " ".join(parts) + suffix + ")"
    return f"color({space} {' '.join(parts)}{suffix})"


def _family(value: Any) -> str:
    families = value.split(",") if isinstance(value, str) else value
    if (
        not isinstance(families, list)
        or not families
        or not all(isinstance(family, str) and family.strip() for family in families)
    ):
        raise ValueError("expected a font family or fallback list")
    generics = {
        "serif",
        "sans-serif",
        "monospace",
        "cursive",
        "fantasy",
        "system-ui",
        "emoji",
        "math",
        "fangsong",
        "ui-serif",
        "ui-sans-serif",
        "ui-monospace",
        "ui-rounded",
    }

    def css_family(family: str) -> str:
        if family in generics:
            return family
        escaped = re.sub(r'[\x00-\x1f\x7f"\\]', lambda m: f"\\{ord(m[0]):x} ", family)
        return '"' + escaped + '"'

    return ", ".join(css_family(family.strip()) for family in families)


def _weight(value: Any) -> str:
    if isinstance(value, str):
        value = _WEIGHTS.get(re.sub(r"[\s_-]+", "", value.lower()), value)
    weight = _number(value)
    if not 1 <= Decimal(weight) <= 1000:
        raise ValueError("font weight must be between 1 and 1000")
    return weight


def _line_height(value: Any) -> str:
    if isinstance(value, dict) or isinstance(value, str) and re.fullmatch(LENGTH, value.strip()):
        return _dimension(value)
    return _number(value)


_CSS_VALUES = {
    "color": _color,
    "dimension": _dimension,
    "fontFamily": _family,
    "fontWeight": _weight,
    "number": _number,
    "lineHeight": _line_height,
}


def _kebab(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    return re.sub(r"[^\w-]+", "-", value.lower()).strip("-")


def _properties(name: str, token: Token) -> list[tuple[str, str, str]]:
    prop = "--" + _kebab(name)
    if prop == "--":
        raise ValueError("token name cannot be exported as a CSS variable")
    if token.kind in _CSS_VALUES:
        return [(prop, _CSS_VALUES[token.kind](token.value), token.kind)]
    if token.kind != "typography":
        return []
    if not isinstance(token.value, dict) or not _TYPOGRAPHY.keys() <= token.value.keys():
        raise ValueError(
            "typography needs fontFamily, fontSize, fontWeight, letterSpacing and lineHeight"
        )
    return [
        (prop + "-" + _kebab(part), _CSS_VALUES[kind](token.value[part]), kind)
        for part, kind in _TYPOGRAPHY.items()
    ]


class DesignTokensAdapter:
    type = "design_tokens"

    def compile(self, source: IntentSource, workspace: Path) -> IntentBundle:
        params = TokenParams.model_validate(source.params)
        try:
            document = json.loads(source_bytes(source, workspace))
            if isinstance(document, dict):
                tokens = resolve_tokens(document, params.token_sets)
            else:
                tokens = {}
        except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid or deeply nested design token JSON") from exc
        except TypeError as exc:
            raise ValueError(str(exc)) from exc

        variables: dict[str, str] = {}
        literals: dict[str, list[str]] = {}
        notes = [
            "# Design tokens",
            "",
            "Use the project's token bindings or these CSS variables.",
            "",
        ]
        for name, token in tokens.items():
            try:
                properties = _properties(name, token)
            except ValueError as exc:
                raise ValueError(f"invalid {token.kind} design token {name}: {exc}") from None
            for prop, value, kind in properties:
                if prop in variables:
                    raise ValueError(f"duplicate design token CSS variable: {prop}")
                variables[prop] = value
                notes.append(f"- {name}: {prop}: {value}")
                if kind in {"color", "dimension"} or (
                    kind == "lineHeight" and re.fullmatch(LENGTH, value)
                ):
                    literals.setdefault(literal_key(value), []).append(f"var({prop})")
        if not variables:
            raise ValueError("no supported design tokens found")

        bundle = design_token_check(source.name, literals)
        filename = source.name + "-tokens.md"
        bundle.context_files[filename] = ("\n".join(notes) + "\n").encode()
        bundle.prompt_notes.append(f"Use the design tokens listed in .sca/context/{filename}.")
        if params.css_variables:
            css = source.name + "-tokens.css"
            declarations = "".join(f"  {prop}: {value};\n" for prop, value in variables.items())
            bundle.context_files[css] = (":root {\n" + declarations + "}\n").encode()
            bundle.prompt_notes.append(
                f"To add CSS variables to the project, use .sca/context/{css} as a starting point."
            )
        return bundle
