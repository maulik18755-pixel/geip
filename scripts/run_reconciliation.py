#!/usr/bin/env python3
"""Cross-source reconciliation: OWID spine vs EIA and Ember challengers.

Loads facts from each connector, runs ReconciliationEngine.compare(), then
prints summary() and the top 20 flagged discrepancies sorted by |pct_delta|.

Connectors included in this run:
  OWID              — always (local CSV, no key required)
  EIA International — historical annual electricity + primary energy
  EIA STEO          — monthly primary-energy series; loaded but expect zero
                      overlap with the current OWID spine (annual electricity
                      only) — included to make missing-key failures visible
  Ember             — annual electricity generation + capacity

EIA IEO is excluded: it emits only is_projection=True records, which the
engine always filters out before comparison.

Environment variables:
  EIA_API_KEY   — https://www.eia.gov/opendata/register.php
  EMBER_API_KEY — https://ember-climate.org/data-catalogue/

If a key is absent the connector is skipped with a warning; OWID-only runs
will report zero overlaps.

Usage:
  python scripts/run_reconciliation.py
  python scripts/run_reconciliation.py --threshold 10 --since 2015
  python scripts/run_reconciliation.py --no-eia
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

# Runnable from project root or scripts/ subdirectory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geip.analytics.engine import DiscrepancyRecord, ReconciliationEngine
from geip.connectors.owid import OWIDConnector
from geip.core.schema import FactRecord

_ROOT     = Path(__file__).resolve().parents[1]
_OWID_CSV = _ROOT / "data" / "owid-energy-data.csv"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_owid() -> list[FactRecord]:
    if not _OWID_CSV.exists():
        sys.exit(f"Fatal: OWID CSV not found at {_OWID_CSV}")
    t0 = time.perf_counter()
    print("  OWID spine             ", end="", flush=True)
    conn = OWIDConnector(_OWID_CSV)
    raw = conn.fetch(since=None)
    facts = conn.normalize(raw)
    report = conn.validate(facts)
    dt = time.perf_counter() - t0
    warn = f"  [{report.n_errors} validation errors]" if not report.ok else ""
    print(f"{len(facts):>8,} facts   {dt:5.1f}s{warn}")
    return facts


def _try_load(label: str, loader_fn) -> list[FactRecord]:
    """Call loader_fn(); catch ValueError (missing key) and any network error."""
    print(f"  {label:<22} ", end="", flush=True)
    t0 = time.perf_counter()
    try:
        facts = loader_fn()
        dt = time.perf_counter() - t0
        print(f"{len(facts):>8,} facts   {dt:5.1f}s")
        return facts
    except ValueError as exc:
        print(f"{'SKIPPED':>14}         ({exc})")
        return []
    except Exception as exc:
        print(f"{'ERROR':>14}         ({exc})", file=sys.stderr)
        return []


def _load_eia_international(since: Optional[date]) -> list[FactRecord]:
    from geip.connectors.eia import EIAInternationalConnector
    conn = EIAInternationalConnector()
    raw  = conn.fetch(since=since)
    return conn.normalize(raw)


def _load_eia_steo(since: Optional[date]) -> list[FactRecord]:
    from geip.connectors.eia import EIASTEOConnector
    conn = EIASTEOConnector()
    raw  = conn.fetch(since=since)
    return conn.normalize(raw)


def _load_ember(since: Optional[date]) -> list[FactRecord]:
    from geip.connectors.ember import EmberConnector
    conn = EmberConnector()
    raw  = conn.fetch(since=since)
    return conn.normalize(raw)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_SEP = "─"

def _print_summary(engine: ReconciliationEngine, log: list[DiscrepancyRecord]) -> None:
    s = engine.summary(log)
    n_total = s["total"]
    print()
    print("Summary")
    print(_SEP * 60)
    print(f"  Total overlapping series : {n_total:,}")
    print(f"  Flagged (|Δ| > {engine.threshold_pct:.1f}%)    : {s['flagged']:,}  ({s['flag_rate_pct']:.1f}%)")
    if engine.min_abs_twh > 0:
        floor_pct = s["below_floor"] / n_total * 100 if n_total else 0.0
        print(
            f"  Below floor (< {engine.min_abs_twh:.1f} TWh)  : "
            f"{s['below_floor']:,}  ({floor_pct:.1f}%)"
        )
    if s["by_source"]:
        print()
        print(f"  {'Challenger source':<26}  {'Compared':>9}  {'Flagged':>8}  {'Suppressed':>10}  {'Flag %':>7}")
        print(f"  {'─'*26}  {'─'*9}  {'─'*8}  {'─'*10}  {'─'*7}")
        for src, counts in s["by_source"].items():
            rate = counts["flagged"] / counts["total"] * 100 if counts["total"] else 0.0
            print(
                f"  {src:<26}  {counts['total']:>9,}  "
                f"{counts['flagged']:>8,}  {counts['below_floor']:>10,}  {rate:>6.1f}%"
            )


def _print_top_flagged(
    flagged: list[DiscrepancyRecord], top_n: int = 20
) -> None:
    ranked = sorted(flagged, key=lambda d: abs(d.pct_delta or 0.0), reverse=True)[:top_n]

    print()
    if not ranked:
        print("No flagged discrepancies — all overlapping series are within threshold.")
        return

    overflow = len(flagged) - len(ranked)
    title = f"Top {len(ranked)} flagged discrepancies  (sorted by |Δ%|)"
    if overflow > 0:
        title += f"  [{overflow:,} more not shown]"
    print(title)
    print(_SEP * len(title))
    print()

    # Column widths
    GEO_W   = 18
    ETYPE_W = 15
    SRC_W   = 18

    hdr = (
        f"{'#':>3}  "
        f"{'Source':<{SRC_W}}  "
        f"{'Geography':<{GEO_W}}  "
        f"{'Energy type':<{ETYPE_W}}  "
        f"{'Family':<13}  "
        f"{'Year':>4}  "
        f"{'Spine':>10}  "
        f"{'Challenger':>10}  "
        f"{'Δ%':>8}  "
        f"{'Spine vint':>10}  "
        f"{'Chal vint':>10}"
    )
    print(hdr)
    print(_SEP * len(hdr))

    for i, d in enumerate(ranked, 1):
        sign    = "+" if (d.pct_delta or 0) > 0 else ""
        pct_str = f"{sign}{d.pct_delta:.1f}%" if d.pct_delta is not None else "n/a"
        geo     = d.geography[:GEO_W]
        print(
            f"{i:>3}  "
            f"{d.challenger_source_id:<{SRC_W}}  "
            f"{geo:<{GEO_W}}  "
            f"{d.energy_type.value:<{ETYPE_W}}  "
            f"{d.metric_family.value:<13}  "
            f"{d.period.year:>4}  "
            f"{d.spine_value:>10,.1f}  "
            f"{d.challenger_value:>10,.1f}  "
            f"{pct_str:>8}  "
            f"{d.spine_vintage.strftime('%Y-%m'):>10}  "
            f"{d.challenger_vintage.strftime('%Y-%m'):>10}"
        )


def _print_geography_note(log: list[DiscrepancyRecord]) -> None:
    """Show how many unique geographies actually matched across sources."""
    geos = {d.geography for d in log}
    print()
    print(
        f"Note: {len(geos)} unique geograph{'y' if len(geos)==1 else 'ies'} matched across sources "
        f"({', '.join(sorted(geos)[:5])}{'…' if len(geos)>5 else ''})."
    )
    print(
        "      Geography name normalisation is Phase 2. Many potential overlaps are not yet "
        "compared because source names differ (e.g. OWID 'United States' vs EIA 'United States')."
    )


# ---------------------------------------------------------------------------
# Diagnostic helpers
# ---------------------------------------------------------------------------

def _median_pct(records: list[DiscrepancyRecord]) -> Optional[float]:
    vals = [d.pct_delta for d in records if d.pct_delta is not None]
    return statistics.median(vals) if vals else None


def _direction(records: list[DiscrepancyRecord]) -> str:
    """Characterise the sign of pct_deltas: are challengers consistently high/low?"""
    vals = [d.pct_delta for d in records if d.pct_delta is not None]
    if not vals:
        return "—"
    pct_pos = sum(1 for v in vals if v > 0) / len(vals) * 100
    if pct_pos >= 80:
        return f"↑ ({pct_pos:.0f}% +ve)"
    if pct_pos <= 20:
        return f"↓ ({100 - pct_pos:.0f}% −ve)"
    return f"↕ mixed ({pct_pos:.0f}% +ve)"


def _diag_geo_coverage(
    spine_facts: list[FactRecord],
    challenger_facts: list[FactRecord],
) -> tuple[set[str], set[str]]:
    """Print geography-coverage table; return (owid_geos, all_challenger_geos)."""
    owid_geos: set[str] = {f.geography for f in spine_facts}
    by_source: dict[str, set[str]] = defaultdict(set)
    for f in challenger_facts:
        by_source[f.source_id].add(f.geography)

    all_chal_geos: set[str] = set().union(*by_source.values()) if by_source else set()
    owid_covered = owid_geos & all_chal_geos

    print()
    print("Diagnostic 1 — Geography coverage")
    print(_SEP * 60)
    W = 26
    print(f"  {'Source':<{W}}  {'Unique geos':>12}  {'Match OWID':>12}  {'Match %':>8}")
    print(f"  {'─'*W}  {'─'*12}  {'─'*12}  {'─'*8}")
    print(f"  {'OWID (spine)':<{W}}  {len(owid_geos):>12,}  {'—':>12}  {'—':>8}")
    for src, geos in sorted(by_source.items()):
        n_match = len(geos & owid_geos)
        pct     = n_match / len(owid_geos) * 100 if owid_geos else 0.0
        print(f"  {src:<{W}}  {len(geos):>12,}  {n_match:>12,}  {pct:>7.1f}%")
    cov_pct = len(owid_covered) / len(owid_geos) * 100 if owid_geos else 0.0
    print(f"  {'All challengers combined':<{W}}  {len(all_chal_geos):>12,}  "
          f"{len(owid_covered):>12,}  {cov_pct:>7.1f}%")

    return owid_geos, all_chal_geos


def _diag_unmatched_geos(
    owid_geos: set[str],
    all_challenger_geos: set[str],
) -> None:
    """Print OWID geographies that have no exact-string match in any challenger."""
    unmatched = sorted(owid_geos - all_challenger_geos)

    print()
    print(
        f"Diagnostic 2 — OWID geographies with no challenger match  "
        f"({len(unmatched)} of {len(owid_geos)})"
    )
    print(_SEP * 60)

    if not unmatched:
        print("  All OWID geographies have at least one challenger match.")
        return

    # Wrap into rows of up to 3 names, left-aligned, so the list is scannable.
    COL_W = 28
    COLS  = 3
    for row_start in range(0, len(unmatched), COLS):
        row = unmatched[row_start : row_start + COLS]
        print("  " + "".join(f"{name:<{COL_W}}" for name in row))

    print()
    print(
        "  Many of these are regional aggregates (e.g. 'Africa', 'G20',\n"
        "  'High-income countries') that country-level sources don't emit.\n"
        "  Country-level mismatches indicate Phase 2 geography-normalisation gaps."
    )


def _diag_flag_breakdown(
    log: list[DiscrepancyRecord],
    flagged: list[DiscrepancyRecord],
) -> None:
    """Print flag rates grouped by energy_type and by geography, then a diagnosis."""
    if not log:
        print()
        print("Diagnostic 3 — Flag breakdown: no comparisons to analyse.")
        return

    n_flagged = len(flagged)
    n_total   = len(log)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _breakdown_table(
        title: str,
        key_fn,
        key_label: str,
        key_width: int,
        max_rows: int = 20,
    ) -> list[tuple]:
        """Group log and flagged by key_fn; return rows sorted by flag count."""
        all_by_key: dict = defaultdict(list)
        for d in log:
            all_by_key[key_fn(d)].append(d)
        flag_by_key: dict = defaultdict(list)
        for d in flagged:
            flag_by_key[key_fn(d)].append(d)

        rows = []
        for key, all_records in all_by_key.items():
            fl = flag_by_key.get(key, [])
            rows.append((key, len(all_records), fl))
        rows.sort(key=lambda r: len(r[2]), reverse=True)

        print()
        overflow = max(0, len(rows) - max_rows)
        hdr_note = f"  (top {max_rows} by flag count; {overflow} more not shown)" if overflow else ""
        print(f"{title}{hdr_note}")
        print(_SEP * 70)
        KW = key_width
        print(
            f"  {key_label:<{KW}}  {'Compared':>9}  {'Flagged':>8}  "
            f"{'Flag%':>6}  {'Median Δ%':>10}  Direction"
        )
        print(f"  {'─'*KW}  {'─'*9}  {'─'*8}  {'─'*6}  {'─'*10}  {'─'*22}")
        for key, n_all, fl in rows[:max_rows]:
            flag_pct  = len(fl) / n_all * 100 if n_all else 0.0
            med       = _median_pct(fl)
            med_str   = f"{med:+.1f}%" if med is not None else "—"
            dirn      = _direction(fl) if fl else "—"
            print(
                f"  {str(key):<{KW}}  {n_all:>9,}  {len(fl):>8,}  "
                f"{flag_pct:>5.1f}%  {med_str:>10}  {dirn}"
            )
        return rows

    # ── energy_type breakdown ─────────────────────────────────────────────────
    print()
    print(
        f"Diagnostic 3 — Flag breakdown  "
        f"({n_flagged:,} flagged / {n_total:,} compared)"
    )
    etype_rows = _breakdown_table(
        "  By energy type",
        lambda d: d.energy_type.value,
        "Energy type",
        key_width=17,
    )

    # ── geography breakdown ───────────────────────────────────────────────────
    geo_rows = _breakdown_table(
        "  By geography  (top 20 by flag count)",
        lambda d: d.geography,
        "Geography",
        key_width=22,
        max_rows=20,
    )

    # ── clustering diagnosis ──────────────────────────────────────────────────
    print()
    print("  Clustering diagnosis")
    print("  " + _SEP * 56)

    # Top energy type
    if etype_rows:
        top_etype, _, top_etype_fl = etype_rows[0]
        pct_of_flags = len(top_etype_fl) / n_flagged * 100 if n_flagged else 0.0
        print(
            f"  • Highest-flagged energy type : {top_etype}  "
            f"({len(top_etype_fl)}/{n_flagged} = {pct_of_flags:.1f}% of all flags)"
        )
        all_for_etype = next(n for k, n, _ in etype_rows if k == top_etype)
        etype_flag_rate = len(top_etype_fl) / all_for_etype * 100 if all_for_etype else 0.0
        print(f"    Flag rate for this type: {etype_flag_rate:.1f}% of its comparisons")

    # Top geography
    if geo_rows:
        top_geo, _, top_geo_fl = geo_rows[0]
        pct_of_flags = len(top_geo_fl) / n_flagged * 100 if n_flagged else 0.0
        print(
            f"  • Highest-flagged geography   : {top_geo}  "
            f"({len(top_geo_fl)}/{n_flagged} = {pct_of_flags:.1f}% of all flags)"
        )

    # Global sign skew across all flagged records
    all_pct_vals = [d.pct_delta for d in flagged if d.pct_delta is not None]
    if all_pct_vals:
        n_pos     = sum(1 for v in all_pct_vals if v > 0)
        pct_pos   = n_pos / len(all_pct_vals) * 100
        direction = "HIGHER" if pct_pos >= 50 else "LOWER"
        print(
            f"  • Sign skew : {pct_pos:.1f}% of flagged deltas are positive — challengers "
            f"tend to report {direction} than OWID."
        )
        if pct_pos >= 70 or pct_pos <= 30:
            print(
                f"    → Strong directional skew (>{max(pct_pos, 100-pct_pos):.0f}% one-sided) "
                f"is characteristic of a methodology or unit difference, not random error."
            )
        else:
            print(
                "    → No strong directional skew; disagreements scatter both ways — "
                "consistent with genuine data differences across countries/years."
            )


def _print_diagnostics(
    spine_facts: list[FactRecord],
    challenger_facts: list[FactRecord],
    log: list[DiscrepancyRecord],
    flagged: list[DiscrepancyRecord],
) -> None:
    print()
    print("DIAGNOSTIC MODE")
    print("=" * 60)
    owid_geos, all_chal_geos = _diag_geo_coverage(spine_facts, challenger_facts)
    _diag_unmatched_geos(owid_geos, all_chal_geos)
    _diag_flag_breakdown(log, flagged)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--threshold", type=float, default=5.0,
        metavar="PCT",
        help="Flag threshold in %%  (default: 5.0)",
    )
    parser.add_argument(
        "--since", type=int, default=None,
        metavar="YEAR",
        help="Only fetch data from YEAR onward (reduces API volume)",
    )
    parser.add_argument("--no-eia",   action="store_true", help="Skip all EIA connectors")
    parser.add_argument("--no-ember", action="store_true", help="Skip Ember connector")
    parser.add_argument(
        "--diag", action="store_true",
        help=(
            "Print diagnostic report: geography coverage, unmatched OWID geographies, "
            "and flag breakdown by energy_type and geography with clustering diagnosis."
        ),
    )
    args = parser.parse_args()

    since: Optional[date] = date(args.since, 1, 1) if args.since else None

    header = "GEIP Cross-Source Reconciliation"
    print(header)
    print("=" * len(header))
    if since:
        print(f"  Fetching data from {args.since} onward")
    print(f"  Threshold: ±{args.threshold}%")
    print()

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    print("Loading facts")
    print(_SEP * 60)

    spine_facts: list[FactRecord] = _load_owid()

    challenger_facts: list[FactRecord] = []

    if not args.no_eia:
        challenger_facts += _try_load(
            "EIA International",
            lambda: _load_eia_international(since),
        )
        challenger_facts += _try_load(
            "EIA STEO",
            lambda: _load_eia_steo(since),
        )
        # IEO emits only projections; engine filters them — skip to save API quota.
        print(f"  {'EIA IEO':<22} {'SKIPPED':>14}         (all projections, engine filters them)")

    if not args.no_ember:
        challenger_facts += _try_load(
            "Ember",
            lambda: _load_ember(since),
        )

    if not challenger_facts:
        print()
        print(
            "No challenger facts loaded.\n"
            "Set EIA_API_KEY and/or EMBER_API_KEY to compare live data,\n"
            "or pass --no-eia / --no-ember to suppress this warning."
        )
        return

    # ------------------------------------------------------------------
    # Reconcile
    # ------------------------------------------------------------------
    print()
    print(
        f"Reconciling  "
        f"(spine {len(spine_facts):,} facts  ×  challengers {len(challenger_facts):,} facts) …",
        flush=True,
    )
    t0 = time.perf_counter()
    engine = ReconciliationEngine(threshold_pct=args.threshold)
    log    = engine.compare(spine_facts=spine_facts, challenger_facts=challenger_facts)
    dt     = time.perf_counter() - t0
    print(f"  {len(log):,} overlapping series found in {dt:.2f}s")

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    flagged = engine.flagged(log)
    _print_summary(engine, log)
    _print_top_flagged(flagged)
    if log and not args.diag:
        _print_geography_note(log)
    if args.diag:
        _print_diagnostics(spine_facts, challenger_facts, log, flagged)
    print()


if __name__ == "__main__":
    main()
