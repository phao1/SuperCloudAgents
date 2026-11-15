from app import user
assert user(" Ana ",0)=={"name":"Ana","age":0}
try: user("x",-1)
except ValueError: pass
else: raise AssertionError("negative age")
