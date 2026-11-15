import ast
from app import total
assert total([])==0
assert total([(1.5,2),(-1,3)])==0
assert total(iter([(3,2)]))==6
tree=ast.parse(open("app.py").read())
assert not any(isinstance(n,(ast.For,ast.While)) for n in ast.walk(tree))
