def expand(field, low, high):
    a, b = map(int, field.split('-'))
    return list(range(a, b+1))
