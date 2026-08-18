# ADR 0001 — Python is the current implementation language

Status: accepted (v0.2 D1, amended)

## Context
The workload is dominated by libraries that are Python-first: LLM SDKs,
faster-whisper, Playwright, OCR, PDF parsing, embeddings, ffmpeg wrappers.

## Decision
Python 3.12 for all current components.

## Consequence
Weaker typing than TypeScript, mitigated by mypy (strict on `app.core`).

## Note
This is not a permanent lock. The rationale is workload fit, and workloads
change. The process and HTTP boundaries in the design would allow a future
component in another language if evidence justified it.
