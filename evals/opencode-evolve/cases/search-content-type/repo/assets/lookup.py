from .catalog import TYPES
def mime(filename):
    return TYPES.get(filename.split('.')[-1], 'application/octet-stream')
