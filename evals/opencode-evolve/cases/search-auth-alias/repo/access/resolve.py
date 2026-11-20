from .catalog import ROLES
from .legacy import normalize
def permissions(role):
    return ROLES.get(normalize(role), ())
