from .layers import combine
def resolve(defaults,file_values,environment):
    return combine(defaults,file_values,environment)
