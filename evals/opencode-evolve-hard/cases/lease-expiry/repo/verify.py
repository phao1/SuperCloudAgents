from app import Leases
l=Leases(lambda:0)
assert l.acquire('x','a',2) is True
assert l.owner('x')=='a'
