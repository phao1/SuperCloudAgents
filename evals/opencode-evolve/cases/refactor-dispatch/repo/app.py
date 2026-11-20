def convert(value, unit):
    if unit == 'c':
        return value
    if unit == 'f':
        return (value-32)*5/9
    if unit == 'k':
        return value-273.15
    raise ValueError('unit')
