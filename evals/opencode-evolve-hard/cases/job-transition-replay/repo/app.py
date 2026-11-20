def reduce(events):
    return {key:('running',1) for key,action in events}
