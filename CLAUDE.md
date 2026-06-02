# CLAUDE.md — Operating rules for AI coding agents on GEIP

This file governs any AI agent (Claude Code or otherwise) making changes to this
repository. These rules exist because multi-source energy-data harmonization has
specific, repeatable failure modes. Violating them produces wrong numbers that
look plausible — the most dangerous kind of bug in a decision-support tool.

## Hard rules — never violate

1. **Never invent a data value or a source URL.** If a series is missing, log it
   and leave it missing. Do not interpolate, guess, or fabricate a citation.

2. **Never sum across metric families.** `primary_energy` and `electricity` are
   different physical quantities. The `FactRecord` unit check and the
   `metric_family` enum exist to stop this. Do not bypass them.

3. **Never re-introduce the biofuel double-count.** In OWID,
   `biofuel_electricity` is a SUBSET of `other_renewable_electricity`. The OWID
   connector intentionally does NOT emit biofuel separately. Adding it back
   inflates the electricity total by ~700 TWh globally. `test_biofuel_not_double_counted`
   guards this — if you find yourself making it pass by changing the test, stop.

4. **Never overwrite a value in place.** Storage is vintage-aware and
   append-only. A revised number from a source is a NEW vintage, not an edit.

5. **Never reconcile by editing the spine.** OWID is the canonical anchor. When
   another source disagrees, emit a discrepancy flag. Do not "fix" OWID to match
   another source, and do not silently pick a winner.

6. **Never hardcode country or energy-type lists in business logic.** Derive them
   from the dimension definitions in `geip/core/schema.py`.

7. **Never relabel a projection as historical.** Preserve `is_projection` and
   `scenario`. Projections must never appear inside historical aggregates.

8. **Never present a number without provenance.** Every value traces to
   `source_id`, `vintage`, and `pull_ts`. Keep it that way through every transform.

9. **Never claim real-time / live updating.** This platform polls sources on
   their publication cadence. No source here pushes in real time. Do not write
   "live" / "real-time" in code comments, API responses, or UI copy.

## When you change the spine

The regression spine (`tests/test_spine.py` + `tests/spine_world_electricity_2024.json`)
encodes hand-verified ground truth. If a legitimate data update changes these
numbers:
  - Confirm the change is real (trace it to a source revision, not a code bug).
  - Update the JSON AND document why in the commit message.
  - Never relax `TOL_TWH` to make a failing test pass.

## Stack
Python 3.13 · pandas · pytest · (Phase 1+: DuckDB, FastAPI, Streamlit, Prefect).
Keep connectors conforming to `geip/connectors/base.py::SourceConnector`.
