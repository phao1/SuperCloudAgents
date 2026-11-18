"""Conservative trace heuristics; findings are evidence, not a filesystem replay."""

import re
from pathlib import PurePosixPath

RULES = frozenset({"tool_output_integrity", "agent_reports_tool_fault",
                   "workaround_after_tool_failure"})


def validate_checks(checks: object) -> list[str]:
    if not isinstance(checks, list) or any(not isinstance(c, str) or c not in RULES for c in checks):
        raise ValueError("trajectory_checks must be a list of built-in rule ids")
    return list(dict.fromkeys(checks))


def overall_passed(row: dict) -> bool:
    return row.get("acceptance_passed") is True and row.get("trajectory_passed") is not False


def path_of(inputs: dict) -> str:
    path = inputs.get("path", inputs.get("filePath", ""))
    return str(PurePosixPath(str(path)).relative_to("/workspace")) if str(path).startswith("/workspace/") else str(path)


def finding(rule: str, event: dict, message: str) -> dict:
    return {"rule_id": rule, "message": message, "event_seq": event["seq"]}


def non_ascii(text: str) -> set[str]:
    return {char for char in text if ord(char) > 127}


def patch_sides(patch: str) -> tuple[str, str]:
    before, after = [], []
    for line in patch.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith(("-", " ")):
            before.append(line[1:])
        if line.startswith(("+", " ")):
            after.append(line[1:])
    return "\n".join(before), "\n".join(after)


def tool_output_integrity(events: list[dict]) -> list[dict]:
    findings = []
    for index, event in enumerate(events):
        if event["type"] != "agent.file_change":
            continue
        payload = event["payload"]
        inputs = payload.get("input", {})
        path = path_of(inputs)
        expected = non_ascii(str(inputs.get("newString", inputs.get("content", ""))))
        evidence: tuple[dict, str] | None = None
        output = str(payload.get("output_tail", ""))
        if expected and output.startswith(("diff --git ", "--- ")) and not payload.get("metadata", {}).get("truncated"):
            _, after = patch_sides(output)
            if expected - non_ascii(after):
                evidence = event, after
        for change in payload.get("changes", []):
            changed_path = path_of({"path": change.get("file", change.get("path", path))})
            if path and changed_path != path:
                continue
            before, after = patch_sides(str(change.get("patch", "")))
            # An edit may corrupt untouched text outside its replacement range.
            preserved = non_ascii(before) - non_ascii(str(inputs.get("oldString", "")))
            if (expected | preserved) - non_ascii(after) and change.get("patch") and not payload.get("metadata", {}).get("truncated"):
                evidence = event, after
                break
        if expected and path and evidence is None:
            for later in events[index + 1:]:
                data = later["payload"]
                if later["type"] == "agent.file_change" and path_of(data.get("input", {})) == path:
                    break
                if later["type"] == "agent.command" and re.search(r"\b(git\s+(restore|checkout)|sed|python|printf)\b", str(data.get("command", ""))) and path in str(data.get("command", "")):
                    break
                if later["type"] == "agent.command" and "git diff" in str(data.get("command", "")):
                    output = str(data.get("output_tail", ""))
                    for section in output.split("diff --git ")[1:]:
                        if section.splitlines()[0] != f"a/{path} b/{path}":
                            continue
                        _, after = patch_sides(section)
                        if expected - non_ascii(after) and after and not output.startswith("…"):
                            evidence = later, after
                    if evidence:
                        break
                if later["type"] != "agent.tool" or data.get("name") != "read":
                    continue
                read = data.get("input", {})
                output = str(data.get("output_tail", ""))
                if path_of(read) != path or any(k in read for k in ("offset", "limit", "startLine", "endLine")):
                    continue
                if output and not output.startswith("…") and not re.search(r"truncat|more lines", output, re.IGNORECASE):
                    if expected - non_ascii(output):
                        evidence = later, output
                    break
        if evidence:
            findings.append(finding("tool_output_integrity", evidence[0],
                                    f"文件工具写入 {path or '文件'} 时丢失非 ASCII 文本（编辑事件 {event['seq']}）。"))
    return findings


FAULT = re.compile(
    r"(?:\b(?:edit|write|file|patch)\b.{0,70}\btools?\b.{0,100}\b(?:corrupt\w*|strip\w*|drop\w*|remov\w*|los[et]\w*)\b.{0,100}\b(?:text|content|characters?|CJK|Chinese|bytes)\b"
    r"|\btools?\b.{0,80}\b(?:corrupt\w*|strip\w*|drop\w*)\b.{0,80}\b(?:text|content|characters?|CJK|Chinese)\b"
    r"|(?:工具|编辑|写入).{0,60}(?:损坏|破坏|剥离|丢失|丢弃|删除|去掉|移除).{0,60}(?:中文|汉字|字符|内容|文本)"
    r"|(?:中文|汉字|字符|内容|文本).{0,40}(?:被|让).{0,30}工具.{0,30}(?:损坏|丢失|删除|移除))", re.IGNORECASE | re.DOTALL)


def agent_reports_tool_fault(events: list[dict]) -> list[dict]:
    return [finding("agent_reports_tool_fault", event, "智能体报告文件工具损坏了内容。")
            for event in events if event["type"] == "agent.message"
            and FAULT.search(str(event["payload"].get("text", "")))]


def workaround_after_tool_failure(events: list[dict]) -> list[dict]:
    findings = []
    edits: dict[str, tuple[int, object]] = {}
    for index, event in enumerate(events):
        payload = event["payload"]
        if event["type"] == "agent.file_change":
            path = path_of(payload.get("input", {}))
            if path:
                edits[path] = (index, payload.get("turn"))
        elif event["type"] == "agent.command":
            command = str(payload.get("command", ""))
            if not re.search(r"\bgit\s+(?:checkout\s+--(?=\s)|restore\b)|\bsed\s+[^\n]*-i|\bpython(?:3)?\b[^\n]*(?:write|open)|\bprintf\b[^\n]*>", command):
                continue
            for path, (edited, turn) in edits.items():
                target = re.escape(path) if path.startswith("/") else r"(?:/workspace/|\./)?" + re.escape(path)
                if index - edited <= 12 and turn == payload.get("turn") and re.search(r"(?<![\w/.-])" + target + r"(?![\w/.-])", command):
                    findings.append(finding("workaround_after_tool_failure", event,
                                            f"文件工具编辑后，Shell 随即恢复或重写了 {path}。"))
    return findings


def evaluate(events: list[dict], checks: list[str]) -> tuple[bool, list[dict]]:
    checks = validate_checks(checks)
    rules = {"tool_output_integrity": tool_output_integrity,
             "agent_reports_tool_fault": agent_reports_tool_fault,
             "workaround_after_tool_failure": workaround_after_tool_failure}
    ordered = sorted(events, key=lambda event: event["seq"])
    findings = [item for check in checks for item in rules[check](ordered)]
    return not findings, findings
