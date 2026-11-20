from app import apply
assert apply('a\n','@@ -1,1 +1,1 @@\n-a\n+b\n')=='b\n'
