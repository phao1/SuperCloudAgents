"""Hidden acceptance for this standalone fixture."""

import signal
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

checks = 0


def check(actual, expected):
    global checks
    expected_type = type(expected)
    assert isinstance(actual, expected_type) and actual == expected, (actual, expected)
    if expected_type in (bool, int):
        assert type(actual) is expected_type, (actual, expected)
    checks += 1


def raises(error_type, call, *args, **kwargs):
    global checks
    try:
        call(*args, **kwargs)
    except error_type as error:
        checks += 1
        return error
    raise AssertionError(f"{call!r} did not raise {error_type.__name__}")


def verify():
    for name, digest in PROTECTED.items():
        assert sha256(Path(name).read_bytes()).hexdigest() == digest, name
    result = subprocess.run([sys.executable, "-B", "verify.py"], check=False, timeout=12)
    if result.returncode != 0:
        print("Public verification failed.", file=sys.stderr)
        raise SystemExit(1)

PROTECTED = {
    'SPEC.md': 'a2ba49e7827c530f1471adc9857f53b2597a0541f19c2c1a47d2b15f20640d2e',
    'verify.py': 'b66bf33f9e869fa4b5058dd788631f466ea82f390254dc8405afbc8b23d8c549',
}


def check_feature():
    import random
    import struct
    import tracemalloc
    import zlib

    from app import Decoder, FrameError, encode

    def wire(kind, payload):
        body = struct.pack(">BH", kind, len(payload)) + payload
        return b"FR" + body + struct.pack(">I", zlib.crc32(body))

    rng = random.Random(704)
    for size in (0, 1, 2, 9, 255, 256, 511, 4096):
        payload = rng.randbytes(size)
        records = [(2, b"FR\0"), (3, payload), (1, payload[::-1])]
        pieces = [wire(*record) for record in records]
        packet = b"".join(pieces)
        for pair, expected in zip(records, pieces):
            check(encode(*pair), expected)
        for step in (1, 2, 3, 5, 7, 31, 257, len(packet)):
            decoder = Decoder()
            starts = [len(piece) for piece in pieces]
            ends = [starts[0], sum(starts[:2]), sum(starts)]
            consumed = 0
            for begin in range(0, len(packet), step):
                batch = decoder.feed(packet[begin:begin + step])
                assert batch == [record for end, record in zip(ends, records) if consumed < end <= begin + step]
                consumed = begin + step
            check(decoder.finish(), None)
    for leading in (b"", wire(1, b""), wire(3, b"FR") * 2):
        for invalid, code, relative in [
            (b"x", "magic", 0), (b"Fx", "magic", 1), (b"FR\x00", "kind", 2),
            (b"FR\x04", "kind", 2), (b"FR\x01\x10\x01", "length", 3),
            (wire(3, b"test")[:-1] + bytes([wire(3, b"test")[-1] ^ 1]), "checksum", 9),
        ]:
            decoder = Decoder()
            error = raises(FrameError, decoder.feed, leading + invalid)
            check((error.code, error.offset), (code, len(leading) + relative))
            assert str(raises(RuntimeError, decoder.feed, None)) == "failed decoder"
            assert str(raises(RuntimeError, decoder.finish)) == "failed decoder"
        for length in (1, 2, 4, 5, 7, 11):
            decoder = Decoder()
            decoder.feed(leading + wire(3, b"xyz")[:length])
            error = raises(FrameError, decoder.finish)
            assert (error.code, error.offset) == ("truncated", len(leading))
    for kind, payload, exc, message in [
        (True, "", ValueError, "kind"), (0, b"", ValueError, "kind"),
        (4, b"abc", ValueError, "kind"), (1.0, b"", ValueError, "kind"),
        (1, bytearray(), TypeError, "bytes required"), (1, b" " * 4097, ValueError, "length"),
    ]:
        assert str(raises(exc, encode, kind, payload)) == message
    first, second = Decoder(), Decoder()
    check(first.feed(b"F"), [])
    raises(TypeError, first.feed, memoryview(b"R"))
    check(second.feed(b""), [])
    check(second.finish(), None)
    check(second.finish(), None)
    assert str(raises(RuntimeError, second.feed, None)) == "closed decoder"
    check(first.feed(wire(1, b"hi")[1:]), [(1, b"hi")])

    packet = wire(3, rng.randbytes(4096))
    decoder = Decoder()
    tracemalloc.start()
    try:
        for _ in range(5):
            batch = decoder.feed(packet * 240)
            assert len(batch) == 240
            del batch
        retained, _ = tracemalloc.get_traced_memory()
        check(retained < len(packet) * 240, True)
    finally:
        tracemalloc.stop()


if __name__ == "__main__":
    signal.alarm(25)
    verify()
    check_feature()
    assert 60 <= checks <= 200, checks
    print(f"{checks} feature checks passed.")
