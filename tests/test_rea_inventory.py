"""What REA adds to a project (analysis.inventory): rebased addresses, no link stubs, nothing already covered.

REA opens a file at its own image base, a project works in the file's own addresses, and link-generated code is not
work. Those three rules are decided here, without REA or a database, so a mistake shows up as a failed assertion rather
than as work items for a PLT slot.
"""

from decompnet.app import analysis

SEGMENTS = [
    {"name": ".text", "start": "0x100000", "end": "0x101000", "executable": True, "readable": True},
    {"name": ".plt", "start": "0x101000", "end": "0x101100", "executable": True, "readable": True},
]


def test_addresses_are_rebased_onto_the_projects_own():
    """Ghidra's 0x100100 is the project's 0x100: the map, the functions and the strings all move together."""
    plan = analysis.inventory(SEGMENTS, [{"name": "main", "address": 0x100100, "size": 0x40}], [], [], set())
    assert plan["base"] == 0x100000
    assert plan["segments"][0]["start"] == 0 and plan["segments"][0]["end"] == 0x1000
    assert plan["functions"] == [{"name": "main", "vram": 0x100, "size": 0x40, "unit": ".text"}]


def test_link_generated_code_is_counted_but_never_offered():
    plan = analysis.inventory(SEGMENTS, [
        {"name": "main", "address": 0x100100, "size": 0x40},
        {"name": "<EXTERNAL>::puts", "address": 0x101040, "size": 0x10},  # a PLT slot
        {"name": "_init", "address": 0x100200, "size": 0x20},             # a link stub by name
        {"name": "__x86.get_pc_thunk.bx", "address": 0x100240, "size": 4},
    ], [], [], set())
    assert [f["name"] for f in plan["functions"]] == ["main"]
    assert plan["counts"]["stubs"] == 3 and plan["counts"]["functions"] == 1


def test_what_a_work_item_already_covers_is_not_added_again():
    plan = analysis.inventory(SEGMENTS, [{"name": "main.cold", "address": 0x100108, "size": 4},
                                         {"name": "helper", "address": 0x100800, "size": 8}],
                              [], [(0x100, 0x40)], set())
    assert [f["name"] for f in plan["functions"]] == ["helper"]
    assert plan["counts"]["covered"] == 1


def test_only_strings_the_project_does_not_have_come_back():
    plan = analysis.inventory(SEGMENTS, [], [("hello", 0x100120), ("world", 0x100180)], [], {0x100120})
    assert plan["strings"] == [{"text": "world", "address": 0x180}]
