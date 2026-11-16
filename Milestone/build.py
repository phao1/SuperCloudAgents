#!/usr/bin/env python3
"""Build the self-contained offline milestone explorer using the Python stdlib."""
import argparse
import html
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def diagram_markup():
    # Keep the separately tracked source SVG untouched. The embedded copy is interactive.
    diagram = (HERE / "assets" / "coding-agent.svg").read_text(encoding="utf-8")
    diagram = re.sub(r"^<\?xml[^>]*\?>\s*", "", diagram)
    diagram = diagram.replace("<svg ", '<svg id="architecture-svg" data-theme="light" ', 1)
    diagram = diagram.replace('role="img"', 'role="group"', 1)
    diagram = diagram.replace('data-animation="trace"', 'data-animation="none"', 1)

    def inline_css(match):
        # SVG style text is parsed differently in HTML than in standalone XML.
        css = html.unescape(match[1]).replace(":root", "#architecture-svg")
        css = re.sub(r"(?m)^svg\s*\{", "#architecture-svg {", css)
        return f"<style>{css}</style>"

    return re.sub(r"<style>(.*?)</style>", inline_css, diagram, flags=re.S)


def render():
    data = json.loads((HERE / "data.json").read_text(encoding="utf-8"))
    # Prevent JSON text from containing a literal HTML script end tag.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    paths = {path for module in data["modules"] for key in ("code", "tests", "docs") for path in module[key]}
    directories = sorted(path for path in paths if (HERE.parent / path).is_dir())
    values = {
        "DIRECTORIES": json.dumps(directories).replace("<", "\\u003c"),
        "STYLES": (HERE / "styles.css").read_text(encoding="utf-8").rstrip(),
        "DIAGRAM": diagram_markup().strip(),
        "DATA": payload,
        "SCRIPT": (HERE / "app.js").read_text(encoding="utf-8").rstrip(),
    }
    template = (HERE / "template.html").read_text(encoding="utf-8")
    return re.sub(r"@@(STYLES|DIAGRAM|DATA|DIRECTORIES|SCRIPT)@@", lambda match: values[match[1]], template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if index.html is out of date")
    args = parser.parse_args()
    output = HERE / "index.html"
    generated = render()
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != generated:
            parser.exit(1, "index.html is out of date; run python3 Milestone/build.py\n")
    else:
        output.write_text(generated, encoding="utf-8")
        print(f"Built {output}")


if __name__ == "__main__":
    main()
