"""Deterministic assignment of a validated subtask graph to agents."""

from sca.kinds.coding.spec import subtask_order


def agent_lanes(subtasks: list[dict]) -> list[list[str]]:
    by_name = {sub["name"]: sub for sub in subtasks}
    dependencies = {name: sub.get("depends_on", []) for name, sub in by_name.items()}
    continuations: dict[str, str] = {}
    for name, needs in dependencies.items():
        if len(needs) == 1 and not (by_name[name].get("best_of") or by_name[needs[0]].get("best_of")):
            continuations.setdefault(needs[0], name)

    chains: list[list[str]] = []
    assigned: dict[str, list[str]] = {}
    for name in subtask_order(dependencies):
        needs = dependencies[name]
        if len(needs) == 1 and continuations.get(needs[0]) == name:
            chain = assigned[needs[0]]
        else:
            chain = []
            chains.append(chain)
        chain.append(name)
        assigned[name] = chain

    # Hints can number chains, but cannot split a chain or combine unrelated steps.
    numbered: dict[int, list[str]] = {}
    unnumbered = []
    for chain in chains:
        hints = [by_name[name]["agent"] for name in chain if by_name[name].get("agent") is not None]
        if not hints:
            unnumbered.append(chain)
            continue
        number = hints[0]
        if (any(type(hint) is not int or hint != number for hint in hints)
                or not 1 <= number <= len(chains) or number in numbered):
            return chains
        numbered[number] = chain
    available = (i for i in range(1, len(chains) + 1) if i not in numbered)
    numbered.update(zip(available, unnumbered, strict=True))
    return [numbered[i] for i in range(1, len(chains) + 1)]
