class Leases:
    def __init__(self,clock): self.data={}
    def acquire(self,key,owner,ttl):
        self.data[key]=owner
        return True
    def owner(self,key): return self.data.get(key)
