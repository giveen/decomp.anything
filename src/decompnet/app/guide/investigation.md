# Investigating a failure or uncertain finding

Use this when a match stalls, REA evidence conflicts, or a reproducible failure needs an explanation. Routine work
whose next step is already clear can follow its normal workflow. Keep the investigation proportional to the question.

## Establish what is known

State the question and the smallest input or action that exposes it. Record the identity that matters: repository
revision for a source regression; target/version/hash when known, architecture, address and provider for analysis;
compiler and flags for matching. Reuse the claimed dossier, earlier notes and best attempt before collecting more data.

Choose the relevant workflow:

- **Repository bug:** inspect the changed source and callers, find a minimal reproduction, and compare working and
  failing revisions. Use the repository's contribution guidance for validation. REA is useful only when artifact or
  requested runtime evidence helps answer the question.
- **Matching target:** consult the workflow and compiler/platform guides through `guide(topic)`. Separate control-flow,
  operand and layout/register differences; the original compiler and byte-for-byte judge decide whether it matches.
- **Analysis target:** use the analysis and REA guides. Keep observations separate from inferred names or behavior;
  reconstructed source is not verified original source.

For an imported target, call `discover_assistance(query, function)` with the remaining question. Read relevant skills
and use a focused available tool. Discovery normally reuses the current session; choose `connect=true` when a cold
REA import is useful. Use the connected input schema and availability rather than copying an old example's arguments.

## Run a discriminating experiment

List the plausible explanations and choose a narrow probe whose result would support or reject one of them. Identify
the expected distinguishing observation before running it. Change one relevant condition at a time when comparing results.

For example, a missing function might be absent from a partial metadata table rather than absent from the executable.
Compare discovery on the same small fixture with and without that table. For a stale tool form, compare connection
identity, live `tools/list` schema and cached form before and after a controlled server restart.

Prefer an existing REA view, retained evidence or a small synthetic fixture over rescanning the whole target. An
unavailable provider, incomplete output or bounded search is a limitation of the probe, not proof that behavior is absent.
Do not repeat an unchanged rejected call without addressing its cause.

## Leave a usable result

A short note should identify the question, observed evidence, current explanation, probe result and remaining unknowns.
Include relevant addresses and REA evidence IDs, provider, confidence and limitations. Evidence IDs may be session-bound;
retain the material observation and its target identity so the next agent can understand the result after a restart.

For a target work item, save useful findings with `add_note` and the knowledge pass with `record_assistance`. Include
`fallback_reason` only when bounded permutation is justified by the remaining differences. Follow the matching judge's
stop guidance rather than accumulating more guesses. Complete an analysis item when its question is answered; otherwise
release it with the unresolved question and next useful probe.

For a repository fix, preserve the reproduction as a regression test and verify the smallest fix through the affected
boundary. Report what the experiment establishes and which environments or providers were not exercised.

This workflow adapts the evidence-first investigation pattern in Cloudflare's pinned
[debug-plan skill](https://github.com/cloudflare/agents/blob/16b6856070f6c711486011b8b9dd926c2eaae52b/examples/agent-skills/src/skills/debug-plan/SKILL.md)
and [checklist](https://github.com/cloudflare/agents/blob/16b6856070f6c711486011b8b9dd926c2eaae52b/examples/agent-skills/src/skills/debug-plan/references/checklist.md)
to decomp.anything. Those files are references; this guide owns the local workflow.
