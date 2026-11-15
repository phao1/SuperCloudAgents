def discounted(price, member):
    if member:
        return max(0, price - price * 0.1)
    return max(0, price - price * 0)
