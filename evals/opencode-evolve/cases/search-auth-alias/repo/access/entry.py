from .resolve import permissions
def authenticate(user, roles):
    return sorted({p for role in roles for p in permissions(role)})
