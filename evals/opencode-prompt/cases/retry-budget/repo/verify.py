from app import retry_delays

assert retry_delays(3) == [1, 2, 4]
