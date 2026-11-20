from itertools import groupby
def runs(items,key=lambda x:x):
    return iter([(k,tuple(g)) for k,g in groupby(items,key)])
