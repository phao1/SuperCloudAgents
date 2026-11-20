def apply(state, operations):
    for key,delta in operations:
        state[key] = state.get(key,0) + delta
        if state[key] < 0:
            raise ValueError('negative')
