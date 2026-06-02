# GEIP — Global Energy Intelligence Platform

Decision-support tool tracking the global energy landscape across every major
source (oil, gas, coal, hydro, solar, wind, nuclear, other renewables) at global
and major-region granularity, built entirely on free, open, API-accessible data.

See `spec/GLOBAL_ENERGY_PLATFORM.md` for the full specification and
`CLAUDE.md` for the rules any AI coding agent must follow.

## What's here (Phase 0 — spine + skeleton)

```
geip/
  core/schema.py          # FactRecord + dimensions + canonical units + reconciliation rules
  connectors/base.py      # SourceConnector protocol every source implements
  connectors/owid.py      # OWID connector — the canonical reconciliation spine
  analytics/reconcile.py  # correct (non-double-counting) electricity-mix summation
data/
  owid-energy-data.csv    # pulled OWID harmonized dataset (the spine source)
  owid-energy-codebook.csv
tests/
  test_spine.py                       # locked regression spine
  spine_world_electricity_2024.json   # hand-verified World 2024 ground truth
```

## Run the spine test

```bash
cd geip
pip install -e ".[dev]"
pytest -v
```

The spine locks the World 2024 electricity mix (reconciles to the reported
total within 0.5 TWh) and guards against the biofuel double-count.

## Next phases

1. EIA + Ember connectors; reconciliation engine + discrepancy log.
2. Energy Institute, IRENA, World Bank prices; full metric coverage.
3. Trend decomposition, 5-year projections (EIA STEO/IEO), uncertainty bands.
4. FastAPI endpoints + Streamlit dashboard with provenance drill-down.
5. Revision tracking, new-vintage alerting, hardening.
