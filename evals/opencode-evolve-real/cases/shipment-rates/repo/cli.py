import json
import sys

from service import quote


def main():
    print(json.dumps(quote(**json.load(sys.stdin))))


if __name__ == "__main__":
    main()
