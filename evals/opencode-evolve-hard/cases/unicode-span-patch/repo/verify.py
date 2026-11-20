from app import patch
assert patch('abc',[(1,2,'X')])=='aXc'
