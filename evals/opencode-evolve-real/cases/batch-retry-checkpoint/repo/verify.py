from delivery import confirm
from runner import Runner
from storage import Storage

runner = Runner(Storage('{"version":1,"applied":{"old/id":1}}'))
assert confirm(runner, [("a/b", "c", 2), ("a", "b/c", 3)]) == [2, 3]
