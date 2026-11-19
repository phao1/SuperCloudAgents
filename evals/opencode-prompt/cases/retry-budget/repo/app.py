def retry_delays(attempts, base=1, limit=8):
    return [base * i for i in range(1, attempts + 1)]


def total_delay(delays):
    return sum(delays)
