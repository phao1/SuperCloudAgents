from app import Decoder, encode

packet = encode(3, b"trial")
decoder = Decoder()
assert decoder.feed(packet[:4]) == []
assert decoder.feed(packet[4:]) == [(3, b"trial")]
decoder.finish()
