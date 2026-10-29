# Knowledge before trial and error

Start each work item with `discover_assistance(query, function)`: ask what you need to learn, not just the function's
name. It returns relevant bundled skills, SKILL.md files in the user's shared reference folders, previous project
knowledge, and tools from the actual REA session. `read_skill(id)` loads the selected instructions for every agent type.
The user's instructions and project boundaries take precedence over skill suggestions.

Read existing notes, the intake report, the claimed dossier and matched similar code before inventing a solution.
State the remaining question, then choose the narrowest useful tool. Call REA only with its advertised inputSchema;
tool names in a skill may describe a different server version. Availability that REA did not report is unknown.
When explanations conflict or a match stalls without a useful next probe, `guide("investigation")` gives a focused
way to distinguish hypotheses and leave a reproducible finding.

Discovery reuses an existing session by default. When REA would answer a useful question, repeat with `connect=true`
to open the project's target. It may require a cold import. An unavailable provider is a limitation to record;
continue useful supported work. Do not repeat rejected or unchanged calls without resolving their cause.

Keep evidence IDs, provider, confidence and limitations in findings. Separate observations from hypotheses.
Prefer retained evidence and focused `inspect_analysis_view` follow-ups to repeating a large analysis. Partial or
truncated output is not proof of complete coverage. `rea_tool` remains restricted to approved read-only operations.

After the knowledge pass, call `record_assistance(function, summary)` with what you read, checked and learned.
This becomes a note for the next agent. More than two compile attempts require a current knowledge review.
When only code layout/register differences remain, include `fallback_reason` explaining why bounded permutation
is appropriate; the permuter only considers functions with that recorded reason. Refresh the review when compiler,
target, project instructions or shared context change. A match still requires the normal byte-for-byte judge.
