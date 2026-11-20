def reserve(stock, requests):
    result = dict(stock)
    for item, quantity in requests.items():
        if quantity < 0 or quantity > result.get(item, 0):
            raise ValueError('quantity')
        result[item] = result.get(item, 0) - quantity
    return result
