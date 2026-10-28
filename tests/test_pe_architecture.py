"""Unsupported Windows architectures must reach analysis without producing x86 work items."""

import pytest

from decompnet.app.server import _analyze_instead
from decompnet.rom import detect, pe
from decompnet.rom.n64 import RomError

from .pe_fixtures import arm_image


@pytest.mark.parametrize("machine", [0x01C4, 0xAA64])
def test_arm_pe_is_routed_to_analysis(machine, monkeypatch):
    data = arm_image(machine)

    def decode(*args, **kwargs):
        pytest.fail("ARM bytes reached the x86 decoder")

    monkeypatch.setattr(pe, "_md", decode)
    with pytest.raises(pe.PeError, match="isn't for x86/x86-64 matching"):
        pe.read(data)
    with pytest.raises(RomError, match="isn't for x86/x86-64 matching") as exc:
        detect.identify(data, "arm.exe")
    assert _analyze_instead(exc.value)
