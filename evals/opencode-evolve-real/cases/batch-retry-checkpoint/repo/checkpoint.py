import json

State = dict[tuple[str, str], int]


def load(text: str) -> State:
    data = json.loads(text)
    return {tuple(key.split("/", 1)): value for key, value in data["applied"].items()}


def dump(state: State) -> str:
    return json.dumps({"version": 1, "applied": {
        f"{source}/{record_id}": value for (source, record_id), value in state.items()
    }})
