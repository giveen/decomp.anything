# The compiler matrix

Some games were built with more than one compiler: PlayStation games mix PSY-Q GCC 2.7.2 and 2.8.1, library code
uses another assembler profile, and the original's file boundaries decide which globals are read through `$gp`.
A fork's build compiles each file one way, and you can't change that. So a function can have the right C and still
read as a near miss: its file is built with the wrong compiler, or it belongs in another translation unit.

The matrix tests that instead of you rewriting C that is right.

## When to use it

`compile_and_diff` names the kinds of difference a miss has left. These are a compiler's, not your code's:

- **GP-relative addressing**: `lw v0, %gp_rel(sym)(gp)` on one side, `lui` + `%lo(sym)` on the other. Whether the
  file defines the global (small data) or declares it `extern` decides that.
- **HI/LO address splitting**: the same address built with another `%hi`/`%lo` split (`lui at`, the `%lo` on the
  load or on an `addiu`).
- **Delay slot scheduling**: a branch's delay slot holds another instruction, or a nop.
- **Load delay handling**: a nop after a load on one side only.
- **Additional NOPs** elsewhere.
- **Register allocation** and **instruction order** alone: a compiler version does that, and so does the C's shape
  (a temporary, the order of declarations). Try the matrix once before a long series of rewrites.

Different operations, constants or missing calls are not on this list: that is the C. Fix it first.

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

Each object is diffed against the original. The answer is a table: profiles down, contexts across, the number of
differing instructions in each cell. Nothing in the source tree or the build changes.

- **A match under the file's own profile, in `file`**: an ordinary match, committed like any other.
- **A match anywhere else** ("MATRIX MATCH"): your C is right. The fix is in the build (the file's compiler, or the
  function in a file of its own), which only the project's maintainer can make. The source and its recipe are saved
  on the function. `release_function` with that note, and claim the next one. Do not keep rewriting it.
- **No match**: the best cell and its kinds of difference are shown. If a cell is much closer than the file's own,
  say so in your notes: it tells the next agent which compiler the function was probably built with.

`experiment_history(function)` lists every run: source revision, profile and flags, context, differing
instructions, kinds of difference, match or not. A source that was already tried is answered from that record, so
asking twice costs nothing, and trying compilers by hand is never needed.

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

`build` runs once per matrix, in the project folder, with `{job}` replaced by the path of a JSON file:

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
