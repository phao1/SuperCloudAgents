# Checkpoint interchange

`checkpoint.load(text)` takes JSON text and returns a dict mapping
`(source, record_id)` to its applied integer value. The entire JSON must
be an object containing exactly `version` and `applied`. Version is the
integer 1 or 2. A value is an actual JSON integer, so booleans and decimals
such as 2.0 do not qualify. JSON objects with duplicate member names are
invalid. Any invalid text or shape raises `ValueError("checkpoint")`.

- Version 1 used an object for applied. Its keys are two nonempty parts
  joined by one slash. Only keys with exactly one slash are recognized.
  Read entries in object member order.
- Version 2 uses an array for applied. Each entry must be an array of
  exactly three elements, source, record_id, and value. Both identity
  strings must be nonempty. Duplicate identities are invalid. Read in
  array order. There are no other restrictions on identity characters.
- `checkpoint.dump(state)` takes a valid dict, using the same tuple keys
  and values as the load result. It always writes version 2 and entries
  in that dictionary's insertion order. JSON whitespace and object member
  order are immaterial. The conversion must not mutate its input.

For example, `{"version":1,"applied":{"a/b":3}}` loads to
`{("a","b"):3}`. Writing it saves `{"version":2,"applied":[["a","b",3]]}`.
The version 2 entries `["a/b","c",1]` and `["a","b/c",2]` are independent.
