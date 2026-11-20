# Desk commands

Set --home before the command. The directory is created on the first
write. A bad request exits at status 2 with one diagnostic on stderr
and no stdout. Successful non-file commands print a compact JSON
value and a newline. The integer stamp on an event is supplied by
the desk and can move backwards. The journal gives each event its
own increasing sequence independently of that stamp.

    publish STREAM KIND KEY PAYLOAD --at STAMP
    journal [--after SEQ] [--limit COUNT]
    summary [--stream STREAM]
    register NAME PATTERN [PATTERN ...] [--exclude PATTERN ...]
    unregister NAME
    routes STREAM
    watch NAME [--after SEQ] [--limit COUNT]
    archive export FILE [--after SEQ]
    archive import FILE
    prune THROUGH

Stream names have one or more dot-separated ASCII segments. A segment
starts with a letter and continues with letters, digits, underscores
or hyphens. Stream keys and kinds are case-sensitive. Keys can contain
non-ASCII text. Payload must be a JSON object. Lists, nested objects,
empty strings and zero are all legal inside it.

The summary orders streams lexically and lists kind counts in lexical
order. If a stream has no events it is omitted. Counts and latest
sequence describe the retained local journal, not the archive.
Registration updates an existing name in its current position.
Unregistration removes that name and its filters.
