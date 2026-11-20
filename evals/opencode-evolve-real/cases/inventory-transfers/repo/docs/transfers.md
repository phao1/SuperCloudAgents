# Bin transfers

A Stock maps (bin name, SKU) pairs to nonnegative quantities. Missing entries
start at zero. A transfer moves a positive quantity of one SKU between two
bins. Use the transfer object in `moves.py`; callers provide valid transfer
objects and tuple keys. Bin and SKU strings may include any characters.

Within a batch, all moves are simultaneous. Stock changes use the net of all
contributions for each identity. Quantities can go to zero or arise in a
previously empty destination. If any final quantity would be negative, raise
`ValueError("insufficient stock")` without any visible change. A batch with a
zero net effect is a successful no-op.

Stock.apply accepts raw adjustments, including zero or negative deltas.
It returns the actual nonzero deltas as a list of three-tuples
(bin name, SKU, delta), with keys in first-contribution order. The reusable
`changes.compact` API defines the normalization behavior. A zero in the input
counts when recording first-contribution order. Consolidation preserves all
stock and receipt behavior for raw adjustments as well as transfers.

`receipt.issue` prints one LF-terminated line per actual delta as
`BIN:SKU:+N` or `BIN:SKU:-N` in list order. It raises
`RuntimeError("no inventory changes")` for a genuine no-op.
