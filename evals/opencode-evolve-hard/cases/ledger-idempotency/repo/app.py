def apply(state, events):
    for eid, account, delta in events:
        state['balances'][account] = state['balances'].get(account, 0) + delta
