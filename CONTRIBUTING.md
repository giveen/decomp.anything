# Contributing

Human and agent-authored pull requests are welcome. Keep the change focused on the requested behavior, inspect the
current implementation and callers, and reuse existing infrastructure before adding another abstraction or tool.

## Set up

Use the Python version required by `pyproject.toml` and the locked development dependencies:

```sh
uv sync --locked --group dev
```

See [Development](README.md#development) for the application entry point. CI's authoritative setup and checks live
in [.github/workflows/ci.yml](.github/workflows/ci.yml); the commands below are local starting points.

## Investigate before fixing

Use the [investigation guide](src/decompnet/app/guide/investigation.md) when a failure or an uncertain explanation
needs evidence. Identify the candidate revision and smallest triggering input. Separate observations from hypotheses,
then choose a probe that would distinguish the plausible causes.

For a bug fix, add a regression that exercises the failing behavior through the relevant boundary. When practical,
run the same test against the pre-fix revision and confirm that it fails for the reported reason. Use an isolated
checkout or temporary fixture; preserve the user's current work. Avoid tests that merely assert implementation text.

Prefer small synthetic fixtures over private binaries or large datasets. For example, lower a SQLite connection's
variable limit instead of inserting a production-sized function table; restore the limit after the test. A parser
fixture should reach the public reader when the bug affects imports, not only the internal unpacking helper.

## Validate the changed behavior

Start with the smallest checks that cover the change and its direct consumers. Broaden when shared behavior changes
or failures leave a question unresolved. Documentation-only edits need link/command review, not a full application
suite. Changes that register or load guides also need the affected discovery and resource checks.

| Change | Relevant local checks |
| --- | --- |
| Python code or tests | `uv run --no-sync ruff check .` and the affected pytest modules |
| PE/ELF discovery or boundaries | `uv run --no-sync pytest -q tests/test_pe.py tests/test_pe_fpo.py tests/test_pe_architecture.py tests/test_elf.py`; add the affected architecture/debug-information tests |
| REA lifecycle, tools, schemas or agent discovery | `uv run --no-sync pytest -q tests/test_rea.py tests/test_console_reconnect.py tests/test_assistance.py tests/test_bridge.py tests/test_agents.py` |
| Browser console behavior | `node --check src/decompnet/app/static/console.js` and `uv run --no-sync pytest -q tests/test_console_reconnect.py`; check the actual view for visual changes |
| Other browser JavaScript | `node --check src/decompnet/app/static/app.js` and tests for the affected API/UI behavior |
| Fork persistence or sync | `uv run --no-sync pytest -q tests/test_fork_sync.py tests/test_forks.py`; include judge/link/room tests when those consumers change |
| App/REA updates or tool installation | `uv run --no-sync pytest -q tests/test_updates.py tests/test_rea_updates.py tests/test_toolchains.py`; use temporary installations and fake installers |
| Guide registration or packaged resources | `uv run --no-sync pytest -q tests/test_assistance.py tests/test_agents.py`; check the built distribution when its resources change |
| CI, packaging or host integration | Run the corresponding job's commands from `.github/workflows/ci.yml`; that file owns versions, setup and the Windows test selection |

For a full local run, use `uv run --no-sync pytest -ra`. Some compiler and game-fixture tests skip without their
dependencies. Record the skip reasons and the coverage they leave untested. The Node browser-logic check also skips
when Node.js is absent. A skipped or unavailable check does not establish that the behavior passed.

REA/MCP lifecycle changes should exercise a real client/server transport with a controlled fake server. Cover the
changed handshake, catalog, error, shutdown or reconnect boundary rather than replacing all protocol calls with mocks.
Changes to shared tool behavior should check its MCP and built-in API consumers. Diagnostics must not pollute stdio
protocol frames.

Compiler integration tests use bubblewrap on Linux. If the execution environment prevents namespace creation, report
the environment failure and use an authorized environment that supports the test; do not weaken the application sandbox
to turn the test green. The CI workflow documents its host setup.

## Submit and update a PR

- Work on a focused branch and preserve unrelated changes. Agent-created branches and PRs are welcome for requested work.
- Describe the concrete problem and resulting behavior. Include the reproduction or regression evidence, validation
  commands/results, and material limitations or skipped coverage. Say when a reported failure was environmental.
- Distinguish synthetic fixtures, fake MCP servers and real-provider checks. Do not imply that a fake-server test
  validated Ghidra, IDA, Hopper or a proprietary compiler.
- Push a coherent, reviewable change. Add follow-up commits for review feedback rather than force-pushing away the
  history reviewers used. Check that CI results apply to the current head.
- Keep review comments focused on reproducible bugs and actionable follow-ups. Track a pre-existing unrelated bug
  separately instead of expanding a small fix without reason.
- Merge only when requested and the required checks pass. `CI passed` is the aggregate check; use the workflow and
  GitHub's current branch rules for the full merge requirements.

## Reference patterns

These instructions are written for this repository. The following pinned examples informed their organization and
workflow; their project-specific commands and policies are reference material, not additional repository rules:

- [nuqs: task-to-guide routing](https://github.com/47ng/nuqs/blob/fb0835c7ba0033d85a08f0c88822cda346ab546d/AGENTS.md#L62-L69).
- [Hermes: choose an existing capability before adding core surface](https://github.com/NousResearch/hermes-agent/blob/d10bb2ab6fb9e16028070bf7075284a7b0476872/AGENTS.md#L132-L151).
- [Effect: validation by change type](https://github.com/Effect-TS/effect/blob/078cdbf6a37d209819ad504274fe72e1080573e0/.agents/AGENTS.md#L17-L26).
- [Anarlog: targeted interface QA and stdio MCP checks](https://github.com/fastrepl/anarlog/blob/143e3d8caa0ebf5c0769af7b566a7bd615d889f2/.agents/skills/qa-cli-mcp-api/SKILL.md).
- [Deno: preserve incremental review history](https://github.com/denoland/deno/blob/c439c90d890ad07c2bf4eaa8582cf7389142f97e/CLAUDE.md#L35-L38).

If a future contribution copies source text or resources, preserve the applicable license and attribution. The linked
examples here supply ideas; this change does not vendor their files.
