# JSON Patch engine

The public function `app.apply(document, operations)` applies an ordered batch
of operations to a JSON value in memory. This file defines the supported
dialect, including errors where other JSON Patch implementations differ.
Use the standard library only, offline. Keep this file and `verify.py`
byte-for-byte unchanged and run `python -B verify.py`.

## Inputs and result

Documents and values are Python representations of JSON with string object
keys and finite numbers. There are no cycles. Operations must be a list.
Each element is an object with string `op` and `path` members. Supported
operations are listed below. Unknown members are ignored. The order of
operations matters. The result is a JSON value with no mutable object shared
with the original document or any operation value. Every copy made by the
batch must be independent of its source. Neither input may be mutated, even
if a late operation fails. Object member insertion order is preserved.
Adding a new member puts it last, and overwriting an existing member
without removing it retains its position.

## Pointer syntax

An empty path refers to the entire document. Any nonempty path must begin
with `/`. Split at slashes, then decode each token left to right. Decode
`~0` as a tilde and `~1` as a slash. An unmatched tilde or any other escape
reports `pointer`. Empty tokens are valid object keys. There is no URL
or Unicode normalization. The whole path syntax is validated even if an
early component cannot be found.

- At an object, a token is always its exact key, including `-` and `01`.
- At an array, a token must be `0` or an ASCII decimal number without a
  leading zero. Numeric tokens with arbitrary digits are allowed in the
  grammar, but an out-of-range index fails. When adding at the final token,
  indexes from 0 through the array length are valid, and `-` means the
  array length. In all other positions, numeric indexes strictly less than
  the length are required. Bad tokens, out-of-range indexes, and treating a
  scalar as a container report `missing`.
- All intermediate containers and all read, remove, or replace targets must
  already exist. Never create intermediate containers automatically.

## Operations

| Operation | Extra required member | Behavior |
| --- | --- | --- |
| `add` | `value` | Replace the whole document at the root. Set or overwrite an object member or insert before an array index, allowing the past-end position. |
| `remove` | none | Remove the existing member or item. The root is never removable. |
| `replace` | `value` | Replace an existing value in place, including at the root. |
| `copy` | string `from` | Read from the source pointer, copy its value and use the add semantics at the destination. Copying into a descendant is permitted. |
| `move` | string `from` | Read the source first. Equal decoded pointers are a no-op after the source lookup. A strict descendant destination of the source reports `descendant`. Otherwise remove the source, then add at the destination in the resulting document. Do not adjust numeric array indexes. |
| `test` | `value` | Require JSON equality at the existing target. A mismatch reports `test`. |

A proper prefix of the decoded token list defines a strict ancestor.
A move from a child to the root is permitted. To compare JSON values, compare
arrays by order and objects by keys and their values, disregarding member order.
Booleans only equal the same boolean. Integers and floats compare numerically
with each other. Strings compare exactly and null only equals null. These rules
also apply at any nesting depth.

## Diagnostics and priority

Raise `app.PatchError` (a ValueError) with public `index` and `code` attributes.
The index is the zero-based position of the failing operation, or -1 for a
non-list operations argument. Errors stop the batch.

1. Validate the operation object, its name, the types of path and source as
   applicable, and presence of value where required. Use `operation` for
   these failures, including the invalid batch shape.
2. Decode the destination, then decode the source if required. Both steps
   precede any lookup.
3. Execute the operation in the order stated above. Attempting to remove
   the root reports `root`. Other errors follow the pointer and operation
   rules. The exception message is free.

## Examples

- Adding 9 at `/items/1` in `{"items":[1,2]}` makes `{"items":[1,9,2]}`.
- Moving from `/0` to `/2` in `["a","b","c"]` gives `["b","c","a"]`.
- Removing `/` removes a member named by the empty string, when it exists.
- The token `~01` decodes to `~1`, a two-character key.
