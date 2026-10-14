# Aurik Industrial Equipment Monitoring Backend

Production-grade, asynchronous industrial equipment monitoring and telemetry normalization backend built for the **Aurik Tech Lead Assessment**.

Ingests inconsistent multi-vendor telemetry (PulseForge, ThermexWatch, MaintaFlow, and CMMS Work Orders), applies two-layer idempotency, normalizes dialect discrepancies into canonical signals, processes state transitions asynchronously via a PostgreSQL row-locked job queue (`SELECT ... FOR UPDATE SKIP LOCKED`), and serves an explainable, deterministic operational attention view across plants, lines, and machines.

---

## 1. Quickstart (Docker Compose)

Run the full stack (PostgreSQL 16, API gateway on port 8000, and background worker daemon) with a single command:

```bash
# 1. Clone and enter directory
cd aurik_backend_assesment

# 2. Copy environment template
cp .env.example .env

# 3. Spin up database, API, and worker services
docker compose up --build
```

The API will be available at `http://localhost:8000`.  
Interactive Swagger docs: `http://localhost:8000/docs`.  
OpenAPI Specification: `http://localhost:8000/openapi.json`.  
Prometheus Metrics: `http://localhost:8000/metrics`.

To seed reference data and all historical fixture records inside Docker:
```bash
docker compose exec api python -m app.cli seed
```

---

## 2. Local Development Setup

Prerequisites: Python 3.12+ and PostgreSQL 16+.

```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate       # On Linux/macOS
.venv\Scripts\activate          # On Windows

# 2. Install dependencies with development tools
pip install -e ".[dev]"
# Or using uv (recommended):
uv pip install -e ".[dev]"

# 3. Configure environment
cp .env.example .env

# 4. Run database migrations
alembic upgrade head

# 5. Seed reference assets and run historical pipeline
python -m app.cli seed

# 6. Start API server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 7. Start background queue worker in a separate terminal
python -m app.cli worker
```

---

## 3. CLI Management Commands

The CLI (`app.cli`) provides deterministic management commands:

| Command | Description |
|---|---|
| `python -m app.cli seed` | Loads reference tables (`plants.csv`, `lines.csv`, `machines.csv`), ingests all seed CSV datasets and JSON fixtures, drains the queue synchronously, and displays the operational attention view. |
| `python -m app.cli seed --as-of 2026-04-18T09:00:00Z` | Seeds and derives states as of an exact historical point in time. |
| `python -m app.cli worker` | Starts the continuous background queue worker polling with `FOR UPDATE SKIP LOCKED`. |
| `python -m app.cli recompute EQ-001` | Manually triggers status recomputation for an individual machine. |
| `python -m app.cli recompute all` | Recomputes derived operational states across all monitored equipment. |

---

## 4. Test Suite & Verification

The test suite contains **135 automated tests** covering unit normalization, property-based shuffle invariance, idempotency layers, queue leasing/crash-recovery, and full anchor integration:

```bash
# Run all tests (unit + integration)
pytest -v

# Run anchor integration tests verifying EQ-001 through EQ-008
pytest -v tests/integration/test_anchors.py

# Run static type checking across all 46 source files (strict mode)
mypy app/ tests/

# Run code style, format, and linting checks
ruff check .
ruff format --check .
```

---

## 5. System Architecture & Key Concepts

```
[External Vendors: PulseForge | ThermexWatch | MaintaFlow | CMMS]
                       │ (HTTP POST)
                       ▼
          [Layer 1: Ingestion Gateway]
          - Role-based API Key Authentication (HMAC constant-time)
          - Structural Envelope Validation
          - Canonical SHA-256 Hash Idempotency
          - Raw-First Storage (raw_records)
                       │ (Atomic DB Transaction)
                       ▼
          [Asynchronous PostgreSQL Queue]
          - jobs table: SELECT ... FOR UPDATE SKIP LOCKED
          - Crash Recovery via lease timeout (locked_until)
          - Exponential Backoff Retries & Dead-Letter Queue
                       │
                       ▼
          [Background Normalization Worker]
          - Dialect parsing & unit conversion (F -> C, g -> mm/s)
          - Entity resolution & catalog validation
          - Layer 2: Semantic Content Deduplication (DUPLICATE_CONTENT)
          - Normalized signal store (normalized_signals)
                       │ (RECOMPUTE_MACHINE job)
                       ▼
          [Pure Deterministic Status Engine]
          - 100% pure function: no DB I/O, no wall-clock reads
          - Chronological signal timeline evaluation
          - Order-independent (verified via 10-run shuffle property tests)
          - Materialized operational view (machine_state_view)
                       │
                       ▼
          [Serving Layer: REST APIs & Summaries]
          - Machine Operational Views (GET /v1/machines/status)
          - Plant & Line Rollups (GET /v1/plants/{id}/summary)
          - Time-Travel Querying (as_of parameter)
          - Prometheus Text Metrics (GET /metrics)
```

### Two-Layer Idempotency Model
1. **Layer 1 (Ingestion Gateway)**: Evaluates incoming records against `(vendor, record_id)` using a canonical SHA-256 hash that handles JSON key ordering, floating point equivalence (`18 == 18.0`), and null normalization.
   - Same record ID + same hash: **`DUPLICATE`** (no-op, HTTP 202, no new row inserted).
   - Same record ID + new hash: **`REVISION`** (increments revision, marks older records `SUPERSEDED`, enqueues normalization).
   - New record ID: **`ACCEPTED`** (creates raw record, enqueues normalization).
2. **Layer 2 (Worker Semantic Deduplication)**: Detects duplicate readings submitted under synthetic or differing IDs (e.g., `TW-8801` vs `TW-8801-R`).
   - Signals matching `(source_vendor, machine_id, event_time, canonical_event_type)` are marked `is_effective = False` and `duplicate_of = original_id`. The raw record transitions to `DUPLICATE_CONTENT`.

---

## 6. Anchor Machine Status Verification (`2026-04-18T09:00:00Z`)

| Machine ID | Plant | Line | Derived Status | Attention | Needs Attention | Processing Status | Reason Codes & Traceability |
|---|---|---|---|---|---|---|---|
| **EQ-001** | `PLANT_01` | `LINE-A` | **`WATCH`** | 1 | `false` | `FRESH` | Vibration alert cleared by newer recovery signal; minor inspection note remaining (`INSPECTION_MINOR_DEFECT`, `RECOVERING`). |
| **EQ-002** | `PLANT_01` | `LINE-A` | **`CRITICAL`** | 4 | `true` | `PARTIAL` | Severe temperature spike (`96.4°C` / `TEMP_CRIT`) exceeding rated limit (`90.0°C`). High criticality asset. **The only CRITICAL machine in the facility.** |
| **EQ-003** | `PLANT_01` | `LINE-B` | **`HEALTHY`** | 0 | `false` | `STALE` | Nominal recovery signal received; all dimensions normal. |
| **EQ-004** | `PLANT_01` | `LINE-C` | **`HEALTHY`** | 0 | `false` | `FRESH` | Power drop anomaly resolved by closed work order with passed post-maintenance check (`RESOLVED_BY_MAINTENANCE`). |
| **EQ-005** | `PLANT_02` | `LINE-D` | **`DEGRADED`** | 3 | `true` | `PARTIAL` | Active vibration alert approaching rated threshold; malformed vendor payload skipped during ingestion. |
| **EQ-006** | `PLANT_02` | `LINE-D` | **`WATCH`** | 1 | `false` | `STALE` | Sensor degraded signal (`SENSOR_HEALTH_DROP`). Per engine rule 4, sensor signals never exceed attention 1 and produce `LOW_SIGNAL_TRUST`. |
| **EQ-007** | `PLANT_02` | `LINE-E` | **`UNDER_MAINTENANCE`** | 1 | `false` | `STALE` | Active recalibration work order (`in_progress`); operational alarms suppressed (`needs_attention = false`, `MACHINE_IN_MAINTENANCE`). |
| **EQ-008** | `PLANT_02` | `LINE-E` | **`DEGRADED`** | 3 | `true` | `FRESH` | Physical inspection found major defect (loose heat shield); unmapped vendor alert code received (`INSPECTION_MAJOR_DEFECT`, `UNMAPPED_ALERT_CODE`). |

---

## 7. API Reference & Examples

### Authentication Scopes
Authenticate via the `X-API-Key` HTTP header:
- Ingestion Scope: `aurik-ingest-secret-key-prod-01`
- Read Scope: `aurik-read-secret-key-prod-03`
- Admin Scope: `aurik-admin-secret-key-prod-02` (Satisfies all scopes)

### 1. Ingest Vendor Payload
```bash
curl -X POST "http://localhost:8000/v1/ingest/pulseforge" \
  -H "X-API-Key: aurik-ingest-secret-key-prod-01" \
  -H "Content-Type: application/json" \
  -d '{
    "vendor": "PulseForge",
    "plant_id": "PLANT_01",
    "events": [
      {
        "event_id": "PF-2001",
        "machine_id": "EQ-001",
        "line_id": "LINE-A",
        "event_time": "2026-04-18T08:00:00Z",
        "event_type": "HIGH_VIBRATION",
        "severity": "high",
        "vibration_mm_s": 12.4,
        "temperature_c": 82.0,
        "machine_state": "running",
        "sensor_health": 0.95,
        "vendor_confidence": 0.90
      }
    ]
  }'
```

### 2. Query Machine Operational Views (Sorted by Urgency)
```bash
curl -X GET "http://localhost:8000/v1/machines/status?needs_attention=true" \
  -H "X-API-Key: aurik-read-secret-key-prod-03"
```

### 3. Query Single Machine State with Time-Travel
```bash
# Query current state
curl -X GET "http://localhost:8000/v1/machines/EQ-001/status" \
  -H "X-API-Key: aurik-read-secret-key-prod-03"

# Query historical point-in-time state
curl -X GET "http://localhost:8000/v1/machines/EQ-001/status?as_of=2026-04-18T07:55:00Z" \
  -H "X-API-Key: aurik-read-secret-key-prod-03"
```

### 4. Query Plant Operational Summary
```bash
curl -X GET "http://localhost:8000/v1/plants/PLANT_01/summary" \
  -H "X-API-Key: aurik-read-secret-key-prod-03"
```

### 5. Inspect Queue Jobs & Operational Metrics
```bash
# List dead-letter jobs
curl -X GET "http://localhost:8000/v1/admin/jobs?status=DEAD" \
  -H "X-API-Key: aurik-admin-secret-key-prod-02"

# Requeue a failed job
curl -X POST "http://localhost:8000/v1/admin/jobs/<job-id>/requeue" \
  -H "X-API-Key: aurik-admin-secret-key-prod-02"

# Prometheus text metrics
curl -X GET "http://localhost:8000/metrics"
```

---

## 8. Environment Variables Reference

| Variable | Default Value | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/aurik_monitoring` | PostgreSQL connection URI using psycopg v3. |
| `API_KEY_INGEST` | `aurik-ingest-secret-key-prod-01` | Secret key for external ingestion endpoints. |
| `API_KEY_READ` | `aurik-read-secret-key-prod-03` | Secret key for operational read and summary APIs. |
| `API_KEY_ADMIN` | `aurik-admin-secret-key-prod-02` | Administrative key for metrics, DLQ requeue, and recomputations. |
| `ALERT_TTL_MINUTES` | `60` | Rolling time-to-live window for transient sensor telemetry alerts. |
| `FRESHNESS_MINUTES` | `1440` | Maximum acceptable telemetry gap (24h) before an asset is marked `STALE`. |
| `VENDOR_CONFLICT_WINDOW_MINUTES` | `5` | Window within which opposing vendor readings trigger `VENDOR_CONFLICT`. |
| `JOB_LEASE_SECONDS` | `60` | Worker lock lease duration before crash-recovery reclaims the job. |
| `JOB_MAX_ATTEMPTS` | `3` | Maximum retry attempts before routing a failed job to dead-letter (`DEAD`). |

---

## 9. Documentation Directory Index

- **[`docs/ARCHITECTURE.md`](file:///d:/project/aurik_backend_assesment/docs/ARCHITECTURE.md)**: Mermaid sequence diagrams, data pipeline architecture, and ER diagram.
- **[`docs/DESIGN_NOTE.md`](file:///d:/project/aurik_backend_assesment/docs/DESIGN_NOTE.md)**: 1-page senior technical lead architectural defense, trade-offs, and rationale.
- **[`docs/RULES.md`](file:///d:/project/aurik_backend_assesment/docs/RULES.md)**: Complete deterministic operational engine rules catalog, reason codes, and attention mappings.
- **[`docs/DATA_FINDINGS.md`](file:///d:/project/aurik_backend_assesment/docs/DATA_FINDINGS.md)**: Detailed findings and dialect discrepancies discovered during initial asset inspection.
- **[`docs/openapi.json`](file:///d:/project/aurik_backend_assesment/docs/openapi.json)**: Complete OpenAPI 3.1 schema.
- **[`docs/postman_collection.json`](file:///d:/project/aurik_backend_assesment/docs/postman_collection.json)**: Ready-to-import Postman v2.1 collection covering all endpoints.
- **[`AI_USAGE.md`](file:///d:/project/aurik_backend_assesment/AI_USAGE.md)**: Full disclosure on AI-assisted pair programming and human review methodology.
