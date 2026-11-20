# ADR 004 Zones and cents

A postcode is a string. Remove all ASCII spaces, tabs and hyphens and
uppercase any ASCII letters. The result must contain 2–6 ASCII letters or
digits. Reject any other character before uppercasing, including non-ASCII
letters that uppercase to ASCII.

Pick the zone by the longest matching postcode prefix.

| Prefix | Zone |
| --- | --- |
| AA1 | metro |
| AA | local |
| B | regional |
| otherwise | remote |

| Zone | Base cents | Per started 500g | Fragile handling cents |
| --- | --- | --- | --- |
| metro | 100 | 25 | 75 |
| local | 120 | 30 | 75 |
| regional | 175 | 40 | 100 |
| remote | 250 | 65 | 125 |

Shipping for each unit is base plus the per-500g rate times the number of
started 500g blocks. Handling for each fragile unit is the zone's fee; other
units have zero handling. Multiply both unit charges by that line's quantity.
Keep all arithmetic in integer cents. If total shipping alone reaches
1000 cents, discount one tenth of that shipping amount, rounded down.
Handling neither counts toward the threshold nor gets discounted.
Total is shipping plus handling minus discount.
