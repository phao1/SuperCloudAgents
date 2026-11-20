from app import apply

assert apply({"a/b": [1]}, [{"op": "add", "path": "/a~1b/-", "value": 2}]) == {"a/b": [1, 2]}
