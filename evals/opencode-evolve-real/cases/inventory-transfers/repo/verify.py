from moves import Move
from receipt import issue
from stock import Stock

stock = Stock({("a", "item"): 4})
assert issue(stock, [Move("a", "b", "item", 2)]) == "a:item:-2\nb:item:+2\n"
