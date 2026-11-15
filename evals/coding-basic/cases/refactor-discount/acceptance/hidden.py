from app import discounted
assert discounted(-10,True)==0
assert discounted(0,False)==0
assert discounted(35,False)==35
assert discounted(10,True)==9

import ast
tree = ast.parse(open("app.py").read())
assert sum(isinstance(node, ast.Mult) for node in ast.walk(tree)) <= 1
