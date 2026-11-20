from normalize import normalize
def route(events):
    return {e['id']:1 for e in events if normalize(e['type']) == 'created'}
