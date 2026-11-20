def redact(value):
    return {k:'[redacted]' if k=='password' else v for k,v in value.items()}
