from app import summarize
assert summarize([{'currency':'USD','quantity':2,'cents':3}]) == 'currency,cents\nUSD,6\n'
