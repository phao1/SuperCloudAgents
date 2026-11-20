# Archive v1

A pack starts with HR1 and LF. Each frame consists of an unsigned
four-byte big-endian length, that many UTF-8 JSON bytes, and a four-byte
big-endian CRC32 of the raw bytes. The largest body is 65536 bytes.
A pack with only its magic is valid. The JSON uses the journal's record
fields including the local sequence, which is replaced on import. Pack
bytes should be transferred unchanged and errors surfaced to the desk.

The import service validates the entire pack before writing anything.
If an archive entry cannot become a local event, the whole import is
rejected. Export is read-only with respect to the journal. Consumers
may use the wire reader directly for maintenance and inspection.

'archive inspect FILE' summarizes the entries and stream stamp ranges without
opening the local journal. 'archive diff LEFT RIGHT' matches notices by their
content and timestamp, ignoring local sequence IDs and object field order.
It matches duplicate occurrences one-to-one and returns unmatched records in
their original order on each side. Arrays and scalar JSON types are significant.
