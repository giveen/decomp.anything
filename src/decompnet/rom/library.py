"""Functions that are not the program's own code: CRT start-up, compiler runtime and static libraries.

Marking them saves agents from "decompiling" _start or operator new, and keeps a project's progress
about the code someone actually wrote. The names come from symbol tables, PDBs and import tables;
auto-generated names (sub_...) are never classified on their own.
"""

from __future__ import annotations

import re

from .symbols import AUTO_NAME

CRT = {
    "_start", "start", "entry", "mainCRTStartup", "WinMainCRTStartup", "wWinMainCRTStartup",
    "_init", "_fini", "__main", "_mainCRTStartup", "__libc_csu_init", "__libc_csu_fini",
    "deregister_tm_clones", "register_tm_clones", "__do_global_dtors_aux", "frame_dummy",
    "_dl_relocate_static_pie", "__x86.get_pc_thunk.bx", "__gmon_start__",
}
CRT_PREFIXES = (
    "__scrt_", "__mingw_", "_GLOBAL__sub_I", "__gxx_personality", "__cxa_", "_Unwind_", "__unwind_",
    "__stack_chk_", "__security_", "_guard_", "__chkstk", "___chkstk", "__libc_", "_IO_", "__printf",
    "__fprintf", "__sprintf", "__snprintf", "__mem", "__str", "__errno", "_errno", "__assert",
    "std::", "operator new", "operator delete", "_ZSt", "_ZNSt", "_ZTV", "_ZTI", "_ZTS",
    "??2@", "??3@", "??_", ".?AV", "__RTDynamicCast", "__RTCastToVoid",
)
CRT_CONTAINS = ("crt", "mingw", "libgcc", "libstdc++", "libc++", "msvcp", "ucrt", "vcruntime")
# Statically linked C library functions. GCC/MinGW keeps their real names; without this an agent would
# "decompile" malloc, memcpy and the printf family as if the game shipped them.
LIBC = {
    "malloc", "calloc", "realloc", "free", "memcpy", "memmove", "memset", "memcmp", "memchr",
    "strlen", "strcmp", "strncmp", "strcpy", "strncpy", "strcat", "strncat", "strstr", "strchr",
    "strrchr", "strdup", "strerror", "strtok", "strspn", "strcspn", "strpbrk", "strtol", "strtoul",
    "strtod", "atoi", "atol", "atof", "qsort", "bsearch", "abs", "labs",
    "printf", "fprintf", "sprintf", "snprintf", "vprintf", "vfprintf", "vsprintf", "vsnprintf",
    "puts", "putchar", "fputs", "fputc", "fgets", "fgetc", "fread", "fwrite", "fopen", "fclose",
    "fflush", "fseek", "ftell", "rewind", "feof", "ferror", "perror", "scanf", "sscanf",
    "exit", "abort", "atexit", "getenv", "setenv", "time", "clock", "rand", "srand", "system",
    "toupper", "tolower", "isalpha", "isdigit", "isspace", "isalnum", "isupper", "islower",
}

# Standard Nintendo 64 SDK (Libultra) functions and math/matrix utilities
LIBULTRA = {
    "osCreateThread", "osStartThread", "osStopThread", "osSetThreadPri", "osGetThreadPri",
    "osCreateMesgQueue", "osSendMesg", "osRecvMesg", "osSetEventMesg", "osViGetCurrentFramebuffer",
    "osViSetMode", "osViBlack", "osViSwapBuffer", "osAiSetFrequency", "osAiGetLength",
    "osDpSetNextBuffer", "osSpTaskLoad", "osSpTaskStartGo", "osContInit", "osContStartReadData",
    "osContGetReadData", "osEepromRead", "osEepromWrite", "osPfsInit", "bzero", "bcopy", "bcmp",
    "guMtxIdent", "guMtxIdentF", "guPerspective", "guPerspectiveF", "guLookAt", "guLookAtF",
    "guRotate", "guRotateF", "guScale", "guScaleF", "guTranslate", "guTranslateF",
    "__osSetSR", "__osGetSR", "__osSetFpcCsr", "__osGetFpcCsr", "__osSiRawReadIo", "__osSiRawWriteIo",
    "__osSpRawReadIo", "__osSpRawWriteIo",
}

# PlayStation 1 Psy-Q / SDK functions
PSYQ = {
    "DrawSync", "PutDrawEnv", "PutDispEnv", "SetDefDrawEnv", "SetDefDispEnv", "ClearImage",
    "LoadImage", "StoreImage", "MoveImage", "CdRead", "CdSync", "CdInit", "CdControl",
    "SpuInit", "SpuSetKey", "SpuSetVoiceAttr", "PadInit", "PadRead", "GsInit", "GsSortObject4",
    "SetGeomOffset", "SetGeomScreen", "RotMatrix", "TransMatrix", "ScaleMatrix", "VectorNormal",
    "SquareRoot0", "Square0", "DeliverEvent", "OpenEvent", "EnableEvent", "TestEvent",
    "EnterCriticalSection", "ExitCriticalSection", "ChangeClearPAD",
}

# Game Boy Advance AGB Lib and BIOS / Sound driver functions
AGBLIB = {
    "AgbSysCall", "CpuSet", "CpuFastSet", "Div", "DivArm", "Sqrt", "ArcTan", "ArcTan2",
    "MidiKey2Freq", "BgCnt", "DmaCopy", "SoundBiasSet", "SoundBiasReset", "RegisterRamReset",
    "Halt", "Stop", "IntrWait", "VBlankIntrWait", "SoftReset", "m4aSoundInit", "m4aSoundMain",
    "m4aSongNumStart", "m4aSongNumStop", "MPlayOpen", "MPlayStart",
}

# Compiler runtime helpers (libgcc soft-float and arithmetic)
LIBGCC = {
    "__divsi3", "__modsi3", "__udivsi3", "__umodsi3", "__divdi3", "__moddi3", "__udivdi3", "__umoddi3",
    "__muldi3", "__ashldi3", "__ashrdi3", "__lshrdi3", "__floatdisf", "__floatdidf", "__fixsfdi",
    "__fixdfdi", "__clzsi2", "__ctzsi2",
}

_SDK_PREFIX = re.compile(r"^(?:(?:os|__os|gu|al|n_al|leo|Leo|Gs|Cd|Spu|Pad|Fnt|GPU|m4a|MPlay)[A-Z]|__os_)")


def kind(name: str) -> str | None:
    """'CRT', 'C++ runtime', 'static library', 'SDK library', 'compiler runtime' or None. Auto names are never guessed."""
    if not name or AUTO_NAME.match(name):
        return None
    if name in LIBC:
        return "C runtime"
    if name in LIBGCC:
        return "compiler runtime"
    if name in LIBULTRA or name in PSYQ or name in AGBLIB or _SDK_PREFIX.match(name):
        return "SDK library"
    if name in CRT or name.startswith(("__scrt_", "mainCRTStartup", "WinMainCRTStartup", "wWinMainCRTStartup",
                                        "_GLOBAL__sub_I", "__libc_csu", "_dl_relocate", "__x86.get_pc_thunk",
                                        "deregister_tm_clones", "register_tm_clones", "_pei386",
                                        "__gcc_", "__gxx_", "__gnu_")):
        return "CRT"
    if name.startswith(("??2@", "??3@", "??_", ".?AV", "_ZTV", "_ZTI", "_ZTS", "_ZSt", "_ZNSt",
                        "std::", "operator new", "operator delete", "__cxa_", "__gxx_personality",
                        "_Unwind_", "__RTDynamicCast")):
        return "C++ runtime"
    if name.startswith(CRT_PREFIXES):
        return "CRT"
    if any(x in name.lower() for x in CRT_CONTAINS):
        return "static library"
    return None
