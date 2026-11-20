def format_plan(packages, skipped) -> dict:
    """Format packages as {name, version, selected_by} and skips as {parent, name}.

    Return {"packages": [...], "skipped": [...]}. selected_by is None for
    a package first visited as a target, or the canonical name of the
    package whose dependency selected it. Preserve the sequence order.
    """
    return {"packages": [{"name": name, "version": version} for name, version, parent in packages]}
