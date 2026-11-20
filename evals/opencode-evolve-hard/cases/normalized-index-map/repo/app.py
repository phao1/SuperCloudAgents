def index(words):
    result={}
    for i,w in enumerate(words): result.setdefault(w.lower(),[]).append((i,w))
    return result
