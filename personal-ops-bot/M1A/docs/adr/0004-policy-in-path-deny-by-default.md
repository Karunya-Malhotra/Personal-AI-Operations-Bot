# ADR 0004 — The policy engine is in the execution path and denies by default

Status: accepted (v0.3 correction #3, v0.3.1)

## Context
v0.2 proposed a policy call site in M1C that returned ALLOW for everything, with
real rules in M1D. Review correctly rejected a security boundary that defaults
open, even temporarily.

## Decision
From M1C, `decide()` can only reach ALLOW by falling through every check. A tool
with no entry in `TOOL_POLICIES` is denied at runtime, and its registration
fails at boot and in CI.

## Consequence
Four explicit policy declarations must be written in M1C. This is less work than
a permissive stub plus a reminder to remove it.

## M1A note
Not yet implemented — M1A has no tools. `Container.startup()` is the hook the
boot-time symmetry check will attach to.
