"""The FPO table of a 32-bit PE: function starts and exact sizes written by the compiler.

A 32-bit MSVC release build has no exception directory, so without this table the splitter falls
back to guessing from call targets. These tests build the debug directory by hand, so they need no
compiler and run everywhere (tests/test_pe.py needs MinGW-w64 and only covers x86-64 images).
"""

import struct

from decompnet.rom.pe import PeSection, _fpo

IMAGE_BASE = 0x400000
DEBUG_RVA = 0x1000
DEBUG_RAW = 0x200
FPO_RAW = 0x400
I386, AMD64 = 0x014C, 0x8664


def _image(entries, records, *, fpo_at=FPO_RAW, mapped=False):
    """A buffer holding a debug directory and its FPO records, plus the section that maps it.

    `entries` are (type, size, address_of_raw_data, pointer_to_raw_data); `records` are the
    (start_rva, size) pairs written at `fpo_at`.
    """
    data = bytearray(0x1000)
    for i, (kind, nbytes, addr_raw, ptr_raw) in enumerate(entries):
        struct.pack_into("<IIHHIIII", data, DEBUG_RAW + 28 * i, 0, 0, 0, 0, kind, nbytes, addr_raw, ptr_raw)
    for j, (start, size) in enumerate(records):
        # FPO_DATA: start RVA, byte size, then locals/params/flags this reader ignores.
        struct.pack_into("<IIIHH", data, fpo_at + 16 * j, start, size, 0, 0, 0)
    sections = [PeSection(".rdata", DEBUG_RVA, 0x800, 0x800, DEBUG_RAW, 0x40000040)]
    if mapped:
        # A second section so AddressOfRawData can be resolved as an RVA.
        sections.append(PeSection(".debug", 0x2000, 0x400, 0x400, fpo_at, 0x40000040))
    dirs = [(0, 0)] * 6 + [(DEBUG_RVA, 28 * len(entries))]
    return bytes(data), sections, dirs


def test_reads_starts_and_sizes():
    data, sections, dirs = _image([(3, 16 * 3, 0, FPO_RAW)],
                                  [(0x1000, 0x12), (0x1020, 0x14), (0x1040, 0x0B)])
    assert _fpo(data, sections, dirs, IMAGE_BASE, I386) == [
        (0x401000, 0x401012), (0x401020, 0x401034), (0x401040, 0x40104B)]


def test_skips_other_debug_types():
    """Only type 3 is FPO; a MISC or CODEVIEW entry beside it must not be read as records."""
    data, sections, dirs = _image([(4, 272, 0, FPO_RAW), (3, 16, 0, FPO_RAW), (2, 47, 0, FPO_RAW)],
                                  [(0x1000, 0x12)])
    assert _fpo(data, sections, dirs, IMAGE_BASE, I386) == [(0x401000, 0x401012)]


def test_falls_back_to_address_of_raw_data():
    """PointerToRawData is normally the only usable field, but honour a mapped RVA when it is 0."""
    data, sections, dirs = _image([(3, 16, 0x2000, 0)], [(0x1000, 0x12)], mapped=True)
    assert _fpo(data, sections, dirs, IMAGE_BASE, I386) == [(0x401000, 0x401012)]


def test_drops_empty_records_and_keeps_the_largest_duplicate():
    data, sections, dirs = _image([(3, 16 * 3, 0, FPO_RAW)],
                                  [(0x1000, 0x10), (0x1000, 0x40), (0x1080, 0)])
    assert _fpo(data, sections, dirs, IMAGE_BASE, I386) == [(0x401000, 0x401040)]


def test_sorted_by_address():
    data, sections, dirs = _image([(3, 16 * 3, 0, FPO_RAW)],
                                  [(0x3000, 0x10), (0x1000, 0x10), (0x2000, 0x10)])
    assert [lo for lo, _hi in _fpo(data, sections, dirs, IMAGE_BASE, I386)] == [0x401000, 0x402000, 0x403000]


def test_ignored_for_x86_64():
    """x86-64 images carry .pdata, which is authoritative; FPO is a 32-bit fallback only."""
    data, sections, dirs = _image([(3, 16, 0, FPO_RAW)], [(0x1000, 0x12)])
    assert _fpo(data, sections, dirs, IMAGE_BASE, AMD64) == []


def test_no_debug_directory():
    data, sections, _dirs = _image([(3, 16, 0, FPO_RAW)], [(0x1000, 0x12)])
    assert _fpo(data, sections, [(0, 0)] * 7, IMAGE_BASE, I386) == []
    assert _fpo(data, sections, [(0, 0)] * 3, IMAGE_BASE, I386) == []
