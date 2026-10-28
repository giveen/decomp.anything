"""The shared context.h every project starts from.

It is compiled by every toolchain in the registry, from GCC 2.7.2 and Visual C++ 4.2 to current
Clang, so its fixed-width typedefs have to be spelled in a way all of them accept. These are plain
string checks: they need no toolchain and run everywhere.
"""

from decompnet.app.engine import BASE_HEADER

HEADER = BASE_HEADER.format(title="demo")


def test_64_bit_types_use_int64_on_pre_2003_msvc():
    """Visual C++ before 2003 (`_MSC_VER < 1310`) has `__int64` but not `long long`.

    A bare `long long` there fails with `error C2632: 'long' followed by 'long' is illegal`, and
    because this header is prepended to every compile, that breaks the project outright. Visual
    C++ 4.2 reports `_MSC_VER` 1020, 6.0 reports 1200; both take the `__int64` branch.
    """
    assert "_MSC_VER < 1310" in HEADER
    assert "typedef signed __int64 s64;" in HEADER
    assert "typedef unsigned __int64 u64;" in HEADER


def test_64_bit_types_keep_the_portable_spelling_for_everything_else():
    """`__int64` is MSVC-only, so every other compiler must still see `long long`."""
    assert "typedef signed long long s64;" in HEADER
    assert "typedef unsigned long long u64;" in HEADER


def test_every_long_long_is_behind_the_msvc_guard():
    """Guards the arrangement itself: an unguarded `long long` would reach old MSVC again."""
    depth, guarded = 0, True
    for line in HEADER.splitlines():
        stripped = line.strip()
        if stripped.startswith("#if"):
            depth += 1
        elif stripped.startswith("#endif"):
            depth -= 1
        elif "long long" in stripped and depth == 0:
            guarded = False
    assert guarded, "a `long long` outside the _MSC_VER guard would break pre-2003 MSVC"


def test_guard_is_balanced():
    assert HEADER.count("#if") == HEADER.count("#endif")
