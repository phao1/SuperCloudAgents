from app import authenticate
assert authenticate('u',[' ADMIN ']) == ['read','write']
