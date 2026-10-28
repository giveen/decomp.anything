"""The FPO table of a 32-bit PE: function starts and exact sizes written by the compiler.

A 32-bit MSVC release build has no exception directory, so without this table the splitter falls
back to guessing from call targets. These tests build the debug directory by hand, so they need no
compiler and run everywhere (tests/test_pe.py needs MinGW-w64 and only covers x86-64 images).
"""

import struct

from decompnet.rom import pe
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


# ------------------------------------------------------- a whole image through read()
#
# An FPO table is exact but *partial*: only a frame-pointer-omitted function gets a record, so
# unlike .pdata it cannot stand in for the scans that find everything else. These build a real
# 32-bit PE -- three functions, an FPO record for one of them -- and check the other two are
# still found. Hand-built for the same reason as the tests above: no compiler needed.

TEXT_RVA, TEXT_RAW = 0x2000, 0x400
RDATA_RVA, RDATA_RAW = 0x1000, 0x200
RECORDS_RAW = 0x280  # inside .rdata, so a .text that claims to be megabytes long cannot reach it
ENTRY, CALLED, UNREACHED = 0x402000, 0x402020, 0x402040

def _pe(records, *, text_raw_size=TEXT_RAW // 2, debug_dir=None):
    """A 32-bit PE holding three functions, with `records` as its FPO table.

    `text_raw_size` is what the section header claims, which is what the 4 MiB direct-call cutoff
    reads; the bytes behind it stay small so the test runs fast. `debug_dir` overrides the data
    directory entry.
    """
    data = bytearray(0x800)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    struct.pack_into("<4sHHIIIHH", data, 0x80, b"PE\0\0", I386, 2, 0, 0, 0, 0xE0, 0x010F)
    opt = 0x98
    struct.pack_into("<H", data, opt, 0x10B)  # PE32
    struct.pack_into("<I", data, opt + 16, TEXT_RVA)  # AddressOfEntryPoint
    struct.pack_into("<I", data, opt + 28, IMAGE_BASE)
    struct.pack_into("<I", data, opt + 92, 16)  # NumberOfRvaAndSizes
    # .rdata holds the debug directory, and comes first so a .text section that claims to be
    # megabytes long does not cover its RVA.
    struct.pack_into("<8sIIIIIIHHI", data, 0x178, b".rdata", RDATA_RAW, RDATA_RVA,
                     RDATA_RAW, RDATA_RAW, 0, 0, 0, 0, 0x40000040)
    struct.pack_into("<8sIIIIIIHHI", data, 0x178 + 40, b".text", text_raw_size, TEXT_RVA,
                     text_raw_size, TEXT_RAW, 0, 0, 0, 0, 0x60000020)
    code = bytearray(b"\xCC" * (TEXT_RAW // 2))
    code[0x00:0x06] = b"\xE8\x1B\x00\x00\x00\xC3"  # entry: call CALLED ; ret
    code[0x20:0x23] = b"\x31\xC0\xC3"              # reached only by that call
    code[0x40:0x43] = b"\x31\xDB\xC3"              # reached by nothing: only the gap scan finds it
    data[TEXT_RAW:TEXT_RAW + len(code)] = code
    struct.pack_into("<IIHHIIII", data, RDATA_RAW, 0, 0, 0, 0, 3, 16 * len(records), 0, RECORDS_RAW)
    for j, (start, size) in enumerate(records):
        struct.pack_into("<IIIHH", data, RECORDS_RAW + 16 * j, start, size, 0, 0, 0)
    struct.pack_into("<II", data, opt + 96 + 8 * 6, *(debug_dir or (RDATA_RVA, 28)))
    return bytes(data)


def _starts(data):
    return [f.vram for f in pe.read(data).functions]


def test_image_without_an_fpo_table_finds_all_three():
    """The baseline the two tests below must not fall below."""
    assert _starts(_pe([], debug_dir=(0, 0))) == [ENTRY, CALLED, UNREACHED]


def test_partial_fpo_table_keeps_the_fallback_scans():
    """A record for the entry only: the call target and the unreferenced function still turn up."""
    assert _starts(_pe([(TEXT_RVA, 6)])) == [ENTRY, CALLED, UNREACHED]


def test_partial_fpo_table_keeps_scanning_a_large_image():
    """Above 4 MiB of code a complete table switches the direct-call scan off. FPO must not."""
    assert _starts(_pe([(TEXT_RVA, 6)], text_raw_size=(4 << 20) + 1)) == [ENTRY, CALLED, UNREACHED]


def test_fpo_bounds_are_still_exact():
    """Discovery carries on around the table without giving up the sizes the compiler wrote."""
    found = {f.vram: f for f in pe.read(_pe([(TEXT_RVA, 6)])).functions}
    assert (len(found[ENTRY].code), found[ENTRY].exact) == (6, True)
    assert not found[CALLED].exact and not found[UNREACHED].exact


def test_truncated_debug_directory_is_not_unpacked():
    """_rva() returns a short slice, not None, when the directory runs off the end of the file."""
    data = _pe([(TEXT_RVA, 6)], debug_dir=(0x800 - 10, 28))
    assert _fpo(data, pe._headers(data)[4], [(0, 0)] * 6 + [(0x800 - 10, 28)], IMAGE_BASE, I386) == []
    assert _starts(data) == [ENTRY, CALLED, UNREACHED]  # and the image still imports


def test_a_complete_table_still_skips_the_scans():
    """The .pdata contract the flag protects: a table that names every function ends discovery."""
    data = _pe([], debug_dir=(0, 0))
    _machine, arch, base, _plus, sections, entry, _dirs = pe._headers(data)
    only_entry = [(ENTRY, ENTRY + 6)]
    small = pe.build(data, arch, base, sections, [], only_entry, base + entry)[0]
    assert [f.vram for f in small] == [ENTRY, CALLED]  # gap scan off, direct calls still cheap
    big_data = _pe([], debug_dir=(0, 0), text_raw_size=(4 << 20) + 1)
    big_sections = pe._headers(big_data)[4]
    big = pe.build(big_data, arch, base, big_sections, [], only_entry, base + entry)[0]
    assert [f.vram for f in big] == [ENTRY]  # both scans off
