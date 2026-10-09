#!/usr/bin/env python3
"""Generate public synthetic parser seeds, with no credential material."""
from pathlib import Path
import sys

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
seeds = {
    "amqp_header": b"AMQP\x00\x01\x00\x00",
    "sasl_header": b"AMQP\x03\x01\x00\x00",
    "heartbeat": b"AMQP\x00\x01\x00\x00\x00\x00\x00\x08\x02\x00\x00\x00",
    "data_binary": b"\x00\x53\x75\xa0\x05\x00\xffABC",
    "null_value": b"\x00\x53\x77\x40",
    "oversized_frame": b"AMQP\x00\x01\x00\x00\xff\xff\xff\xff\x02\x00\x00\x00",
    "malformed_map": b"\x00\x53\x74\xc1\x02\x01\x40",
    "deep_list": b"\x00\x53\x77" + b"\xc0\xff\x01"*200 + b"\x40",
    "array_of_lists": b"\x00\x53\x77\xe0\x08\x02\xc0\x02\x01\x41\x02\x01\x42",
}


def nested_list32(depth):
    wire = bytearray(b"\x00\x53\x77")
    for level in range(depth):
        wire += b"\xd0" + ((depth - level - 1) * 9 + 5).to_bytes(4, "big") + (1).to_bytes(4, "big")
    return bytes(wire + b"\x45")


# Overflows Proton's recursive decoder unless the validator rejects it first.
seeds["deep_list32_100k"] = nested_list32(100000)
for name, data in seeds.items():
    (root / name).write_bytes(data)
