"""Build a champion's overlay into OpenCode and evaluate both forms."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from sqlalchemy.exc import NoResultFound

    from sca.agents.opencode_build import records_dir, run_command, write_record
    from sca.agents.opencode_prompt import version_commit
    from sca.config import Settings
    from sca.core.models import AgentOverlay
    from sca.db.repo import Store
    from sca.eval.suites import case_metadata, load_suite, select_cases


SOLIDIFY_KIND = "champion-solidify"
DEFAULT_OVERLAY = "packages/core/src/sca-default-overlay.json"


def champion_branch(champion_id: UUID) -> str:
    return f"sca/champion-{champion_id.hex[:12]}"


async def publish_overlay(
    settings: Settings, champion_id: UUID, base: str, created_at: datetime,
    overlay: dict, log: Path,
) -> str:
    with TemporaryDirectory(prefix="sca-solidify-") as temporary:
        source = Path(temporary).resolve() / "src"
        await run_command(
            ["git", "clone", "--quiet", "--no-checkout", "--shared", "--",
             settings.opencode_repo_url, str(source)], log,
        )
        git = ["git", "-C", str(source), "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false"]
        await run_command([*git, "fetch", "--quiet", "origin", base], log)
        await run_command([*git, "checkout", "--quiet", "--detach", base], log)
        target = source / DEFAULT_OVERLAY
        if target.resolve() != target or target.is_symlink():
            raise ValueError("Invalid embedded overlay path")
        if not target.exists():
            # Existing overlay-capable binaries may predate the JSON import.
            patch_path = Path("deploy/opencode/sca-overlay-default.patch")
            patch = Path(__file__).resolve().parents[3] / patch_path
            if not patch.is_file():
                patch = patch_path.resolve()
            await run_command([*git, "apply", str(patch)], log)
        loader = target.with_name("sca-overlay.ts")
        if (not target.is_file() or loader.resolve() != loader or not loader.is_file()
                or '"./sca-default-overlay.json"' not in loader.read_text(encoding="utf-8")):
            raise ValueError("The OpenCode base does not load the embedded overlay")
        defaults = json.loads(target.read_text(encoding="utf-8"))
        effective = {**defaults, **overlay}
        if "tool_descriptions" in defaults and "tool_descriptions" in overlay:
            effective["tool_descriptions"] = {
                **defaults["tool_descriptions"], **overlay["tool_descriptions"],
            }
        target.write_text(json.dumps(effective, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")), encoding="utf-8")
        await run_command([*git, "add", "--all", "--", "packages/core"], log)
        # The same champion produces the same commit when an activity or request is retried.
        date = f"{int(created_at.timestamp())} +0000"
        await run_command(
            ["env", f"GIT_COMMITTER_DATE={date}", *git, "-c", "user.name=SCA",
             "-c", "user.email=solidify@sca.local", "-c", "commit.gpgsign=false",
             "commit", "--quiet", "--allow-empty", f"--date={date}",
             "-m", f"Solidify champion {champion_id}"], log,
        )
        commit = await run_command([*git, "rev-parse", "HEAD"], log)
        await run_command(
            [*git, "push", "--quiet", "origin", f"HEAD:refs/heads/{champion_branch(champion_id)}"],
            log,
        )
    return commit


def regression_plan(
    settings: Settings, record: dict, champion: dict, overlay: dict, evidence: dict,
) -> dict:
    source = evidence["variant"]
    root = settings.eval_suites_dir.resolve()
    path = root / Path(source["suite"]).name
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != root:
        raise ValueError("The champion's evaluation suite is not installed")
    suite = load_suite(path)
    if suite["hash"] != evidence["suite_hash"]:
        raise ValueError("The champion's evaluation suite has changed")
    cases = select_cases(suite)
    repeat, case_budget = source["repeat"], source["case_budget"]
    # The eval scheduler reserves each case's cost cap in microdollars.
    budget = max(1, round(case_budget * 1e6)) * len(cases) * repeat / 1e6
    common = {
        "suite": str(path), "suite_hash": suite["hash"], "split": None,
        "case_metadata": case_metadata(cases),
        "repeat": repeat, "concurrency": source["concurrency"], "case_budget": case_budget,
        "budget": budget, "source_build_id": record["id"], "champion_id": str(champion["id"]),
    }
    variant = {"agent_backend": "opencode", "model": champion["model"]}
    return {
        "suite_name": suite["name"],
        "runs": {
            "overlay": {**common, "variant": {
                **variant, "agent_version": champion["agent_version"], "agent_overlay": overlay,
            }},
            "solidified": {**common, "variant": {
                **variant, "agent_version": record["version"], "agent_overlay": None,
            }},
        },
    }


class ChampionSolidifyActivities:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store

    def _path(self, record: dict, suffix: str = "json") -> Path:
        return records_dir(self.settings) / f"{record['id']}.{suffix}"

    @activity.defn(name="champion_solidify_prepare")
    async def prepare(self, request: dict) -> dict:
        record = request["record"]
        champion_id = UUID(record["labels"]["champion_id"])
        store = self.store.for_tenant(record["tenant_id"])
        failure = "无法读取冠军的基线版本或覆盖内容"
        try:
            champion = await store.get_champion(champion_id)
            if champion is None:
                raise ValueError("champion not found")
            overlay = json.loads(AgentOverlay.model_validate(champion["overlay"]).canonical_json())
            base = version_commit(self.settings, champion["agent_version"])
            failure = "无法读取冠军的原始评测套件，请确认套件内容与演进时一致"
            evidence = await store.get_champion_eval(champion_id)
            plan = await asyncio.to_thread(
                regression_plan, self.settings, record, champion, overlay, evidence,
            )
            failure = "无法准备固化提交，请查看构建日志"
            commit = await publish_overlay(
                self.settings, champion_id, base, champion["created_at"], overlay,
                self._path(record, "log"),
            )
        except (KeyError, NoResultFound, OSError, RuntimeError, TypeError, ValueError):
            write_record(self._path(record), {**record, "status": "failed", "error": failure})
            raise ApplicationError(failure, non_retryable=True) from None
        build = {**record, "ref": commit}
        write_record(self._path(record), build)
        return {"record": build, **plan}

    @activity.defn(name="champion_solidify_queue_regression")
    async def queue_regression(self, request: dict) -> None:
        record = request["record"]
        runs = request["runs"]
        store = self.store.for_tenant(record["tenant_id"])
        await store.create_solidify_eval_runs([
            {"id": UUID(run["id"]), "suite": request["suite_name"], "suite_hash": run["suite_hash"],
             "variant": run, "created_by": request["created_by"], "status": "queued"}
            for run in runs.values()
        ])
        write_record(self._path(record), {
            **record, "regression_status": "running",
            "overlay_eval_run_id": runs["overlay"]["id"],
            "solidified_eval_run_id": runs["solidified"]["id"],
        })

    @activity.defn(name="champion_solidify_finish_regression")
    async def finish_regression(self, request: dict) -> dict:
        path = self._path(request["record"])
        record = json.loads(path.read_text())
        statuses = request["statuses"]
        store = self.store.for_tenant(record["tenant_id"])
        for key, status in statuses.items():
            if status == "failed":
                await store.finish_eval_run(request["runs"][key]["id"], "failed")
        record["regression_status"] = next(
            (status for status in ("failed", "budget_exhausted") if status in statuses.values()),
            "completed",
        )
        write_record(path, record)
        return record

    def all(self) -> list:
        return [self.prepare, self.queue_regression, self.finish_regression]


@workflow.defn
class ChampionSolidifyWorkflow:
    @workflow.run
    async def run(self, request: dict) -> dict:
        options = {"retry_policy": RetryPolicy(maximum_attempts=3),
                   "start_to_close_timeout": timedelta(minutes=5)}
        plan = await workflow.execute_activity("champion_solidify_prepare", request, **options)
        record = await workflow.execute_child_workflow(
            "OpenCodeBuildWorkflow", plan["record"], id=f"opencode-build-{plan['record']['id']}",
            result_type=dict,
        )
        runs = {key: {**run, "id": str(workflow.uuid4())} for key, run in plan["runs"].items()}
        regression = {"record": record, "runs": runs}
        try:
            await workflow.execute_activity("champion_solidify_queue_regression", {
                **regression, "suite_name": plan["suite_name"], "created_by": request["created_by"],
            }, **options)
        except ActivityError:
            statuses = dict.fromkeys(runs, "failed")
        else:
            outcomes = await asyncio.gather(*(
                workflow.execute_child_workflow(
                    "EvalRunWorkflow", run, id=f"eval-{run['id']}", result_type=str,
                ) for run in runs.values()
            ), return_exceptions=True)
            statuses = {key: outcome if outcome in ("completed", "budget_exhausted") else "failed"
                        for key, outcome in zip(runs, outcomes, strict=True)}
        return await workflow.execute_activity("champion_solidify_finish_regression", {
            **regression, "statuses": statuses,
        }, **options)
