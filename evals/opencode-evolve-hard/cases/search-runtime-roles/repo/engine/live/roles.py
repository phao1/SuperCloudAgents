from engine import catalog
def resolve(roles):
    return sorted({p for role in roles if role in catalog.ROLES for p in catalog.ROLES[role][0]})
