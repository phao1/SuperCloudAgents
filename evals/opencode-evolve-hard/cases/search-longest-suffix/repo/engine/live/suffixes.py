from engine import catalog
def resolve(names):
    return [catalog.TYPES.get('.'+name.split('.')[-1],catalog.DEFAULT) for name in names]
