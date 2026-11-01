"""Diff -> hypothesis: what a miss probably means in C terms (scoring.hypotheses).

The diff's markers already classify a row (stack offset, operand, regalloc, reorder, different instruction, missing,
extra); these tests pin what each classification is turned into, because a wrong guess costs an agent a compile and a
right one saves several.
"""

from decompnet import scoring


def result(*rows: tuple[str, str, str]) -> scoring.DiffResult:
    return scoring.DiffResult(score=len(rows), max_score=100, rows=list(rows), bytes_match=False)


def text(*rows: tuple[str, str, str]) -> str:
    return "\n".join(scoring.hypotheses(result(*rows)))


def test_a_clean_diff_says_nothing():
    assert scoring.hypotheses(result((" ", "mov eax, 1", "mov eax, 1"))) == []


def test_stack_offsets_point_at_a_struct_or_local_size():
    assert "stack offset" in text(("s", "mov eax, [rbp-0x8]", "mov eax, [rbp-0x10]"))


def test_an_operand_change_points_at_a_constant():
    out = text(("i", "cmp eax, 0x10", "cmp eax, 0x20"))
    assert "constant/offset" in out and "0x10" in out


def test_registers_only_means_a_temporary():
    assert "register" in text(("r", "mov eax, ecx", "mov eax, edx"))


def test_reordered_rows_mean_statement_order():
    assert "another order" in text(("o", "mov eax, 1", "mov ebx, 2"))


def test_a_width_difference_points_at_the_field_type():
    assert "operand width" in text(("|", "movzx eax, byte ptr [rdi]", "mov eax, dword ptr [rdi]"))


def test_lea_versus_add_points_at_an_address():
    assert "lea" in text(("|", "lea rax, [rbx+rcx*4]", "add rax, rcx"))


def test_a_call_on_one_side_means_inlining():
    assert "calls a function" in text(("|", "call helper", "mov eax, 1"))
    assert "inlined" in text(("|", "mov eax, 1", "call helper"))


def test_a_null_test_versus_a_comparison():
    assert "null/zero test" in text(("|", "test rax, rax", "cmp qword ptr [rbp-8], 0"))


def test_length_deltas_say_extra_or_missing_work():
    longer = text(*[(">", "", "nop")] * 4, (" ", "ret", "ret"))
    assert "longer" in longer
    shorter = text(*[("<", "nop", "")] * 4, (" ", "ret", "ret"))
    assert "shorter" in shorter


def test_the_most_actionable_come_first():
    out = scoring.hypotheses(result((">", "", "nop"), ("r", "mov eax, ecx", "mov eax, edx"),
                                    ("s", "mov eax, [rbp-8]", "mov eax, [rbp-16]")))
    assert out[0].startswith("1 instruction(s) differ only by a stack offset")
