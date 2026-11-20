# Framed records

This codec reads a local instrument file that can be supplied in chunks of
any size. Implement `app.encode(kind, payload)` and `app.Decoder()` as below.
Use only the Python standard library. No files, sockets, or current time
are used by the codec. Keep this specification and `verify.py` byte-for-byte
unchanged. Run `python -B verify.py`.

## Wire representation

A record is the following concatenation. It has no separator or padding.

| Section | Size | Format |
| --- | --- | --- |
| Magic | 2 bytes | ASCII bytes `FR` |
| Kind | 1 byte | The integer 1, 2, or 3 |
| Length | 2 bytes | Unsigned big-endian payload size, 0 through 4096 inclusive |
| Payload | Length bytes | Arbitrary bytes including zeroes or `FR` |
| Check | 4 bytes | Unsigned big-endian CRC-32 of the kind byte, both length bytes and payload |

The CRC-32 is the unsigned integer returned by `zlib.crc32` with its default
initial value. Magic bytes are outside its input. Concatenating encoded
records produces a stream. An empty stream is valid.

`encode(kind, payload) -> bytes` emits exactly one record. Kind must be an
actual int (booleans are invalid) from 1 to 3 inclusive. Payload must be
bytes (neither bytearray nor str). Bad kind raises `ValueError("kind")`,
bad payload type raises `TypeError("bytes required")` and oversized payload
raises `ValueError("length")`, in that order of validation. Empty payloads
are valid for every kind.

## Incremental decoder

Each new Decoder is open and starts at byte offset 0. `feed(chunk)` takes
bytes and returns a fresh list of zero or more `(kind, payload)` tuples,
in stream order. It immediately returns every complete valid record that
ends within input supplied so far and has not already been returned.
The payload is bytes. A call with empty bytes is permitted and returns an
empty list. Record boundaries can split any part of the input, including
magic, length and check. The decoder must keep only an incomplete trailing
record between successful calls. Many small feeds are supported. Separate
decoders do not share state.

When a decoder is open, a non-bytes argument raises
`TypeError("bytes required")` and does not change the stream or its state.
Catch this exception separately from wire errors. On a wire error, raise
`app.FrameError` (a ValueError) with public `code` and `offset` attributes.
Offset is the absolute zero-based byte offset in this stream. Message
text may vary. If a single call reads valid frames then encounters bad
data, that call raises instead of returning a partial list. Frames returned
successfully by earlier calls remain valid.

Validate fields in this order and as soon as the corresponding bytes arrive,
without waiting for the entire header or payload:

- At each of the first two bytes, a magic mismatch gives `magic` at the
  offset of the mismatching byte.
- An invalid kind gives `kind` at the offset of that byte.
- A length greater than 4096 gives `length` at the offset of the first
  length byte, once both length bytes are present.
- When the full check arrives, a CRC mismatch gives `checksum` at the
  offset of the first check byte.
- Process records from left to right, stopping at the first wire error.
  Never scan for a later magic sequence after an error.

## End of stream

`finish() -> None` on an open decoder closes a complete stream, even an
empty stream. If unfinished bytes remain, raise `FrameError("truncated", offset)`
with `offset` at the first byte of the unfinished frame. Finish on a closed
decoder returns None again. A feed after closing, even an empty feed, raises
`RuntimeError("closed decoder")`. Any wire or truncation error leaves the
decoder failed. Both feed and finish on a failed decoder raise
`RuntimeError("failed decoder")`. These state checks precede validation of
the argument to feed.

For example, `feed(b"F")` returns an empty list, but a following
`feed(b"x")` immediately raises with `("magic", 1)`. For an empty
payload, the CRC starts at byte 5. If that complete record is followed by
one byte `F` and then end of input, the truncation offset is 9.
