# The compiler matrix

Some games were built with more than one compiler: PlayStation games mix PSY-Q GCC 2.7.2 and 2.8.1, library code
uses another assembler profile, and the original's file boundaries decide which globals are read through `$gp`.
A fork's build compiles each file one way, and you can't change that. So a function can have the right C and still
read as a near miss: its file is built with the wrong compiler, or it belongs in another translation unit.

The matrix tests that hypothesis with the same submitted C. A closer result can still contain a source error.

## When to use it

`compile_and_diff` names observed patterns and shows example rows. Each pattern suggests an experiment, not a cause:

- **GP-relative addressing**: `lw v0, %gp_rel(sym)(gp)` on one side, `lui` + `%lo(sym)` on the other. Whether the
  file defines the global (small data) or declares it `extern` decides that.
- **HI/LO address splitting**: the same address built with another `%hi`/`%lo` split (`lui at`, the `%lo` on the
  load or on an `addiu`).
- **Delay slot scheduling**: a branch's delay slot holds another instruction, or a nop.
- **Load delay handling**: a nop after a load on one side only.
- **Additional NOPs** elsewhere.
- **Register allocation** and **instruction order** alone: a compiler version does that, and so does the C's shape
  (a temporary, the order of declarations). Try the matrix once before a long series of rewrites.

Different symbols, addends, operations, constants and calls remain unexplained code differences. Changed branch
destinations remain visible even when one side has extra instructions; a length change does not prove identical
control flow. Registers, scheduling and `$gp` patterns can also result from incorrect C or declarations.

When every difference is compiler-like and the miss is close, decomp.anything runs the matrix itself and adds the
result to the answer.

## What it does

`compiler_matrix(function)` compiles the function's C (the definition in its file, or the `source` you send) under
every **profile** the project declares (compiler version, flags, assembler profile) and every **context**:

| context | the translation unit |
| --- | --- |
| `file` | the source file as it is (what `compile_and_diff` builds) |
| `alone` | a dedicated file: the function alone, every other function only a prototype |
| `neighbors` | the function with the one before and the one after it |
| `extern` | alone, and the globals the file defines are declared `extern` instead |
| `tentative` | alone, and the file's `extern` globals are defined in the file instead |

Each object is diffed against the original. The table shows differing instruction rows, with separate byte,
constant and reference evidence. A zero row count or 100% similarity is insufficient: MATCH requires identical
function bytes outside relocated fields, matching relocation identities, and no constant or reference mismatches.
A jump to a place inside the function is compared by where it lands, since the original's object and the compiler
name such places differently (`L8001A22C`, `LM12`); a reference to anything outside the function has to name the
same symbol with the same addend. Unavailable verification is shown as `verify`, never MATCH. This verifies
function objects; the complete linked image still needs the project's normal verification.

Some valid variants remain unverified here, because only the link can prove them: a field reached from its
struct's symbol (`D_80085428+10` for the original's `D_80085432`) and a switch's jump table. In a PlayStation
project whose build is byte-identical, that was 23 of 1,420 matched functions. When such a function shows `verify`
under its file's own profile, `compile_and_diff` still judges it by the project's rules.

- **A match under the file's own profile, in `file`**: recompiled with the normal build and byte-checked again
  before committing. If that build disagrees, nothing is saved or committed.
- **A match anywhere else** ("MATRIX MATCH"): the source and verified recipe are saved on the function, without
  changing the source tree. The maintainer must verify the normal build before integrating it. Preserve the recipe,
  `release_function` with that note, and claim the next one.
- **No match**: the best cell, remaining patterns and example rows are shown. When the current profile in `file`
  was measured, the report quantifies improvement against it: for example, 100 differing rows reduced to 10 is
  90% fewer differing rows. This is not a 90% probability that the compiler caused the differences. Without that
  baseline, no reduction percentage is inferred. Inspect remaining constants, calls and code even after a large gain.

## Learning from other functions

The first run without history measures the requested matrix. Later functions first try the profile/context pair
with the most verified matches from other functions in the same source file and unit. Ten distinct functions
matching GCC 2.7.2 / `alone` give that pair ten votes. Repeating one function does not add votes, and the function
being tested cannot vote for itself. A failed or unverified latest experiment replaces an earlier success.

Votes are scoped to the project, source path, unit, matrix configuration and original object hash. Changed profiles
or flags, another target, other files and old records without byte evidence do not supply votes. History persists
across app restarts. It determines search order only; the new function must pass its own byte check.

The preferred pair is built together with every profile on the file as it is (`file`): a match the file's own
build makes has to be seen, or an ordinary match would be saved as one that needs the build changed. If any of them
verifies a match, the remaining builds are skipped and the report says how many were avoided. If none compiles or
verifies, every remaining requested pair is tested. `exhaustive=true` always measures the
complete requested matrix. Profile/context filters still apply. Build adapters must accept jobs containing a subset
of the declared pairs; the normal fallback is a second job for the remainder.

`experiment_history(function)` lists every run: source revision, profile and flags, context, differing
instructions, patterns with example rows, byte hashes, verified status, learning evidence and baseline improvement.
Repeated identical inputs are answered from the latest recorded run; `again=true` recompiles. The cache includes
the original object and verification version. Changes outside these inputs (headers, compiler binaries or build
scripts) require `again=true`. Historical recipes without byte evidence remain available for a new attempt.

## For the project's maintainer: declaring a matrix

In the project's `.decomp.wtf.json`:

```json
"matrix": {
  "build": ["./run.sh", "python3", "tools/matrix_build.py", "{job}"],
  "profiles": [
    {"name": "2.7.2-psx", "compiler": "GCC 2.7.2 PSX", "flags": "-O2 -G8"},
    {"name": "2.7.2-cdk", "compiler": "GCC 2.7.2 CDK", "flags": "-O2 -G8"},
    {"name": "2.8.1-psx", "compiler": "GCC 2.8.1 PSX", "flags": "-O2 -G8"}
  ],
  "contexts": ["file", "alone", "neighbors", "extern", "tentative"],
  "auto": true
}
```

`build` runs once per batch (at most two batches per matrix), in the project folder, with `{job}` replaced by a JSON file:

```json
{"function": "func_80012345", "symbol": "func_80012345", "unit": "main/game", "source_path": "src/main/game.c",
 "result": ".decomp-wtf/matrix/<run>/result.json",
 "builds": [{"id": "b0", "profile": "2.7.2-psx", "context": "file",
             "source": ".decomp-wtf/matrix/<run>/file.c", "object": ".decomp-wtf/matrix/<run>/b0.o"}]}
```

For each build, compile `source` the way `profile` says and write the object to `object`. The sources are copies
outside the tree, so add the include paths `source_path`'s folder would have had. Optionally write `result`:

```json
{"current": "2.7.2-psx", "builds": {"b0": {"ok": true}, "b1": {"ok": false, "log": "error: ..."}}}
```

`current` is the profile the project's own build uses for `source_path`: with it, the matrix knows which matches
are ordinary ones (committed) and which need the build changed (saved). Without `result`, a build whose object is
missing counts as failed.

Saved matches are listed by `GET /api/matrix?slug=<project>` (`saved`: function, unit, profile, context, source), for
the script that moves them into the build.
