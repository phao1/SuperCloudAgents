def apply(base,diff):
    if not diff: return base
    return ''.join(line[1:]+'\n' for line in diff.splitlines() if line.startswith('+'))
