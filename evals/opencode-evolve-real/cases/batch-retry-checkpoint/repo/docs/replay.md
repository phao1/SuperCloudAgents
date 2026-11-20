# Import acknowledgment

The importer receives a batch of `(source, record_id, value)` triples. Both
parts of the identity are nonempty strings. They may contain slashes, spaces
or Unicode. Compare the two fields exactly without normalization. Values are
signed integers; batch inputs are valid triples.

A first occurrence stores a value and an exact replay adds no new entry.
A conflicting value for a previously applied identity raises
`ValueError("conflict")` and leaves the checkpoint unchanged. The durable
checkpoint is the source of truth after reopening a Runner. It preserves
first-application order and uses the compatibility rules in
[checkpoints.md](checkpoints.md). After a successful apply,
`delivery.confirm` returns the persisted values in the same order as the
requested triples, including repeated triples and signed or zero values.
