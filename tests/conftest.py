import sys

import pytest

# Unit coverage for container drivers and tests that require POSIX host facilities.
_POSIX_UNIT_MODULES = {
    "test_brain_handd.py",
    "test_handd.py",
    "test_handd_pty.py",
    "test_layers_build.py",
    "test_opencode_brain.py",
    "test_overlay.py",
    "test_sandbox_exec.py",
}
_POSIX_UNIT_CASES = {
    "test_agent_overlay.py": {"test_backend_file_and_env", "test_patch_applies"},
    "test_best_of.py": {
        "test_parallel_forks_with_real_git_and_independent_budgets",
        "test_attempt_budget_exhaustion_stops_fix_spend_and_keeps_verification",
        "test_each_attempt_uses_its_own_turn_limit",
        "test_human_selection_uses_review_roles_and_preserves_chosen_failed_patch",
        "test_normal_tasks_emit_identical_event_payloads",
        "test_snapshot_rejects_link_that_would_escape_after_fork",
    },
    "test_biz_retention.py": {
        "test_retention_expires_only_old_terminal_directories",
        "test_cache_cleanup_skips_symlink_ancestors_and_cleans_child_cache",
    },
    "test_champion_solidify.py": {"test_workflow_builds_from_pinned_base_then_runs_full_suite"},
    "test_codegraph.py": {"test_commit_cache_and_patch_union"},
    "test_decouple_subtasks.py": {
        "test_dag_starts_dependencies_as_soon_as_ready",
        "test_failed_or_conflicting_dependencies_block_agent_turn",
        "test_final_merge_blocks_changes_based_on_a_conflicted_dependency",
        "test_scope_rejects_turn_and_keeps_session_for_retry",
        "test_rejected_last_turn_leaves_no_out_of_scope_patch",
        "test_impact_endpoint_uses_published_commit_and_durable_bundle",
    },
    "test_design_remote.py": {"test_coding_verification_passes_git_base_as_an_argument"},
    "test_design_tokens.py": {"test_generated_check_reads_only_changed_committed_styles"},
    "test_egress_rules.py": {
        "test_ca_is_persistent_and_only_bundle_is_public",
        "test_docker_only_mounts_public_bundle_into_sandbox",
        "test_k8s_restricted_egress_uses_tls_proxy",
    },
    "test_eval_splits.py": {"test_suite_readme_symlink_is_rejected"},
    "test_harvest.py": {"test_publication_rejects_split_edits_and_suite_symlinks"},
    "test_intents_adapters.py": {"test_no_path_escape", "test_context_refuses_symlink_and_preserves_hardlink"},
    "test_maintenance_workspaces.py": {"test_prune_workspace"},
    "test_memory.py": {"test_untrusted_proposals_and_context"},
    "test_object_store.py": {"test_local_symlink_escape"},
    "test_opencode.py": {"test_build", "test_build_failure", "test_build_serialization",
                         "test_build_rejects_checkout_symlink", "test_fix_dependency_mounts",
                         "test_build_commands_honor_the_configured_timeout",
                         "test_timed_out_build_container_is_removed", "test_live_build_output"},
    "test_pi.py": {"test_launcher_writes_only_env_reference"},
    "test_portal_demo_script.py": {"test_bash_syntax", "test_shellcheck", "test_help_without_docker",
                                   "test_mode_selection", "test_invalid_mode_is_rejected",
                                   "test_gateway_starts_with_and_without_a_model_key"},
    "test_secrets.py": {"test_askpass"},
    "test_workspace_snapshots.py": {"test_publish_fork_isolation_and_gc"},
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    posix = pytest.mark.skipif(sys.platform == "win32", reason="Requires POSIX or a container driver")
    brain_pool = pytest.mark.skipif(sys.platform != "linux", reason="Brain pool uses /proc/self/fd")
    for item in items:
        if item.path.parent.name != "unit":
            continue
        module = item.path.name
        test = getattr(item, "originalname", item.name)
        if module in _POSIX_UNIT_MODULES or test in _POSIX_UNIT_CASES.get(module, ()):
            item.add_marker(posix)
        if module == "test_opencode_brain_pool.py":
            item.add_marker(brain_pool)


@pytest.fixture
async def best_of_repo(tmp_path):
    from tests.best_of_harness import make_repo

    return await make_repo(tmp_path / "repo")
