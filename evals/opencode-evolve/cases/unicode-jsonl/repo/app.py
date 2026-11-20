import json
def encode(records):
    return json.dumps(list(records))
def decode(text):
    return json.loads(text)
