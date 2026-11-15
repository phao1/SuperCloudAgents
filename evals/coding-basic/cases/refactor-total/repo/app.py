def total(lines):
    result = 0
    for price, count in lines:
        result += price * count
    return result
