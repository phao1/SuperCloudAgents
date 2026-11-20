# ADR 020 — Incremental inspection of an HR1 pack

        The existing v1 byte format and error names both remain in force.
        The public iterator 'iter_frames(chunks)' now yields a record as soon
        as its checksum passes. Constructing the iterator does not ask for a
        chunk. A call to next reads no chunks after the one containing the end
        of the requested frame. Chunk boundaries have no wire meaning, even
        in the magic, length, multibyte text or checksum. An empty byte chunk
        does not mean end; end is iterator exhaustion. Bound carry-over by
        one frame plus the unused portion of the current source chunk.

        Treat exhaustion between frames as success. Partial magic is an
        invalid header. Partial length/body/checksum is a truncated frame.
        Validate the length before looking for the body, then checksum before
        decoding a body. A source exception propagates when decoding reaches
        it, after any already yielded valid frames. File import still validates
        the complete input before writing; inspect and diff use the shared
        reader and continue to reject invalid late frames.
