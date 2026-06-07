#!/usr/bin/env python3
"""Build a local Parquet cache of all connector facts for dashboard use.

Pulls facts from OWID (local CSV), EIA International, EIA STEO, EIA IEO, and
Ember, then writes the combined set to data/demo_facts.parquet. The dashboard
loads from this file rather than hitting live APIs on every run.

Connectors that require API keys (EIA, Ember) are skipped with a warning when
the key is absent — the cache will still be written with whichever sources did
load.

Usage:
    python scripts/build_demo_cache.py

Environment variables:
    EIA_API_KEY   — https://www.eia.gov/opendata/register.php
    EMBER_API_KEY — https://ember-energy.org/data/api/
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geip.connectors.owid import OWIDConnector
from geip.core.schema import FactRecord

_ROOT     = Path(__file__).resolve().parents[1]
_OWID_CSV = _ROOT / "data" / "owid-energy-data.csv"
_OUT      = _ROOT / "data" / "demo_facts.parquet"

_SEP = "─"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_owid() -> list[FactRecord]:
    if not _OWID_CSV.exists():
        sys.exit(f"Fatal: OWID CSV not found at {_OWID_CSV}")
    t0 = time.perf_counter()
    print("  owid_energy            ", end="", flush=True)
    conn = OWIDConnector(_OWID_CSV)
    facts = conn.normalize(conn.fetch(since=None))
    print(f"{len(facts):>8,} facts   {time.perf_counter() - t0:5.1f}s")
    return facts


def _try_load(label: str, loader_fn) -> list[FactRecord]:
    print(f"  {label:<22} ", end="", flush=True)
    t0 = time.perf_counter()
    try:
        facts = loader_fn()
        print(f"{len(facts):>8,} facts   {time.perf_counter() - t0:5.1f}s")
        return facts
    except ValueError as exc:
        print(f"{'SKIPPED':>14}         ({exc})")
        return []
    except Exception as exc:
        print(f"{'ERROR':>14}         ({exc})", file=sys.stderr)
        return []


def _load_eia_international() -> list[FactRecord]:
    from geip.connectors.eia import EIAInternationalConnector
    conn = EIAInternationalConnector()
    return conn.normalize(conn.fetch(since=None))


def _load_eia_steo() -> list[FactRecord]:
    from geip.connectors.eia import EIASTEOConnector
    conn = EIASTEOConnector()
    return conn.normalize(conn.fetch(since=None))


def _load_eia_ieo() -> list[FactRecord]:
    from geip.connectors.eia import EIAIEOConnector
    conn = EIAIEOConnector()
    return conn.normalize(conn.fetch(since=None))


def _load_ember() -> list[FactRecord]:
    from geip.connectors.ember import EmberConnector
    conn = EmberConnector()
    return conn.normalize(conn.fetch(since=None))


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _to_dataframe(facts: list[FactRecord]) -> pd.DataFrame:
    """Convert FactRecords to a DataFrame with plain Python types throughout.

    Enum values are stored as their string values (e.g. "coal", "electricity")
    so the Parquet file has no dependency on the geip.core.schema module.
    """
    rows = [
        {
            "source_id":     f.source_id,
            "geography":     f.geography,
            "energy_type":   f.energy_type.value,
            "metric":        f.metric,
            "metric_family": f.metric_family.value,
            "period":        f.period,
            "period_type":   f.period_type,
            "value":         f.value,
            "unit":          f.unit,
            "vintage":       f.vintage,
            "pull_ts":       f.pull_ts,
            "is_projection": f.is_projection,
            "scenario":      f.scenario,
        }
        for f in facts
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("GEIP demo cache builder")
    print(_SEP * 60)
    print("Loading facts")
    print(_SEP * 60)

    all_facts: list[FactRecord] = []
    all_facts.extend(_load_owid())
    all_facts.extend(_try_load("eia_international",  _load_eia_international))
    all_facts.extend(_try_load("eia_steo",           _load_eia_steo))
    all_facts.extend(_try_load("eia_ieo",            _load_eia_ieo))
    all_facts.extend(_try_load("ember",              _load_ember))

    if not all_facts:
        sys.exit("No facts loaded — nothing to write.")

    print()
    print(f"Serialising {len(all_facts):,} facts ...", end="", flush=True)
    t0 = time.perf_counter()
    df = _to_dataframe(all_facts)
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_OUT, index=False)
    dt = time.perf_counter() - t0
    print(f"  {dt:.1f}s")
    print(f"Wrote → {_OUT}")

    # ------------------------------------------------------------------
    # Coverage summary
    # ------------------------------------------------------------------
    years = pd.to_datetime(df["period"]).dt.year
    sources = sorted(df["source_id"].unique())
    energy_types = sorted(df["energy_type"].unique())
    families = sorted(df["metric_family"].unique())

    print()
    print("Coverage")
    print(_SEP * 60)
    print(f"  Rows            : {len(df):,}")
    print(f"  Years           : {years.min()}–{years.max()}")
    print(f"  Sources         : {', '.join(sources)}")
    print(f"  Energy types    : {', '.join(energy_types)}")
    print(f"  Metric families : {', '.join(families)}")
    print(f"  Geographies     : {df['geography'].nunique():,}")

    print()
    print(f"  {'Source':<26}  {'Rows':>8}  {'Historical':>10}  {'Projections':>11}")
    print(f"  {'─'*26}  {'─'*8}  {'─'*10}  {'─'*11}")
    for src in sources:
        sub = df[df["source_id"] == src]
        n_hist = int((~sub["is_projection"]).sum())
        n_proj = int(sub["is_projection"].sum())
        print(f"  {src:<26}  {len(sub):>8,}  {n_hist:>10,}  {n_proj:>11,}")


if __name__ == "__main__":
    main()
