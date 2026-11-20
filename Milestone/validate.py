"""Validate the authored milestone snapshot and its generated HTML."""

import json
import re
import subprocess
from collections import Counter
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

MILESTONE = Path(__file__).resolve().parent
REPOSITORY = MILESTONE.parent
BASELINE_COMMIT = "a5a0d63dfc1e25dfc75ec62a203761478d88e32a"
BASELINE_DATE = "2026-11-18"
ORIGINAL_NODE_COUNT = 50
V2_ADDITIONS = {
    "analysis_kind", "model_gateway", "portal_loop", "opencode_versions",
    "brain_split", "brain_pool", "trajectory_checks", "admission_control",
    "gateway_limiter", "workspace_overlay", "workspace_forks", "pi_backend",
    "idle_suspend", "handd",
}
TOTAL_MODULE_COUNT = 64
STATUSES = {"done", "partial", "adapted", "planned"}
PLACEHOLDER = re.compile(r"@@[A-Z][A-Z0-9_]*@@")


class EmbeddedSnapshot(HTMLParser):
    def __init__(self):
        super().__init__()
        self.snapshots = []
        self.node_ids = []
        self.capturing = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-node-id" in attrs:
            self.node_ids.append(attrs["data-node-id"])
        if tag == "script" and attrs.get("id") == "milestone-data":
            if attrs.get("type") != "application/json":
                raise ValueError("milestone-data must be an application/json script")
            self.snapshots.append("")
            self.capturing = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.capturing = False

    def handle_data(self, data):
        if self.capturing:
            self.snapshots[-1] += data


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_svg(expected_hash):
    path = MILESTONE / "assets" / "coding-agent.svg"
    require(sha256(path.read_bytes()).hexdigest() == expected_hash,
            "Original SVG checksum differs from meta.diagramSha256")
    ids = [node.attrib["data-node-id"] for node in ElementTree.parse(path).iter()
           if "data-node-id" in node.attrib]
    require(len(ids) == ORIGINAL_NODE_COUNT and len(set(ids)) == ORIGINAL_NODE_COUNT,
            f"Original SVG must contain {ORIGINAL_NODE_COUNT} unique node IDs")
    return set(ids)


def validate_module(module):
    module_id = module["id"]
    require(bool(re.fullmatch(r"[a-z][a-z0-9_]*", module_id)), f"Invalid ID {module_id!r}")
    require(module["status"] in STATUSES, f"{module_id} has an unknown status")
    for key in ("name", "subtitle", "group", "current", "how"):
        require(isinstance(module[key], str) and bool(module[key].strip()),
                f"{module_id}.{key} must be nonempty text")
    for key in ("stack", "done", "gaps", "code", "tests", "docs"):
        items = module[key]
        require(isinstance(items, list) and all(isinstance(item, str) and item.strip()
                                               for item in items),
                f"{module_id}.{key} must be a list of nonempty strings")
        require(len(items) == len(set(items)), f"{module_id}.{key} contains duplicate entries")

    if module["status"] != "planned":
        require(module["done"] and module["code"] and module["tests"],
                f"{module_id} needs implementation and test evidence")
    if module["status"] != "done":
        require(module["gaps"], f"{module_id} needs an explicit capability boundary")
    require(module["docs"], f"{module_id} needs design evidence")
    if "reviewNote" in module:
        require(isinstance(module["reviewNote"], str) and module["reviewNote"].strip(),
                f"{module_id}.reviewNote must be nonempty text")

    for key in ("code", "tests", "docs"):
        for path in module[key]:
            relative = PurePosixPath(path)
            target = (REPOSITORY / relative).resolve()
            require(not relative.is_absolute() and ".." not in relative.parts
                    and REPOSITORY in target.parents and target.exists(),
                    f"{module_id}.{key} has a missing or invalid repo path: {path}")


def validate_snapshot(data):
    meta = data["meta"]
    modules = data["modules"]
    require(meta["commit"] == BASELINE_COMMIT and meta["date"] == BASELINE_DATE,
            "The recorded commit or date differs from the audited baseline")
    svg_ids = validate_svg(meta["diagramSha256"])
    require(not svg_ids & V2_ADDITIONS, "A V2 addition duplicates an original SVG node")
    expected_ids = svg_ids | V2_ADDITIONS
    require(len(expected_ids) == TOTAL_MODULE_COUNT, "Module count constants disagree")
    ids = [module["id"] for module in modules]
    require(len(ids) == len(expected_ids) and len(set(ids)) == len(ids),
            f"Snapshot must contain {len(expected_ids)} unique module IDs")
    require(set(ids) == expected_ids,
            f"Module ID coverage mismatch. Missing: {sorted(expected_ids - set(ids))}. "
            f"Unexpected: {sorted(set(ids) - expected_ids)}.")
    coverage = json.loads((MILESTONE / "coverage-map.json").read_text(encoding="utf-8"))
    require(set(coverage["modules"]) == set(ids), "Coverage map IDs differ from data.json")
    for module_id, entry in coverage["modules"].items():
        require(entry["platform"] in {"any", "linux-host", "k8s", "planned"},
                f"Invalid coverage platform: {module_id}")
        require(isinstance(entry["manual"], str) and bool(entry["how"].strip()),
                f"Invalid coverage instructions: {module_id}")
        require(isinstance(entry["evidence"], list), f"Invalid rules: {module_id}")
        require(entry["platform"] != "planned" or not entry["evidence"],
                f"Planned module has evidence: {module_id}")
    for module in modules:
        validate_module(module)
    paths = {path.rstrip("/") for module in modules
             for key in ("code", "tests", "docs") for path in module[key]}
    for path in sorted(paths):
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{BASELINE_COMMIT}:{path}"],
            cwd=REPOSITORY, capture_output=True, timeout=5, check=False,
        )
        require(result.returncode == 0,
                f"Evidence path does not exist at {BASELINE_COMMIT[:7]}: {path}")
    return svg_ids


def validate_html(data, svg_ids):
    document = (MILESTONE / "index.html").read_text(encoding="utf-8")
    placeholders = sorted(set(PLACEHOLDER.findall(document)))
    require(not placeholders, f"Unresolved HTML placeholders: {', '.join(placeholders)}")
    parser = EmbeddedSnapshot()
    parser.feed(document)
    parser.close()
    require(len(parser.snapshots) == 1, "Generated HTML needs one milestone-data script")
    require(json.loads(parser.snapshots[0]) == data,
            "Embedded snapshot differs from data.json. Rebuild index.html.")
    require(Counter(parser.node_ids) == Counter(svg_ids),
            "Generated HTML does not contain exactly the original SVG nodes")


def main():
    try:
        data = json.loads((MILESTONE / "data.json").read_text(encoding="utf-8"))
        svg_ids = validate_snapshot(data)
        validate_html(data, svg_ids)
    except (OSError, ValueError, TypeError, KeyError, ElementTree.ParseError,
            subprocess.TimeoutExpired) as error:
        print(f"Validation failed: {error}")
        return 1

    print(f"Validated {len(data['modules'])} modules ({len(svg_ids)} original SVG nodes), "
          "status values, evidence paths in checkout and baseline commit, "
          "baseline, and embedded HTML snapshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
