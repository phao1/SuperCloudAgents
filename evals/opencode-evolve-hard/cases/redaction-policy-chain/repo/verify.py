from app import redact
assert redact({'password':'x'})=={'password':'[redacted]'}
