from runner import Runner


def confirm(runner: Runner, rows: list[tuple[str, str, int]]) -> list[int]:
    runner.apply(rows)
    persisted = runner.storage.applied()
    return [persisted[source, record_id] for source, record_id, _ in rows]
