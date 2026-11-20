def stats(values):
    values = list(values)
    return (len(values), sum(values), min(values) if values else None, max(values) if values else None)
