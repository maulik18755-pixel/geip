# Global Energy Intelligence Platform (GEIP) — Build Specification

**Purpose:** Decision-support tool for energy investment and strategy. Tracks current
status, recent trends, and forward 5-year projections across every major energy source
(oil, gas, coal, hydro, solar, wind, nuclear, and other renewables) at global and
major-region granularity, built entirely on free, open, API-accessible data.

**Status:** Specification v1.0 — ready for phased build in Claude Code.

---

## 1. Scope and Design Principles

### 1.1 What this tool is
A multi-source ingestion, harmonization, and analytics platform that:
- Pulls from free public energy data APIs on each source's natural update cadence.
- Harmonizes everything into a single common schema (one fact table, shared dimensions).
- Reconciles overlapping sources against a designated canonical spine.
- Surfaces, per energy source: current production/capacity, demand/consumption, prices,
  emissions intensity, project/investment pipeline, and a forward 5-year projection.
- Delivers via an API-first backend plus an interactive dashboard.

### 1.2 What this tool is NOT (explicit non-goals)
- **Not real-time.** No source in this domain pushes data in real time except live market
  price feeds (which are commercial). "Auto-update the moment the source updates" is
  implemented as **per-source scheduled polling** that detects and ingests new data within
  hours of publication. Do not build or promise sub-minute push.
- **Not a substitute for paid IEA/Wood Mackenzie/Rystad data.** IEA flagship datasets
  (World Energy Outlook, World Energy Balances) are paywalled and not freely API-streamable.
  GEIP uses free equivalents and clearly labels projection provenance.
- **Not a financial-advice engine.** It surfaces data and projections; it does not issue
  buy/sell recommendations.

### 1.3 Core design principles (carried from prior project discipline)
1. **Canonical spine first.** Designate Our World in Data's harmonized energy dataset as the
   reconciliation anchor (the "regression spine" equivalent). All other sources are
   reconciled *against* it; discrepancies are logged, never silently overwritten.
2. **Vintage-aware, never destructive.** Every data point carries a `vintage` (the source's
   publication/release date). New vintages are appended; old ones are retained for audit and
   revision tracking. Never overwrite a value in place.
3. **Unit discipline.** Every value carries an explicit unit. No cross-source arithmetic
   happens before unit normalization. (See failure modes in `CLAUDE.md`.)
4. **Primary energy vs. electricity are different quantities.** Never sum them. Track them
   in separate metric families.
5. **Provenance on every number.** Every value is traceable to source, dataset, vintage, and
   pull timestamp. The UI can always answer "where did this number come from?"

---

## 2. Data Source Registry

All sources below are free and either API-accessible or programmatically downloadable.
Each connector implements: `fetch()`, `normalize()`, `validate()`, and declares its
`cadence` and `license`.

| Source | Coverage | Access | Cadence | License | Role |
|--------|----------|--------|---------|---------|------|
| **EIA Open Data API v2** | US + international; oil, gas, coal, electricity by fuel, STEO + IEO projections | REST API, free key | Petroleum weekly (Wed); STEO monthly; IEO annual | US Gov public domain | Primary US + projections |
| **Ember** | 215 countries; power-sector generation, capacity, demand, CO2 | REST API + CSV | Twice monthly | CC BY 4.0 | Global power/renewables |
| **Our World in Data (owid/energy-data)** | 200+ geographies; harmonized generation, capacity, consumption, emissions | Versioned GitHub repo (CSV) | Irregular (on source refresh) | CC BY | **Canonical spine** |
| **Energy Institute Statistical Review** | Global; oil/gas/coal reserves, production, consumption, primary energy | CSV/Excel download | Annual (mid-year) | Open w/ attribution | Authoritative annual fossil + primary |
| **ENTSO-E Transparency** | Europe; near-real-time grid generation/load | REST API, free key | Sub-hourly | Open w/ attribution | Optional high-resolution EU grid |
| **IRENA** | Global; renewable capacity statistics | Download / query tool | Annual | Open w/ attribution | Renewable capacity detail |
| **World Bank "Pink Sheet" / IMF** | Global commodity benchmark prices (oil, coal, gas) | CSV / API | Monthly | Open | Prices |

**Connector contract (every source must implement):**
```
class SourceConnector(Protocol):
    source_id: str
    cadence: Cadence            # poll schedule
    license: str
    def fetch(self, since: Optional[date]) -> RawPayload: ...
    def normalize(self, raw: RawPayload) -> list[FactRecord]: ...
    def validate(self, facts: list[FactRecord]) -> ValidationReport: ...
```

---

## 3. Data Model (Star Schema)

**Fact table: `energy_fact`**
| Column | Type | Notes |
|--------|------|-------|
| `source_id` | str | FK -> dim_source |
| `geography_id` | str | FK -> dim_geography (country/region/world) |
| `energy_type` | str | FK -> dim_energy_type (oil, gas, coal, hydro, solar, wind, nuclear, other) |
| `metric` | str | FK -> dim_metric (production, capacity, consumption, price, emissions, etc.) |
| `metric_family` | str | primary_energy \| electricity \| price \| emissions \| pipeline — never mix in aggregation |
| `period` | date | observation period |
| `period_type` | str | hourly \| monthly \| yearly |
| `value` | float | |
| `unit` | str | canonical unit per metric_family |
| `vintage` | date | source publication/release date |
| `pull_ts` | timestamp | when GEIP ingested it |
| `is_projection` | bool | true for STEO/IEO/scenario data |
| `scenario` | str | null for historical; scenario label for projections |

**Dimensions:** `dim_source`, `dim_geography` (with region rollups), `dim_energy_type`,
`dim_metric`, `dim_scenario`.

**Freshness manifest: `source_freshness`** — one row per source: last successful pull,
latest vintage seen, next scheduled poll, last status.

---

## 4. Architecture (Three Layers)

### Layer 1 — Ingestion & Harmonization
- Per-source connectors (Section 2 contract).
- **Scheduler:** APScheduler or Prefect; each source on its own cadence. On each run:
  fetch since last vintage → normalize → validate → upsert by (source, geography,
  energy_type, metric, period, vintage) → update freshness manifest.
- **Reconciliation engine:** after ingest, compare overlapping series against the OWID
  spine. Emit a `discrepancy_log` (source, series, % delta, flag) when divergence exceeds a
  configurable threshold. Never auto-correct; surface for review.
- **Storage:** start with DuckDB or SQLite + Parquet (zero infra, fast analytics).
  Migrate to Postgres only if multi-user write becomes necessary.

### Layer 2 — Analytics
- **Current status** snapshot per energy_type × geography (latest non-projection vintage).
- **Trend** decomposition (YoY, CAGR, share-of-mix shifts).
- **5-year projection** assembled from EIA STEO (short term) + IEO (long term) projection
  series, with scenario bands where the source provides them. Clearly flagged
  `is_projection=true`.
- **Uncertainty bands** on projections (range across available scenarios; optional simple
  stochastic envelope). Apply prior stochastic-optimization experience here.
- **Cross-source consistency report** built from the discrepancy_log.

### Layer 3 — Delivery
- **FastAPI** backend: `/status`, `/trend`, `/projection`, `/freshness`, `/provenance`,
  `/discrepancies`. API-first so the data is reusable beyond the dashboard.
- **Streamlit** dashboard: one panel per energy source (current / demand / price /
  emissions / pipeline / 5-yr projection), a global energy-mix overview, a freshness
  indicator, and a provenance drill-down on every number.

**Stack:** Python 3.13, FastAPI, Streamlit, DuckDB (start), Prefect or APScheduler,
pandas/polars, httpx. Matches the existing project stack.

---

## 5. Phased Build Plan

**Phase 0 — Spine + skeleton (week 1)**
- Repo scaffold, `CLAUDE.md` guard file, schema, DuckDB store.
- Ingest the OWID harmonized dataset as the canonical spine. Lock a small hand-verified
  slice (e.g. global electricity mix for one recent year) as a regression test.

**Phase 1 — Core connectors (weeks 2–3)**
- EIA (oil, gas, coal, electricity, STEO, IEO) and Ember connectors.
- Reconciliation engine + discrepancy_log against the spine.
- Scheduler with per-source cadences; freshness manifest.

**Phase 2 — Breadth (weeks 4–5)**
- Energy Institute Statistical Review, IRENA, World Bank prices connectors.
- Full metric coverage: capacity, consumption, price, emissions, pipeline.

**Phase 3 — Analytics (weeks 6–7)**
- Trend decomposition, 5-year projection assembly, uncertainty bands.
- Cross-source consistency report.

**Phase 4 — Delivery (weeks 8–9)**
- FastAPI endpoints + Streamlit dashboard with provenance drill-down.

**Phase 5 — Hardening (week 10)**
- Revision tracking (how vintages change a series over time), alerting on new vintages,
  full regression suite.

---

## 6. Testing Methodology
- **Regression spine:** hand-verified slice of the OWID dataset locked as ground truth;
  any pipeline change that perturbs it fails CI.
- **Unit tests** per connector: `normalize()` output schema + unit correctness.
- **Reconciliation tests:** known-divergent series must raise a discrepancy flag.
- **Projection tests:** projection series must carry `is_projection=true` and a scenario
  label; never appear in historical aggregates.
- **No-mixing invariant test:** assert primary_energy and electricity metric_families are
  never summed together anywhere in the analytics layer.

---

## 7. Risk Register
| Risk | Mitigation |
|------|------------|
| Treating polling as real-time | Document cadence per source; UI shows last-update + next-poll, never claims live |
| Unit mismatch across sources | Canonical unit per metric_family; normalize before any arithmetic |
| Double-counting primary energy vs electricity | Separate metric_family; invariant test forbids summation |
| Silent overwrite on source revision | Vintage-aware append-only store; revision tracking |
| IEA paywall assumption | Free-source-only registry; projection provenance labeled |
| Source API/schema drift | Connector validate() + schema tests catch breaking changes early |
| Aggregate-region double counting (country + region both summed) | Geography rollup rules enforced in dim_geography |

---

## 8. CLAUDE.md Guard File (to ship in repo root)
Enforce on AI-assisted coding agents:
1. Never invent a data value or a source URL. If a series is missing, log it; do not fill.
2. Never sum primary-energy and electricity metric families.
3. Never overwrite an existing (source, series, period, vintage) value — append new vintages.
4. Never reconcile by silently editing the spine; emit a discrepancy flag instead.
5. Never hardcode country/energy-type lists — derive from dimension tables.
6. Never relabel a projection as historical; preserve is_projection and scenario.
7. Never present a number without traceable provenance (source, vintage, pull_ts).
8. Never claim real-time/live updating in code comments, API, or UI copy.
