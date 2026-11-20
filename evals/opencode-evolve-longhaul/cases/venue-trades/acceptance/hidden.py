"""Hidden end-to-end acceptance for venue block trades."""

import ast
import json
import signal
import subprocess
import sys
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

PROGRAM = 'framehouse'
GROUP = 'venue-trades'
START = 'propose'
FINISH = 'approve'
FINISH_ACTOR = 'lead'
FINISH_ROLE = 'manager'
START_EVENT = 'venue-trades.proposed'
FINISH_EVENT = 'venue-trades.approved'
FIRST = {'id': 'room-switch', 'left': 'block-b', 'right': 'block-a', 'day': '2025-04-10'}
SECOND = {'id': 'room-switch', 'day': '2025-04-11'}
STARTED = {'scope': 'north',
 'status': 'proposed',
 'day': '2025-04-10',
 'approved': None,
 'blocks': [{'booking': 'block-a',
             'from': 'north',
             'to': 'south',
             'day': '2025-04-13',
             'end': '2025-04-14'},
            {'booking': 'block-b',
             'from': 'south',
             'to': 'north',
             'day': '2025-04-15',
             'end': '2025-04-16'}]}
FINISHED = {'scope': 'north',
 'status': 'approved',
 'approved': '2025-04-11',
 'blocks': [{'booking': 'block-a',
             'from': 'north',
             'to': 'south',
             'day': '2025-04-13',
             'end': '2025-04-14'},
            {'booking': 'block-b',
             'from': 'south',
             'to': 'north',
             'day': '2025-04-15',
             'end': '2025-04-16'}]}
HISTORY_TABLE = 'bookings'
HISTORY_KEY = 'trade'
HISTORICAL = [{'id': 'paper-trade',
  'scope': 'north',
  'status': 'approved',
  'day': '2025-04-01',
  'approved': '2025-04-01',
  'blocks': [{'booking': 'paper-block-a',
              'from': 'north',
              'to': 'south',
              'day': '2025-04-03',
              'end': '2025-04-03'},
             {'booking': 'paper-block-b',
              'from': 'south',
              'to': 'north',
              'day': '2025-04-04',
              'end': '2025-04-04'}]}]
SECTIONS = [('north', '2025-04-10', {'pending': ['room-switch'], 'approved': ['paper-trade']}),
 ('south', '2025-04-10', {'pending': ['room-switch'], 'approved': ['paper-trade']}),
 ('south', '2025-04-11', {'pending': [], 'approved': ['paper-trade', 'room-switch']}),
 ('north', '2025-04-11', {'pending': [], 'approved': ['paper-trade', 'room-switch']})]
PROTECTED = {'README.md': 'c7d41718b387c725c114599474c31aa4f6e3f494849a80b765ab3c04af22942a',
 'docs/development.md': 'f0de482bf52e3fb8775cc04c26c6342e6bcec6fe9cc1a958d8466a1f8580c94c',
 'docs/operations.md': '61c0529de3332825937bcf6201c7f1bc3e3a19a82bf2003e9d866e829399d94d',
 'docs/registers.md': '55af7bc65134d8ca46b28160d6b7884414cae540334cfa73c2a61842e6a93fa3',
 'fixtures/workspace-v1.json': '086129310136c21c4ca7d53b8112616abba0f0312d5c49c4c6740f6520c3bdc3',
 'tests/__init__.py': '3ed25a44fd2e2ce081a61fb7fba37d04eb13e3c438c75c7e058664469420b0ce',
 'tests/project.py': 'e8b13f3a9a1cd6020c594d25a06daabdc19178cd3afa0931bad4c8b0f642659d',
 'tests/runtime.py': 'b71f0f1fda550d79ea5cf01562b080572b110d940bb49e45379e185204990c98',
 'tests/test_bulletins.py': '94549805849e977e2418ee41edfed1e9b9f49144b7a1144b0abedc5c847af67a',
 'tests/test_dispatch.py': '4ab170159a29e676e3cd4c977c790959ec041aeb7cae0feaa6fbd8f7875be746',
 'tests/test_domain.py': '2c9f1735d23e2e2fc4dc044914064d4327586587910f0ca27e61817bd0e79b09',
 'tests/test_exchange.py': '309fa593cd3489de138bc43f3d213532b5355506f9a4b41dc12a1e0a9d476d77',
 'tests/test_fields.py': '85280529d06e1920d759d352b3ecdb868c7daa010ad2258b1de8a50cf428d338',
 'tests/test_followups.py': 'd54c8e80e60cd2eef19ae628622548d22ef8f0fba0cf3e41d3247a4f8e166768',
 'tests/test_intake.py': '8aa1157a0a444971db46f145ded98eb2736e9761e256c851fcac889fb35277f7',
 'tests/test_registers.py': '2c701feaf8492de21a8d49bcd919eace02286a31c615797f284bafcc449f7558',
 'tests/test_runtime.py': 'cc82edea75221531cbf60035d7750366cc7dfb9d6b3acfb987e8f569fceb21ca',
 'tests/test_storage.py': '3480c67d281a9114c89b4f403500d695529303dce073d2c7014c792c84b08122',
 'tools/read_v1.py': 'a7442f28650e3ac8fd46f19d3bac248bf439e7e650b1762a3146213359c63220',
 'verify.py': 'baa6ed1e6a60e66b145d78324a88e717b55cbe2fe13587f47f32a16f3f89e050'}


def workflow(root):
    path = fresh(root, "trade")
    blocked(path, START, {**FIRST, "right": "block-b"})
    blocked(path, START, {**FIRST, "left": "big-block"})
    blocked(path, START, {**FIRST, "day": "2025-04-14"}, status=400, code="bad_request")
    act(path, START, FIRST)
    blocked(path, START, {**FIRST, "id": "duplicate", "left": "block-a", "right": "block-b"})
    amend(path, "bookings", "block-a", end="2025-04-15")
    blocked(path, FINISH, SECOND)
    amend(path, "bookings", "block-a", end="2025-04-14")
    post(path, "bookings/add", {"id": "third-block", "scope": "south", "day": "2025-04-14",
                               "end": "2025-04-14", "production": "moss", "status": "confirmed"})
    blocked(path, FINISH, SECOND)
    post(path, "bookings/cancel", {"id": "third-block", "reason": "Move to another week"})
    final = act(path, FINISH, SECOND)
    expect(final["blocks"], FINISHED["blocks"])
    for record_id, original, destination in (("block-a", "north", "south"), ("block-b", "south", "north")):
        includes(legacy(path, "bookings", record_id), {"scope": destination, "former_scope": original,
                 "trade": FIRST["id"], "swapped": SECOND["day"], "version": 1})
    items = read(path, "bookings/calendar", {"scope": "south", "first": "2025-04-13", "last": "2025-04-14"})
    expect([item["id"] for item in items], ["block-a"])
    blocked(path, FINISH, SECOND)

    invalid = fresh(root, "unpaired-trade")
    data = snapshot(invalid)
    del data["tables"]["bookings"]["paper-block-b"]
    workspace_json(invalid, data)
    reject(invalid, "store/upgrade", {"name": GROUP}, status=422, code="bad_store")


def expect(actual, expected):
    assert type(actual) is type(expected) and actual == expected, (actual, expected)


def includes(row, fields):
    for field, expected in fields.items():
        expect(row.get(field), expected)


def fresh(root, label):
    path = root / (label + ".json")
    copyfile("fixtures/workspace-v1.json", path)
    return path


def invoke(path, method, route, params=None, *, actor="lead", locale="en", status=200):
    request = import_module(f"{PROGRAM}.api").request
    result = request(path, actor, method, "/v1/" + route, params, locale=locale)
    assert result.status == status, (route, params, status, result.status, result.body)
    return result.body


def read(path, route, params=None, **options):
    return invoke(path, "GET", route, params, **options)["data"]


def post(path, route, params, **options):
    return invoke(path, "POST", route, params, **options)["data"]


def act(path, action, params, **options):
    return post(path, f"{GROUP}/{action}", params, **options)


def reject(path, route, params, *, code="conflict", status=409, actor="lead"):
    before = path.read_bytes()
    body = invoke(path, "POST", route, params, actor=actor, status=status)
    expect(body["error"]["code"], code)
    expect(path.read_bytes(), before)


def blocked(path, verb, params, **options):
    reject(path, f"{GROUP}/{verb}", params, **options)


def rows(path, scope, status=None, **options):
    return read(path, GROUP + "/list", {"scope": scope, **({"status": status} if status else {})}, **options)


def section(path, scope, day):
    report = read(path, "desk/report", {"scope": scope, "day": day}, actor="viewer")
    assert GROUP in report, f"desk report is missing {GROUP}"
    return report[GROUP]


def legacy(path, table, record_id):
    return read(path, table + "/show", {"id": record_id}, actor="viewer")


def amend(path, table, record_id, **changes):
    return post(path, table + "/amend", {"id": record_id, "changes": changes})


def snapshot(path):
    return json.loads(path.read_text(encoding="utf-8"))


def workspace_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def cli(path, group, verb, values, *, actor="lead", locale="en", status=0):
    flags = []
    for key, value in values.items():
        flags.extend(("--" + key.replace("_", "-"), value if isinstance(value, str) else json.dumps(value)))
    result = subprocess.run(
        [sys.executable, "-B", "-m", PROGRAM, "--state", str(path), "--actor", actor,
         "--locale", locale, group, verb, *flags],
        capture_output=True, check=False, text=True, encoding="utf-8", timeout=5,
    )
    expect(result.returncode, status)
    return json.loads(result.stdout if status == 0 else result.stderr)


def v1_reader(path, table):
    result = subprocess.run(
        [sys.executable, "-B", "tools/read_v1.py", str(path), table],
        capture_output=True, check=True, text=True, encoding="utf-8", timeout=5,
    )
    return json.loads(result.stdout)


def upgrade(path, *, imported):
    result = post(path, "store/upgrade", {"name": GROUP})
    expect(result, {"name": GROUP, "imported": imported})


def history_rows(path, expected):
    for scope in sorted({row["scope"] for row in expected} | {"north", "south"}):
        found = rows(path, scope, actor="viewer")
        scoped = sorted((row for row in expected if row["scope"] == scope), key=lambda row: row["id"])
        expect([row["id"] for row in found], [row["id"] for row in scoped])
        for row, contract in zip(found, scoped, strict=True):
            includes(row, {**contract, "version": 2})


def renamed(value, renames):
    if isinstance(value, str):
        return renames.get(value, value)
    if isinstance(value, list):
        return [renamed(item, renames) for item in value]
    if isinstance(value, dict):
        return {renamed(key, renames): renamed(item, renames) for key, item in value.items()}
    return value


def history(root):
    path = fresh(root, "upgrade")
    before = read(path, "bundle/export")
    old_rows = v1_reader(path, HISTORY_TABLE)
    post(path, "store/upgrade", {"name": "bulletins"})
    unrelated = snapshot(path)["extensions"]["bulletins"]
    reject(path, "store/upgrade", {"name": GROUP}, code="forbidden", status=403, actor="clerk")
    upgrade(path, imported=len(HISTORICAL))
    after = snapshot(path)
    expect(after["format"], 1)
    expect(after["upgrades"][GROUP], 2)
    expect(after["extensions"][GROUP]["schema"], 2)
    expect(sorted(after["extensions"][GROUP]["records"]), sorted(row["id"] for row in HISTORICAL))
    expect(after["extensions"]["bulletins"], unrelated)
    expect(after["audit"][-1]["event"], "store.upgraded")
    expect(after["audit"][-1]["record_id"], GROUP)
    expect(after["audit"][-1]["details"], {"version": 2, "imported": len(HISTORICAL)})
    expect(v1_reader(path, HISTORY_TABLE), old_rows)
    expect(read(path, "bundle/export"), before)
    history_rows(path, HISTORICAL)
    stable = path.read_bytes()
    cli_data = cli(path, "store", "upgrade", {"name": GROUP})["data"]
    expect(cli_data, {"name": GROUP, "imported": 0})
    expect(path.read_bytes(), stable)

    varied = fresh(root, "other-history")
    data = snapshot(varied)
    ids = {record["id"] for record in HISTORICAL}
    records = data["tables"][HISTORY_TABLE]
    renames = {
        name: f"archive-{name}"
        for record_id, row in records.items() if row[HISTORY_KEY] in ids
        for name in (record_id, row[HISTORY_KEY])
    }
    assert ids <= renames.keys(), (ids, renames)
    data["tables"][HISTORY_TABLE] = renamed(records, renames)
    workspace_json(varied, data)
    upgrade(varied, imported=len(HISTORICAL))
    history_rows(varied, renamed(HISTORICAL, renames))

    live = fresh(root, "live-before-upgrade")
    final = lifecycle(live)
    old_read = v1_reader(live, HISTORY_TABLE)
    upgrade(live, imported=len(HISTORICAL))
    includes(snapshot(live)["extensions"][GROUP]["records"][final["id"]], final)
    expect(v1_reader(live, HISTORY_TABLE), old_read)
    expect(sorted(snapshot(live)["extensions"][GROUP]["records"]),
           sorted({final["id"], *(row["id"] for row in HISTORICAL)}))

    conflict = fresh(root, "history-collision")
    conflicting = snapshot(conflict)
    record_id = HISTORICAL[0]["id"]
    conflicting["extensions"] = {GROUP: {"schema": 2, "records": {
        record_id: {"id": record_id, "version": 2, "status": "different"},
    }}}
    workspace_json(conflict, conflicting)
    reject(conflict, "store/upgrade", {"name": GROUP})


def lifecycle(path):
    first = act(path, START, FIRST, actor="clerk")
    includes(first, {"id": FIRST["id"], "version": 2, **STARTED})
    second = act(path, FINISH, SECOND, actor=FINISH_ACTOR)
    includes(second, {"id": FIRST["id"], "version": 2, **FINISHED})
    return second


def distinct_records(root):
    path = fresh(root, "new-record-identifiers")
    data = snapshot(path)
    renames = {row_id: "field-" + row_id for table in data["tables"].values()
               for row_id in table if row_id not in ("north", "south")}
    renames[FIRST["id"]] = "branch-" + FIRST["id"]
    data["tables"] = renamed(data["tables"], renames)
    workspace_json(path, data)
    initial = act(path, START, renamed(FIRST, renames), actor="clerk")
    final = act(path, FINISH, renamed(SECOND, renames), actor=FINISH_ACTOR)
    for record, expected in ((initial, STARTED), (final, FINISHED)):
        includes(record, {"id": renames[FIRST["id"]], "version": 2, **renamed(expected, renames)})
    expect(rows(path, final["scope"], final["status"], actor="viewer"), [final])


def workflows(root):
    workflow(root)
    distinct_records(root)


def reports(root):
    path = fresh(root, "report")
    upgrade(path, imported=len(HISTORICAL))
    final = lifecycle(path)
    for scope, day, expected in SECTIONS:
        expect(section(path, scope, day), expected)
    for scope in ("north", "south"):
        result = read(path, "desk/report", {"scope": scope, "day": "2025-04-20"})
        assert {"title", "activity", "visible_bulletins", "followups", GROUP} <= result.keys()
    stable = path.read_bytes()
    upgrade(path, imported=0)
    expect(path.read_bytes(), stable)
    actual = rows(path, final["scope"], final["status"])
    assert final in actual, actual


def interfaces(root):
    APP = import_module(f"{PROGRAM}.runtime").APP
    path = fresh(root, "interfaces")
    assert GROUP in APP.schema and APP.schema[GROUP].version == 2
    assert GROUP in APP.upgrades
    for verb, method, role in ((START, "POST", "operator"), (FINISH, "POST", FINISH_ROLE),
                               ("list", "GET", "reader")):
        command = APP.registry.by_name.get((GROUP, verb))
        assert command is not None, f"unregistered command {GROUP} {verb}"
        expect(command.method, method)
        expect(command.path, f"/v1/{GROUP}/{verb}")
        expect(APP.registry.by_route.get((method, command.path)), command)
        expect(APP.policy.get(command.permission), role)
        for locale in ("en", "fr"):
            message = APP.messages[locale].get(command.message, "")
            assert len(message) > 3 and message != command.message, (verb, locale, message)
        assert APP.messages["en"][command.message] != APP.messages["fr"][command.message], verb

    blocked(path, START, FIRST, status=403, code="forbidden", actor="viewer")
    before = path.read_bytes()
    denied = cli(path, GROUP, START, FIRST, actor="viewer", locale="fr", status=1)
    expect(denied["error"]["code"], "forbidden")
    expect(path.read_bytes(), before)
    prepared = cli(path, GROUP, START, FIRST, actor="clerk", locale="fr")
    includes(prepared["data"], {"id": FIRST["id"], "version": 2, **STARTED})
    expect(prepared["message"], APP.messages["fr"][APP.registry.by_name[GROUP, START].message])
    denied_actor = "clerk" if FINISH_ROLE == "manager" else "viewer"
    blocked(path, FINISH, SECOND, status=403, code="forbidden", actor=denied_actor)
    finished = cli(path, GROUP, FINISH, SECOND, actor=FINISH_ACTOR)
    includes(finished["data"], {"id": FIRST["id"], "version": 2, **FINISHED})
    expect(finished["message"], APP.messages["en"][APP.registry.by_name[GROUP, FINISH].message])
    before = path.read_bytes()
    params = {"scope": finished["data"]["scope"], "status": finished["data"]["status"]}
    from_cli = cli(path, GROUP, "list", params, actor="viewer", locale="fr")
    by_route = invoke(path, "GET", GROUP + "/list", params, actor="viewer", locale="fr")
    expect(from_cli, by_route)
    expect(from_cli["data"], [finished["data"]])
    expect(path.read_bytes(), before)
    invoke(path, "GET", GROUP + "/list", {"scope": "unknown"}, actor="viewer", status=404)
    invoke(path, "GET", GROUP + "/list", {"scope": "north", "status": "unknown"}, actor="viewer", status=400)
    expect(path.read_bytes(), before)
    audit = [event for event in snapshot(path)["audit"] if event["event"].startswith(GROUP + ".")]
    expect([(event["event"], event["actor"], event["record_id"], event["version"]) for event in audit],
           [(START_EVENT, "clerk", FIRST["id"], 1), (FINISH_EVENT, FINISH_ACTOR, FIRST["id"], 1)])
    for event in audit:
        assert event["event"] in APP.events
        expect(event["details"]["scope"], finished["data"]["scope"])


def documents():
    guide = Path("docs") / (GROUP + ".md")
    assert guide.is_file(), f"add an operator guide at {guide}"
    text = guide.read_text(encoding="utf-8")
    for command in (f"{GROUP} {START}", f"{GROUP} {FINISH}", f"{GROUP} list",
                    f"store upgrade --name {GROUP}"):
        assert command in text, f"{guide} needs an example of {command}"
    new_tests = [path for path in Path("tests").glob("test_*.py") if str(path) not in PROTECTED]
    assert new_tests, "add focused unittest coverage for the feature"
    count = sum(node.name.startswith("test_") for path in new_tests
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.FunctionDef))
    assert count >= 2, "add at least two feature tests"


def main():
    signal.setitimer(signal.ITIMER_REAL, 33)
    for name, digest in PROTECTED.items():
        path = Path(name)
        assert path.is_file() and not path.is_symlink(), name
        assert sha256(path.read_bytes()).hexdigest() == digest, name
    verify = subprocess.run([sys.executable, "-B", "verify.py"], check=False, timeout=12)
    if verify.returncode:
        print("Public verification failed.", flush=True)
        return 1
    print("Public verification passed.", flush=True)
    with TemporaryDirectory(prefix="longhaul-acceptance-") as tmp:
        root = Path(tmp)
        stage = ""
        try:
            for stage, check in (("history", history), ("workflow", workflows),
                                 ("report", reports), ("interfaces", interfaces)):
                check(root)
            stage = "documentation"
            documents()
        except AssertionError:
            print(f"Feature check failed: {stage}", flush=True)
            raise
    print("Feature checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
