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
}
for name, data in seeds.items():
    (root / name).write_bytes(data)
