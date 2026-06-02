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
import sys
import time
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
    print()
    print("Summary")
    print(_SEP * 60)
    print(f"  Total overlapping series : {s['total']:,}")
    print(f"  Flagged (|Δ| > {engine.threshold_pct:.1f}%)    : {s['flagged']:,}  ({s['flag_rate_pct']:.1f}%)")
    if s["by_source"]:
        print()
        print(f"  {'Challenger source':<26}  {'Compared':>9}  {'Flagged':>8}  {'Flag %':>7}")
        print(f"  {'─'*26}  {'─'*9}  {'─'*8}  {'─'*7}")
        for src, counts in s["by_source"].items():
            rate = counts["flagged"] / counts["total"] * 100 if counts["total"] else 0.0
            print(
                f"  {src:<26}  {counts['total']:>9,}  "
                f"{counts['flagged']:>8,}  {rate:>6.1f}%"
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
    _print_summary(engine, log)
    _print_top_flagged(engine.flagged(log))
    if log:
        _print_geography_note(log)
    print()


if __name__ == "__main__":
    main()
