def windows(values, width):
    if width <= 0:
        raise ValueError('width')
    values = list(values)
    return [tuple(values[i:i+width]) for i in range(len(values)-width+1)]
