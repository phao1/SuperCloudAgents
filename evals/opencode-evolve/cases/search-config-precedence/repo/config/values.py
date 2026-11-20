def choose(key,defaults,file_values,environment):
    return environment.get(key) or file_values.get(key) or defaults.get(key)
