"""Read Figma frames and local variables via the official REST API."""

import json
import re
import struct
from collections.abc import Iterator
from itertools import islice
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sca.intents.base import IntentBundle, IntentSource
from sca.intents.remote import RemoteClient, RemoteError, SecretName
from sca.intents.token_check import literal_key
from sca.intents.visual import VisualAdapter, VisualParams

_FRAME_LIMIT = 8
_FRAME_TYPES = {"FRAME", "COMPONENT", "COMPONENT_SET"}
_NODE_ID = re.compile(r"I?\d+[:-]\d+(?:;\d+:\d+)*")
_DIMENSION_SCOPES = {
    "GAP",
    "WIDTH_HEIGHT",
    "CORNER_RADIUS",
    "STROKE_FLOAT",
    "FONT_SIZE",
    "LETTER_SPACING",
    "LINE_HEIGHT",
    "PARAGRAPH_SPACING",
    "PARAGRAPH_INDENT",
}


class FrameVisualParams(VisualParams):
    width: int | None = Field(default=None, ge=1, le=4096)
    height: int | None = Field(default=None, ge=1, le=4096)


class FigmaParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    node_ids: list[Annotated[str, Field(max_length=128)]] | None = Field(
        default=None, min_length=1, max_length=_FRAME_LIMIT
    )
    token_secret: SecretName
    visual: dict[str, FrameVisualParams] = Field(default_factory=dict, max_length=_FRAME_LIMIT)
    variable_modes: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("node_ids")
    @classmethod
    def _normalize_nodes(cls, ids: list[str] | None) -> list[str] | None:
        if ids is None:
            return None
        if any(not _NODE_ID.fullmatch(node_id) for node_id in ids):
            raise ValueError("invalid Figma node id")
        normalized = [node_id.replace("-", ":") for node_id in ids]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Figma node ids must be unique")
        return normalized

    @field_validator("visual")
    @classmethod
    def _normalize_visual(cls, value: dict[str, FrameVisualParams]) -> dict[str, FrameVisualParams]:
        ids = cls._normalize_nodes(list(value))
        return dict(zip(ids or [], value.values(), strict=True))


def _nodes(root: dict, *, discover: bool = False) -> Iterator[dict]:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.get("visible") is False:
            continue
        yield node
        if not discover or node.get("type") in {"DOCUMENT", "CANVAS", "SECTION"}:
            stack.extend(reversed(node.get("children") or []))


def _color(value: dict) -> dict:
    return {
        "colorSpace": "srgb",
        "components": [value[channel] for channel in "rgb"],
        "alpha": value.get("a", 1),
    }


def _paint_color(value: dict) -> str:
    rgb = _color(value)
    components = " ".join(str(part) for part in rgb["components"])
    return literal_key(f"color(srgb {components} / {rgb['alpha']})")


def _node_note(node: dict, components: dict) -> str:
    parts = [f"{node.get('type', 'Layer')} {node.get('name', '')!r}"]
    bounds = node.get("absoluteBoundingBox") or {}
    if "width" in bounds and "height" in bounds:
        parts.append(f"{bounds['width']} × {bounds['height']} px")
    layout_keys = (
        "layoutMode",
        "layoutWrap",
        "layoutSizingHorizontal",
        "layoutSizingVertical",
        "itemSpacing",
        "paddingTop",
        "paddingRight",
        "paddingBottom",
        "paddingLeft",
        "cornerRadius",
        "primaryAxisAlignItems",
        "counterAxisAlignItems",
    )
    parts.extend(f"{key}={node[key]}" for key in layout_keys if key in node)
    if isinstance(node.get("characters"), str):
        parts.append(f"text={node['characters'][:2000]!r}")
    for paint_type in ("fills", "strokes"):
        colors = []
        for paint in node.get(paint_type) or []:
            if paint.get("visible") is False:
                continue
            if paint.get("type") == "SOLID" and paint.get("color"):
                color = dict(paint["color"])
                color["a"] = color.get("a", 1) * paint.get("opacity", 1)
                colors.append(_paint_color(color))
            elif stops := paint.get("gradientStops"):
                colors.extend(_paint_color(stop["color"]) for stop in stops)
        if colors:
            parts.append(f"{paint_type}=" + ", ".join(colors))
    style = node.get("style") or {}
    for key in ("fontFamily", "fontWeight", "fontSize", "lineHeightPx", "letterSpacing"):
        if key in style:
            parts.append(f"{key}={style[key]}")
    if component_id := node.get("componentId"):
        component = components.get(component_id, {})
        parts.append(f"component={component.get('name', component_id)!r}")
        if key := component.get("key"):
            parts.append(f"component key={key}")
    return "- " + " | ".join(parts)[:2400]


def _variable_type(variable: dict) -> str | None:
    kind = variable.get("resolvedType")
    scopes = set(variable.get("scopes", []))
    if kind == "COLOR":
        return "color"
    if kind == "STRING" and "FONT_FAMILY" in scopes:
        return "fontFamily"
    if kind == "FLOAT":
        if "FONT_WEIGHT" in scopes:
            return "fontWeight"
        return "dimension" if scopes & _DIMENSION_SCOPES else "number"
    return None


def _name_part(name: str) -> str:
    return re.sub(r"[.{}$]", "-", name).strip() or "variable"


def variables_to_tokens(meta: dict, modes: dict[str, str]) -> dict:
    collections = meta.get("variableCollections", {})
    variables = meta.get("variables", {})
    selected: dict[str, tuple[tuple[str, ...], str, object]] = {}
    used: set[tuple[str, ...]] = set()
    for collection_id, mode in modes.items():
        collection = collections.get(collection_id, {})
        if mode not in {entry["modeId"] for entry in collection.get("modes", [])}:
            raise ValueError("Figma variable mode is not in the specified collection")
    for variable_id, variable in sorted(variables.items()):
        if not (kind := _variable_type(variable)):
            continue
        collection_id = variable.get("variableCollectionId")
        collection = collections.get(collection_id, {})
        values = variable.get("valuesByMode") or {}
        mode = modes.get(collection_id, collection.get("defaultModeId") or next(iter(values), ""))
        if (value := values.get(mode)) is None:
            continue
        prefix = _name_part(collection.get("name", "Variables"))
        path = (prefix, *(_name_part(part) for part in variable["name"].split("/")))
        # Root tokens keep both "color" and "color/accent" valid in the same group.
        if path in used:
            path = (*path[:-1], path[-1] + "-" + _name_part(variable_id))
        used.add(path)
        selected[variable_id] = (path, kind, value)

    def literal(variable_id: str, value: object) -> object:
        visited = {variable_id}
        while isinstance(value, dict) and value.get("type") == "VARIABLE_ALIAS":
            target_id = value["id"]
            target = selected.get(target_id)
            if not target or target_id in visited:
                return None
            visited.add(target_id)
            value = target[2]
        return value

    def as_token_value(kind: str, value: object) -> object:
        if kind == "color" and isinstance(value, dict) and all(c in value for c in "rgb"):
            return _color(value)
        if kind == "dimension" and type(value) in {int, float}:
            return {"value": value, "unit": "px"}
        if kind in {"number", "fontWeight"} and type(value) in {int, float}:
            return value
        if kind == "fontFamily" and isinstance(value, str):
            return value
        return None

    tokens: dict = {}
    for variable_id, (path, kind, raw) in selected.items():
        value = as_token_value(kind, literal(variable_id, raw))
        if value is None:
            continue
        if isinstance(raw, dict) and raw.get("type") == "VARIABLE_ALIAS":
            target_path, target_kind, _ = selected[raw["id"]]
            if target_kind == kind:
                value = "{" + ".".join((*target_path, "$root")) + "}"
        group = tokens
        for part in path:
            group = group.setdefault(part, {})
        group["$root"] = {"$type": kind, "$value": value}
    return tokens


def _render_host(url: httpx.URL) -> bool:
    return url.host in {
        "figma-alpha-api.s3.us-west-2.amazonaws.com",
        "figma-alpha-api.s3-us-west-2.amazonaws.com",
        "s3-alpha-sig.figma.com",
    }


def _viewport(png: bytes, frame: dict) -> tuple[int, int]:
    if len(png) >= 24 and png[12:16] == b"IHDR":
        return struct.unpack(">II", png[16:24])
    bounds = frame.get("absoluteBoundingBox") or {}
    return round(bounds.get("width", 1280)), round(bounds.get("height", 720))


async def compile_figma(source: IntentSource, remote: RemoteClient) -> IntentBundle:
    params = FigmaParams.model_validate(source.params)
    credential = remote.credential("figma", params.token_secret)
    assert credential is not None
    headers = {"X-Figma-Token": credential.token.get_secret_value()}
    api = remote.settings.figma_api_url.rstrip("/") + "/v1"
    file_url = f"{api}/files/{params.file_key}"
    node_ids = params.node_ids
    if node_ids is None:
        file = await remote.json("figma", "GET", file_url, headers=headers, params={"depth": 3})
        frames = (
            node["id"]
            for node in _nodes(file["document"], discover=True)
            if node.get("type") in _FRAME_TYPES
        )
        node_ids = list(islice(frames, _FRAME_LIMIT))
    if not node_ids:
        raise ValueError("Specify node_ids for a Figma file without top-level frames.")
    if not params.visual.keys() <= set(node_ids):
        raise ValueError("Figma visual settings must refer to selected node ids")
    ids = {"ids": ",".join(node_ids)}
    data = await remote.json("figma", "GET", file_url + "/nodes", headers=headers, params=ids)
    nodes = data["nodes"]
    if any(not (nodes.get(node_id) or {}).get("document") for node_id in node_ids):
        raise ValueError("Figma node was not found")
    version = {"version": data["version"]} if data.get("version") else {}
    images = await remote.json(
        "figma",
        "GET",
        f"{api}/images/{params.file_key}",
        headers=headers,
        params={**ids, **version, "format": "png", "scale": 1},
    )
    notes = ["# Figma design", "", "Selected frames and layer properties", ""]
    bundle = IntentBundle()
    for index, node_id in enumerate(node_ids, start=1):
        entry = nodes[node_id]
        frame = entry["document"]
        url = images.get("images", {}).get(node_id)
        if not url:
            raise RemoteError("figma", f"could not render node {node_id}")
        png = await remote.asset("figma", url, _render_host)
        name = f"{source.name}.{index}"
        visual = params.visual.get(node_id)
        visual_params = None
        if visual:
            width, height = _viewport(png, frame)
            visual_params = VisualParams.model_validate(
                {
                    **visual.model_dump(exclude_none=True),
                    "width": visual.width or width,
                    "height": visual.height or height,
                }
            )
        reference = VisualAdapter.from_png(name, png, visual_params)
        bundle.context_files.update(reference.context_files)
        bundle.checks.extend(reference.checks)
        notes.extend([f"## {node_id} ({name}-baseline.png)", ""])
        notes.extend(
            _node_note(node, entry.get("components", {})) for node in islice(_nodes(frame), 120)
        )
        notes.append("")

    try:
        variables = await remote.json(
            "figma", "GET", file_url + "/variables/local", headers=headers
        )
    except RemoteError as exc:
        if exc.status not in {401, 403, 404}:
            raise
        notes.append("Figma local variables are unavailable with the current access.")
    else:
        if tokens := variables_to_tokens(variables.get("meta", {}), params.variable_modes):
            token_path = f"{source.name}-tokens.json"
            bundle.context_files[token_path] = json.dumps(
                tokens, ensure_ascii=False, indent=2
            ).encode()
            notes.append(f"Figma variables in W3C Design Tokens format: {token_path}")
    note_path = f"{source.name}-design.md"
    bundle.context_files[note_path] = ("\n".join(notes) + "\n").encode()
    bundle.prompt_notes.append(
        f"Figma frame references and design notes: .sca/context/{note_path}."
    )
    return bundle
