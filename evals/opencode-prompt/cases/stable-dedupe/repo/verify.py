from app import unique_records

assert unique_records([{"id": "x", "amount": 1}, {"id": "x", "amount": 2}]) == [
    {"id": "x", "amount": 1},
]
