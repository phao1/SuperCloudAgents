# Delivery replay

The journal holds records with unique positive sequence numbers in increasing
order. Gaps are valid. A record of kind `entry` is visible to subscribers.
Records of any other kind consume journal sequences and scan slots but are
omitted from delivery.

Replay is bounded by an exclusive lower checkpoint `after` and an inclusive
upper checkpoint `through`. Its underlying scan consumes a budget measured
in physical journal records, including invisible ones. Even an empty page can
represent progress. Clients stop as soon as the page is exhausted. When no
matching records remain, it is important that the completed checkpoint reaches
the requested upper bound even when there is no physical record at that bound.
The public `Index.scan` interface specifies the exact cursor tuple.

`subscription.collect` returns a list of all visible payload strings in
sequence order and the final checkpoint. It must work for trailing invisible
records, pages consisting entirely of invisible records, and an empty range.
The page budget is a positive int. Sequence bounds are nonnegative ints with
`after <= through`. Test inputs satisfy these requirements.
