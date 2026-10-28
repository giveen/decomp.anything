"""Small Windows images for architecture routing tests; no cross compiler needed."""

import struct


def arm_image(machine):
    data = bytearray(0x600)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    plus = machine == 0xAA64
    optsize = 240 if plus else 224
    struct.pack_into("<HHIIIHH", data, 0x84, machine, 1, 0, 0, 0, optsize, 0x102)
    opt = 0x98
    struct.pack_into("<H", data, opt, 0x20B if plus else 0x10B)
    struct.pack_into("<I", data, opt + 16, 0x1000)
    struct.pack_into("<Q" if plus else "<I", data, opt + (24 if plus else 28), 0x400000)
    struct.pack_into("<I", data, opt + (108 if plus else 92), 16)
    body = bytes.fromhex("40 05 80 52 c0 03 5f d6" if plus else "00 20 70 47")
    struct.pack_into("<8sIIIIIIHHI", data, opt + optsize, b".text", len(body), 0x1000, len(body), 0x400,
                     0, 0, 0, 0, 0x60000020)
    data[0x400:0x400 + len(body)] = body
    return bytes(data)
