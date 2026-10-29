---
name: investigate
description: Investigate a stalled decomp.anything match, conflicting REA evidence, or a target-specific failure with a focused experiment. Use when the next useful probe is unclear; routine matching and analysis use the decompile skill.
---

# Investigate a decomp.anything work item

Work within the user's selected project. Use the decompile skill for setup or importing a new target; an existing
project's authorization and instructions still apply. This skill does not require installing another analysis tool.

Read `guide("investigation")` from the decomp MCP server for the maintained investigation workflow. This bundled guide
is also discoverable through `discover_assistance` and `read_skill("investigation")`, including for built-in API agents.
Load the relevant matching, compiler or analysis guide only when the question calls for it.

Use the existing dossier and findings to state the unresolved question. Select a narrow probe that can distinguish
the plausible explanations, preserve its evidence and limitations, and record what the result changes. Keep unknowns
explicit. The canonical guide describes the tool sequence and when to finish or release the work item.

If decomp tools are unavailable, report that limitation and continue with evidence the user already supplied; do not
invent a schema or claim a target was analyzed. For a bug in this source repository, use its `CONTRIBUTING.md` and
investigation guide directly.
