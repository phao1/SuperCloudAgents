class Cache:
    def __init__(self, clock):
        self.clock = clock
        self.data = {}
    def put(self, key, value, ttl):
        self.data[key] = (value, self.clock() + ttl)
    def get(self, key):
        return self.data.get(key, (None, 0))[0]
