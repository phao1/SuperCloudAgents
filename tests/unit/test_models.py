from sca.core.models import ALLOWED_TRANSITIONS, TERMINAL_STATES, TaskState, can_transition, tail


def test_transitions():
    assert set(ALLOWED_TRANSITIONS) == set(TaskState)
    for state, exits in ALLOWED_TRANSITIONS.items():
        assert all(can_transition(state, target) for target in exits)
        if state in TERMINAL_STATES:
            assert not exits
        else:
            seen, pending = set(), [state]
            while pending:
                node = pending.pop()
                if node not in seen:
                    seen.add(node)
                    pending.extend(ALLOWED_TRANSITIONS[node] - seen)
            assert seen & TERMINAL_STATES


def test_tail():
    assert tail("short") == "short"
    assert tail("abcdef", 3) == "…def"
    assert tail("你好世界", 7) == "…世界"
    assert tail("x" * 5000) == "…" + "x" * 4096
