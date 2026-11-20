from .values import choose
def combine(defaults,file_values,environment):
    return {k: choose(k,defaults,file_values,environment) for k in defaults.keys() | file_values.keys() | environment.keys()}
