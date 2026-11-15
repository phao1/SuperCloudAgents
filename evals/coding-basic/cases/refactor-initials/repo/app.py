def initials(names):
    return [n.strip()[0].upper() if n.strip() else "?" for n in names]
