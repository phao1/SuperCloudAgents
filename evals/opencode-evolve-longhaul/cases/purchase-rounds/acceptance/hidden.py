"""Hidden end-to-end acceptance for purchase release rounds."""

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

PROGRAM = 'groveboard'
GROUP = 'purchase-rounds'
START = 'assemble'
FINISH = 'release'
FINISH_ACTOR = 'lead'
FINISH_ROLE = 'manager'
START_EVENT = 'purchase-rounds.assembled'
FINISH_EVENT = 'purchase-rounds.released'
FIRST = {'id': 'orchard-round', 'orders': ['po-c', 'po-b', 'po-a'], 'day': '2025-04-11'}
SECOND = {'id': 'orchard-round', 'expected_cents': 9830, 'day': '2025-04-12'}
STARTED = {'scope': 'north',
 'status': 'assembled',
 'supplier': 'orchard',
 'day': '2025-04-11',
 'orders': ['po-a', 'po-b', 'po-c'],
 'lines': [{'product': 'apple', 'units': 16, 'cents': 6830},
           {'product': 'pear', 'units': 5, 'cents': 3000}],
 'total_cents': 9830,
 'released': None}
FINISHED = {'scope': 'north',
 'status': 'released',
 'orders': ['po-a', 'po-b', 'po-c'],
 'total_cents': 9830,
 'released': '2025-04-12'}
HISTORY_TABLE = 'purchase-orders'
HISTORY_KEY = 'round'
HISTORICAL = [{'id': 'paper-round',
  'scope': 'north',
  'status': 'released',
  'supplier': 'orchard',
  'day': '2025-04-02',
  'released': '2025-04-03',
  'orders': ['paper-po-a', 'paper-po-b'],
  'lines': [{'product': 'apple', 'units': 3, 'cents': 1230},
            {'product': 'pear', 'units': 7, 'cents': 4410}],
  'total_cents': 5640},
 {'id': 'paper-south',
  'scope': 'south',
  'status': 'released',
  'supplier': 'farm',
  'day': '2025-04-01',
  'released': '2025-04-02',
  'orders': ['paper-po-s'],
  'lines': [{'product': 'plum', 'units': 2, 'cents': 1060}],
  'total_cents': 1060}]
SECTIONS = [('north', '2025-04-10', {'awaiting_release': [], 'released_cents': 5640}),
 ('north', '2025-04-11', {'awaiting_release': ['orchard-round'], 'released_cents': 5640}),
 ('north', '2025-04-12', {'awaiting_release': [], 'released_cents': 15470}),
 ('south', '2025-04-12', {'awaiting_release': [], 'released_cents': 1060})]
PROTECTED = {'README.md': '2231654030d58a23bee61130f299ae06c12e33e5c15e29e6b423bf91f6380934',
 'docs/development.md': 'f0de482bf52e3fb8775cc04c26c6342e6bcec6fe9cc1a958d8466a1f8580c94c',
 'docs/operations.md': '61c0529de3332825937bcf6201c7f1bc3e3a19a82bf2003e9d866e829399d94d',
 'docs/registers.md': 'd7224440a31c98d16c110d180b5a5d0770c2871d1c02f1212ab508a54c22edf2',
 'fixtures/workspace-v1.json': 'fa9d71084c69616221039f2fac576c9bf10dcf8f046e376bcaff2a237c078337',
 'tests/__init__.py': '3ed25a44fd2e2ce081a61fb7fba37d04eb13e3c438c75c7e058664469420b0ce',
 'tests/project.py': '11910449aaa866bd171087776959248e7a1e58ca838be0adc340e27a327a6fc0',
 'tests/runtime.py': 'b71f0f1fda550d79ea5cf01562b080572b110d940bb49e45379e185204990c98',
 'tests/test_bulletins.py': '94549805849e977e2418ee41edfed1e9b9f49144b7a1144b0abedc5c847af67a',
 'tests/test_dispatch.py': '4ab170159a29e676e3cd4c977c790959ec041aeb7cae0feaa6fbd8f7875be746',
 'tests/test_domain.py': 'c84e17a636db7a7b0fb69f1453ef96881e8f13f19534516e1dbf764e9a189838',
 'tests/test_exchange.py': '309fa593cd3489de138bc43f3d213532b5355506f9a4b41dc12a1e0a9d476d77',
 'tests/test_fields.py': '85280529d06e1920d759d352b3ecdb868c7daa010ad2258b1de8a50cf428d338',
 'tests/test_followups.py': 'd54c8e80e60cd2eef19ae628622548d22ef8f0fba0cf3e41d3247a4f8e166768',
 'tests/test_intake.py': '8aa1157a0a444971db46f145ded98eb2736e9761e256c851fcac889fb35277f7',
 'tests/test_registers.py': '2c701feaf8492de21a8d49bcd919eace02286a31c615797f284bafcc449f7558',
 'tests/test_runtime.py': 'cc82edea75221531cbf60035d7750366cc7dfb9d6b3acfb987e8f569fceb21ca',
 'tests/test_storage.py': '3480c67d281a9114c89b4f403500d695529303dce073d2c7014c792c84b08122',
 'tools/read_v1.py': 'a7442f28650e3ac8fd46f19d3bac248bf439e7e650b1762a3146213359c63220',
 'verify.py': 'bd1f1ff1bf7c263211e7e380bf1cf15f485c9f734a0baf7d9f19e4bf2e37160f'}


def workflow(root):
    path = fresh(root, "release")
    blocked(path, START, {**FIRST, "orders": ["po-a"]}, status=400, code="bad_request")
    for orders in (["po-a", "po-g"], ["po-a", "po-s"], ["po-a", "paper-po-a"]):
        blocked(path, START, {**FIRST, "orders": orders})
    act(path, START, FIRST)
    blocked(path, START, {**FIRST, "id": "duplicate", "orders": ["po-a", "po-b"]})
    blocked(path, FINISH, {**SECOND, "expected_cents": 9829})
    amend(path, "purchase-orders", "po-a", cents=428)
    amend(path, "purchase-orders", "po-b", cents=425)
    blocked(path, FINISH, SECOND)
    amend(path, "purchase-orders", "po-a", cents=425)
    amend(path, "purchase-orders", "po-b", cents=430)
    act(path, FINISH, SECOND)
    for order in FIRST["orders"]:
        includes(legacy(path, "purchase-orders", order), {
            "released": SECOND["day"], "round": FIRST["id"], "status": "released", "version": 1,
        })
    pending = read(path, "purchase-orders/pipeline", {"scope": "north", "day": "2025-04-12"})
    expect([row["supplier"] for row in pending], ["garden"])
    blocked(path, FINISH, SECOND)

    broken = fresh(root, "mixed-release-days")
    amend(broken, "purchase-orders", "paper-po-b", released="2025-04-05")
    reject(broken, "store/upgrade", {"name": GROUP}, status=422, code="bad_store")


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
