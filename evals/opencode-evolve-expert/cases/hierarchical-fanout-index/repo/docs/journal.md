# Local journal, format 1

The journal is an append-ordered NDJSON file. A blank file is an empty
journal. Each record has seq, stream, kind, key, value and stamp. The
reader rejects malformed files (including repeated sequence numbers)
instead of silently ignoring the trailing lines. Sequence zero
represents a consumer that has never read from the journal.

Multi-record writes stage all entries and replace the current file
once. Run one process per desk while writing. An in-process lock
protects multiple threads using the same Journal object. Older
releases inferred the high-water mark from the retained records on
opening a journal. A local prune used to be advisory housekeeping.

The route directory is stored separately because dispatch rules
can be changed independently from incoming events. Its order is
registration order. Records are always returned as snapshots.
