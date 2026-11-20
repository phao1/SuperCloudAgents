import sys
from service import timeout
print(timeout(sys.argv[1] if len(sys.argv)>1 else None))
