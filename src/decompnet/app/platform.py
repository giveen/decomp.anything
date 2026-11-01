"""What decomp.wtf can import, and with which compiler.

One table per question the GUI, the importer and the agent prompt ask: which platforms are ready for a full decomp
and not a toy one (PLATFORM_STATUS), which compilers each platform uses (PLATFORM_COMPILERS, COMPILERS, MW_VERSIONS),
what Setup installs (TOOLS), and the helpers that turn "this program is C++ on x86" into the right driver
(source_language, cxx_toolchain, arch_toolchain). The header a project starts from, and the notes a Windows or Linux
build needs, live next to the compilers they describe.
"""

from __future__ import annotations

from .. import toolchains

DEFAULT_TOOLCHAIN = {"KMC": "gcc-2.7.2-kmc", "IDO": "ido-7.1", "PSYQ": "gcc-2.7.2-psx", "EEGCC": "ee-gcc-2.96",
                     "AGBCC": "agbcc"}

# How ready each platform is for a full decomp you can port and mod; the GUI and the README show it.
# recommended: the most tested path, from import to a project that builds the game (GameCube and Wii: the same kind
# of project as decomp.dev's, linked file by file as it's finished; N64: tested on real games, rebuilt byte for byte).
# beta: works and is tested on homemade games, less so on real ones yet.
PLATFORM_STATUS = {"gc": "recommended", "wii": "recommended", "n64": "recommended",
                   "psx": "beta", "ps2": "beta", "gba": "beta", "nds": "beta", "windows": "beta", "linux": "beta"}

# PC programs: split from the executable itself (pe.py, elfexe.py) and judged by the x86 matcher.
X86_PLATFORMS = ("windows", "linux")

# C++ drivers: the source is compiled as C++ (see toolchains.toml), and the UI and agent prompt say C++.
CXX_TOOLCHAINS = ("gxx-", "clangxx-", "mingwxx-", "clang-clxx-", "msvcxx-")

def source_language(toolchain: str) -> str:
    return "C++" if toolchain.startswith(CXX_TOOLCHAINS) else "C"

def source_ext(toolchain: str) -> str:
    return ".cpp" if source_language(toolchain) == "C++" else ".c"

def cxx_toolchain(toolchain: str) -> str:
    """The C++ driver for a C toolchain on the same target, when there is one."""
    for c, cpp in (("mingw-", "mingwxx-"), ("clang-cl-", "clang-clxx-"), ("msvc-", "msvcxx-"),
                   ("gcc-linux-", "gxx-linux-"), ("clang-linux-", "clangxx-linux-")):
        if toolchain.startswith(c):
            return cpp + toolchain[len(c):]
    return toolchain

def arch_toolchain(toolchain: str, arch: str) -> str:
    """The same PC compiler for the target's architecture (a 32-bit import must not compile x86-64)."""
    suffix = "x86_64" if arch == "x86_64" else "x86"
    for old in ("x86_64", "x86"):
        if toolchain.endswith("-" + old):
            candidate = toolchain[: -len(old)] + suffix
            try:
                toolchains.get(candidate)
            except toolchains.ToolchainError:
                return toolchain
            return candidate
    return toolchain

LINUX_COMPILERS = {
    "gcc-linux-x86_64": ("GCC (x86-64)", "Linux programs (x86-64), with the GCC on this machine"),
    "gxx-linux-x86_64": ("G++ (x86-64)", "Linux C++ programs (x86-64), with the G++ on this machine"),
    "clang-linux-x86_64": ("Clang (x86-64)", "Linux programs (x86-64), with the Clang on this machine"),
    "clangxx-linux-x86_64": ("Clang++ (x86-64)", "Linux C++ programs (x86-64), with the Clang++ on this machine"),
    "gcc-linux-x86": ("GCC (x86)", "Linux programs (32-bit x86), with the GCC on this machine"),
    "gxx-linux-x86": ("G++ (x86)", "Linux C++ programs (32-bit x86), with the G++ on this machine"),
    "clang-linux-x86": ("Clang (x86)", "Linux programs (32-bit x86), with the Clang on this machine"),
    "clangxx-linux-x86": ("Clang++ (x86)", "Linux C++ programs (32-bit x86), with the Clang++ on this machine"),
}

COMPILERS = {
    **{k: use for k, (_short, use) in LINUX_COMPILERS.items()},
    "mingw-x86_64": "Windows games (x86-64), with the GCC on this machine",
    "mingw-x86": "Windows games (32-bit x86), with the GCC on this machine",
    "mingwxx-x86_64": "Windows C++ games (x86-64), with the G++ on this machine",
    "mingwxx-x86": "Windows C++ games (32-bit x86), with the G++ on this machine",
    "clang-cl-x86_64": "Windows games (x86-64), with LLVM's MSVC-compatible clang-cl",
    "clang-cl-x86": "Windows games (32-bit x86), with LLVM's MSVC-compatible clang-cl",
    "clang-clxx-x86_64": "Windows C++ games (x86-64), with LLVM's MSVC-compatible clang-cl",
    "clang-clxx-x86": "Windows C++ games (32-bit x86), with LLVM's MSVC-compatible clang-cl",
    "msvc-x86_64": "Windows games (x86-64), with the cl.exe on this machine",
    "msvc-x86": "Windows games (32-bit x86), with the cl.exe on this machine",
    "msvcxx-x86_64": "Windows C++ games (x86-64), with the cl.exe on this machine",
    "msvcxx-x86": "Windows C++ games (32-bit x86), with the cl.exe on this machine",
    "gcc-2.7.2-kmc": "many third-party N64 games",
    "ido-7.1": "most later first-party N64 games",
    "ido-5.3": "early N64 games (1996-97)",
    "gcc-2.7.2-psx": "PlayStation games (PSY-Q era GCC)",
    "gcc-2.8.1-psx": "later PlayStation games",
    "ee-gcc-2.96": "PlayStation 2 games (Sony's EE GCC)",
    "agbcc": "Game Boy Advance games (pret's agbcc, GCC 2.95)",
}

SHORT_NAMES = {"gcc-2.7.2-kmc": "KMC GCC 2.7.2", "ido-7.1": "IDO 7.1", "ido-5.3": "IDO 5.3",
               "gcc-2.7.2-psx": "GCC 2.7.2 (PS1)", "gcc-2.8.1-psx": "GCC 2.8.1 (PS1)", "ee-gcc-2.96": "EE-GCC 2.96",
               "agbcc": "agbcc", "mingw-x86_64": "MinGW-w64 GCC (x86-64)", "mingw-x86": "MinGW-w64 GCC (x86)",
               "mingwxx-x86_64": "MinGW-w64 G++ (x86-64)", "mingwxx-x86": "MinGW-w64 G++ (x86)",
               "clang-cl-x86_64": "clang-cl (x86-64)", "clang-cl-x86": "clang-cl (x86)",
               "clang-clxx-x86_64": "clang-cl (x86-64, C++)", "clang-clxx-x86": "clang-cl (x86, C++)",
               "msvc-x86_64": "MSVC cl (x86-64)", "msvc-x86": "MSVC cl (x86)",
               "msvcxx-x86_64": "MSVC cl (x86-64, C++)", "msvcxx-x86": "MSVC cl (x86, C++)",
               "llvm-tools": "LLVM tools", "upx": "UPX", "rizin": "Rizin", "detect-it-easy": "Detect It Easy",
               **{k: short for k, (short, _use) in LINUX_COMPILERS.items()}}

# CodeWarrior versions in the `mwcc` pack (GameCube, Wii) and the `mwccarm` pack (DS), for games started from scratch.
MW_VERSIONS = {
    "gc": ["GC/1.0", "GC/1.1", "GC/1.1p1", "GC/1.2.5", "GC/1.2.5n", "GC/1.3", "GC/1.3.2", "GC/1.3.2r", "GC/2.0",
           "GC/2.0p1", "GC/2.5", "GC/2.6", "GC/2.7", "GC/3.0a3", "GC/3.0a3.2", "GC/3.0a3.3", "GC/3.0a3.4", "GC/3.0a5",
           "GC/3.0a5.2"],
    "wii": ["Wii/0x4201_127", "Wii/1.0RC1", "Wii/1.0a", "Wii/1.0", "Wii/1.1", "Wii/1.3", "Wii/1.5", "Wii/1.6", "Wii/1.7"],
    "ds": ["DS/1.2/base", "DS/1.2/sp2", "DS/1.2/sp2p3", "DS/1.2/sp3", "DS/1.2/sp4", "DS/2.0/base", "DS/2.0/sp1",
           "DS/2.0/sp1p2", "DS/2.0/sp1p5", "DS/2.0/sp1p6", "DS/2.0/sp1p7", "DS/2.0/sp2", "DS/2.0/sp2p2", "DS/2.0/sp2p3",
           "DS/2.0/sp2p4", "DS/dsi/1.1", "DS/dsi/1.1p1", "DS/dsi/1.2", "DS/dsi/1.2p1", "DS/dsi/1.2p2", "DS/dsi/1.3",
           "DS/dsi/1.3p1", "DS/dsi/1.6sp1", "DS/dsi/1.6sp2"],
}

# Tools shown in Setup besides the compilers.
TOOLS = {
    "m2c": "machine-made first drafts (MIPS, PowerPC, ARM)",
    "objdiff-cli": "judges forks of public decomps, function by function",
    "ghidra": "drafts for PC, Switch and other platforms m2c can't read (540 MB, needs Java 21+)",
    "rea": "the RE console and the agents' inspect_binary (needs Node.js; native code needs Ghidra, Hopper or IDA)",
    "mwcc": "CodeWarrior, the compilers of GameCube and Wii games (proprietary)",
    "mwccarm": "CodeWarrior for DS, the compilers of DS games (proprietary)",
    "dsd": "ds-decomp: analyses DS ROMs and rebuilds them",
    "llvm-tools": "PDB debug symbols for Windows programs (system LLVM; optional)",
    "upx": "unpack UPX-packed PC programs before splitting (optional)",
    "rizin": "lightweight analysis provider for REA (optional)",
    "detect-it-easy": "compiler and packer fingerprints (optional)",
}

WINDOWS_NOTE = """
/* Windows globals: define them here WITHOUT `extern` (for example `int g_counter;`). With `extern`, MinGW
 * assumes the name may come from a DLL and loads it through an extra pointer, which never matches the
 * game's direct RIP-relative access. Imported functions are the exception: declare those
 * `__declspec(dllimport)`, which is exactly the indirect call through the import table the game makes. */
"""

LINUX_NOTE = """
/* Linux: calls into shared libraries (memcpy, printf, operator new) go through the program's PLT stubs, which
 * are named after the function: declare them yourself (system headers can't be included). Globals can be
 * `extern` or defined here. With a C++ compiler the names to match are the mangled ones (_ZN7Archive4OpenEv):
 * write the C++ that mangles to them, or wrap a sub_<address> you write in `extern "C" { ... }`. */
"""

# Functions this small are handed out in batches (claim_function count): agents match most on the first try.
BATCH_INSNS = 64

PLATFORM_COMPILERS = {"n64": ["gcc-2.7.2-kmc", "ido-7.1", "ido-5.3"], "psx": ["gcc-2.7.2-psx", "gcc-2.8.1-psx"],
                      "ps2": ["ee-gcc-2.96"], "gba": ["agbcc"],
                      "windows": ["mingw-x86_64", "mingw-x86", "mingwxx-x86_64", "mingwxx-x86",
                                  "clang-cl-x86_64", "clang-cl-x86", "clang-clxx-x86_64", "clang-clxx-x86",
                                  "msvc-x86_64", "msvc-x86", "msvcxx-x86_64", "msvcxx-x86"],
                      "linux": list(LINUX_COMPILERS)}

def _linux_flags(flags: list[str], pie: bool) -> list[str]:
    """A Linux toolchain's flags for this program: position-independent programs are compiled -fPIE."""
    return [f for f in flags if f not in ("-fno-pic", "-fno-pie")] + ["-fPIE"] if pie else list(flags)

GP_NOTE = """
/* This game keeps small globals (8 bytes or less) in $gp-relative small data. Declare those here WITHOUT
 * `extern` (for example `s32 D_8001B278;`): then the compiler reads them through $gp, like the game does.
 * With `extern`, it builds the full address instead and the function can't match. */
"""

BASE_HEADER = """/* context.h for {title}: shared by every function. Agents grow it with edit_context:
 * add a prototype or struct the moment you work one out, so the next function benefits. */

typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
#if defined(_MSC_VER) && _MSC_VER < 1310  /* Visual C++ before 2003 has __int64, not long long */
typedef signed __int64 s64;
typedef unsigned __int64 u64;
#else
typedef signed long long s64;
typedef unsigned long long u64;
#endif
typedef float f32;
typedef double f64;
typedef volatile u8 vu8;
typedef volatile u16 vu16;
typedef volatile u32 vu32;
typedef volatile s32 vs32;
typedef int s32_bool;

#define NULL ((void *)0)
#define TRUE 1
#define FALSE 0
#define ARRAY_COUNT(arr) (s32)(sizeof(arr) / sizeof(arr[0]))
"""
