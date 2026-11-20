from app import retry
calls = []
def flaky():
    calls.append(1)
    if len(calls) < 2:
        raise OSError('again')
    return 0
assert retry(flaky,3) == 0 and len(calls) == 2
for attempts in [0,-1,True]:
    try:
        retry(lambda:1,attempts)
    except ValueError:
        pass
    else:
        raise AssertionError('attempts')
