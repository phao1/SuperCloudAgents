from app import Dialect, records, sniff

source = 'a;b\r\n"1;2";3\r\n\n'
assert records(source, ";") == [["a", "b"], ["1;2", "3"]]
assert sniff(source) == Dialect(";", 2, 2, "\r\n")
