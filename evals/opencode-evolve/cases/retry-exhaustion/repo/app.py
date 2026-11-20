def retry(call, attempts, retry_on=(OSError,)):
    for _ in range(attempts):
        try:
            return call()
        except Exception:
            pass
    return None
