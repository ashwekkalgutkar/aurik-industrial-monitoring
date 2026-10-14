# Aurik Industrial Equipment Monitoring Backend

Production-grade, asynchronous industrial equipment monitoring and telemetry normalization backend built for the **Aurik Tech Lead Assessment**.

Ingests inconsistent multi-vendor telemetry (PulseForge, ThermexWatch, MaintaFlow, and CMMS Work Orders), applies two-layer idempotency, normalizes dialect discrepancies into canonical signals, processes state transitions asynchronously via a PostgreSQL row-locked job queue (`SELECT ... FOR UPDATE SKIP LOCKED`), and serves an explainable, deterministic operational attention view across plants, lines, and machines.

---

## 1. Quickstart (Docker Compose)

Run the full stack (PostgreSQL 16, isolated one-shot migration service, API gateway on port 8000, and background worker daemon) with Docker Compose:

```bash
# 1. Clone and enter directory
cd aurik_backend_assesment

# 2. Copy environment template
cp .env.example .env

# 3. Spin up database, run migrations, and launch api + worker services
docker compose up --build
```

The stack starts cleanly:
- `postgres`: PostgreSQL 16 database with health check.
- `migrate`: Runs `alembic upgrade head` once and terminates cleanly (`completed successfully`).
- `api`: FastAPI gateway listening on `http://localhost:8000` (runs as non-root `appuser:10001`).
- `worker`: Asynchronous queue worker daemon processing background jobs.

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
# Or using uv:
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

### Makefile Convenience Commands

A standard `Makefile` is provided for common development and verification workflows:

```bash
make up         # Spin up docker compose stack with one-shot migration
make down       # Tear down compose stack with volumes
make seed       # Seed reference data and historical fixtures
make test       # Run pytest test suite (144 tests)
make lint       # Run ruff check and formatting validation
make typecheck  # Run mypy strict type checker across app/ and tests/
make openapi    # Regenerate docs/openapi.json from the active application
make postman    # Regenerate docs/postman_collection.json
```

---

## 3. Test Suite & Verification (148 Tests Passing)

The test suite contains **148 automated tests** covering unit normalization, property-based shuffle invariance, idempotency layers, queue leasing/crash-recovery, and full anchor integration:

```bash
# Run all tests (unit + integration)
pytest -v

# Run static type checking across all source and test files (strict mode)
mypy app/ tests/ scripts/

# Run code style, format, and linting checks
ruff check .
ruff format --check .
```

### Key Reviewer Property & Resilience Tests

| Test Identifier | File | Coverage & Verification |
|---|---|---|
| **Shuffle-Order Invariance** | `tests/property/test_status_engine_properties.py::test_pure_engine_shuffle_order_invariance` | Ingests fixture signals in 10 randomized shuffle permutations, proving that the pure deterministic engine derives identical statuses and attention scores regardless of signal arrival order. |
| **Expired Lease Reclaim** | `tests/integration/test_queue_integration.py::test_expired_lease_reclaim_by_another_worker` | Simulates a worker crash holding a locked job; advances time past `locked_until`, and verifies a second worker reclaims and executes the job without duplication. |
| **Transient Retry (No Sleeping)** | `tests/integration/test_queue_integration.py::test_transient_retry_exponential_backoff_no_sleep_dlq_and_requeue` | Injects transient failures, advances an injected clock through exponential backoff without `time.sleep()`, routes exhausted retries to `DEAD`, and verifies manual requeue recovers. |
| **Permanent Error Handling** | `tests/integration/test_queue_integration.py::test_permanent_error_unsupported_vendor_rejected_invalid_zero_retries` | Verifies unrecoverable records (invalid vendor or malformed payload) immediately transition to `REJECTED_INVALID` with zero retries. |
| **CSV + JSON Overlap Dedupe** | `tests/integration/test_anchors.py::test_csv_and_json_overlap_produces_duplicate_noop` | Ingests identical records across JSON payloads and CSV files, verifying Layer 1 SHA-256 hash detection returns `DUPLICATE` (no-op). |
| **Semantic Dedupe (`TW-8801-R`)** | `tests/integration/test_anchors.py::test_layer2_content_dedupe_duplicate_content_suppression` | Proves synthetic record IDs (`TW-8801` vs `TW-8801-R`) are caught at Layer 2, marked `DUPLICATE_CONTENT`, and suppressed from double-counting. |
| **Auth 401 & 403 Scopes** | `tests/integration/test_anchors.py::test_auth_missing_invalid_key_401` & `test_auth_insufficient_scope_403` | Tests authentication rejection on missing/invalid keys (HTTP 401) and scope enforcement (HTTP 403 on read keys accessing ingest endpoints). |
| **Revision Supersedes Signals** | `tests/integration/test_anchors.py::test_revision_supersedes_signals_and_triggers_recompute` | Submits a modified payload for an existing record ID, verifying older signals become `is_effective = False` and status recomputes. |

---

## 4. Anchor Machine Status Verification (`2026-04-18T09:00:00Z`)

Complete verified operational attention status across all 8 monitored assets:

| Machine ID | Plant | Line | Derived Status | Attention | Needs Attention | Processing Status | Full Reason Codes (`reason_codes`) |
|---|---|---|---|---|---|---|---|
| **EQ-001** | `PLANT_01` | `LINE-A` | **`WATCH`** | 1 | `false` | `FRESH` | `["INSPECTION_MINOR_DEFECT", "RECOVERING"]` |
| **EQ-002** | `PLANT_01` | `LINE-A` | **`CRITICAL`** | 4 | `true` | `PARTIAL` | `["TEMPERATURE_ABOVE_RATED", "MAINT_OVERDUE", "OPEN_WORK_ORDER"]` |
| **EQ-003** | `PLANT_01` | `LINE-B` | **`HEALTHY`** | 0 | `false` | `STALE` | `[]` |
| **EQ-004** | `PLANT_01` | `LINE-C` | **`HEALTHY`** | 0 | `false` | `FRESH` | `["RESOLVED_BY_MAINTENANCE"]` |
| **EQ-005** | `PLANT_02` | `LINE-D` | **`DEGRADED`** | 3 | `true` | `PARTIAL` | `["VIBRATION_ABOVE_RATED", "VIBRATION_HIGH", "INSPECTION_MINOR_DEFECT", "MAINT_DUE_SOON"]` |
| **EQ-006** | `PLANT_02` | `LINE-D` | **`WATCH`** | 1 | `false` | `STALE` | `["LOW_SIGNAL_TRUST"]` |
| **EQ-007** | `PLANT_02` | `LINE-E` | **`UNDER_MAINTENANCE`** | 1 | `false` | `STALE` | `["CALIBRATION_PENDING", "OPEN_WORK_ORDER", "MACHINE_IN_MAINTENANCE"]` |
| **EQ-008** | `PLANT_02` | `LINE-E` | **`DEGRADED`** | 3 | `true` | `FRESH` | `["INSPECTION_MAJOR_DEFECT", "UNMAPPED_ALERT_CODE", "OPEN_WORK_ORDER", "RECOVERING"]` |

> [!NOTE]
> - **EQ-008**: Full reason codes include `INSPECTION_MAJOR_DEFECT`, `UNMAPPED_ALERT_CODE`, `OPEN_WORK_ORDER`, and `RECOVERING`. There is zero `VENDOR_CONFLICT` (TW-9921 TEMP_CRIT at 08:29:10Z is cleared by PF-1401 RECOVERY_SIGNAL at 08:39:10Z; time diff 10m > 5m conflict window).
> - **EQ-002**: Severe temperature spike (`96.4°C` vs rated `90.0°C`) plus open work order and overdue maintenance. **The only `CRITICAL` asset in the facility.**
> - **EQ-006**: Telemetry sensor degraded (`SENSOR_HEALTH_DROP`). Per engine rule 4, sensor signals never exceed attention 1 and produce `LOW_SIGNAL_TRUST`.
> - **Criticality Escalation**: Applied only when calculated attention is already >= 2 and criticality is `high`. Recorded in `explanation` only when actually applied, and is never a reason code.

---

## 5. Live Endpoint Verification & Real Output Examples

All endpoints have been verified live against PostgreSQL.

### Authentication Scopes
Authenticate via the `X-API-Key` HTTP header:
- Ingestion Scope: `CHANGE_ME_INGEST_KEY_PROD_MIN_24_CHARS`
- Read Scope: `CHANGE_ME_READ_KEY_PROD_MIN_24_CHARS`
- Admin Scope: `CHANGE_ME_ADMIN_KEY_PROD_MIN_24_CHARS` (Satisfies all scopes)

### 1. Ingestion: PulseForge (Accepted & Duplicate)

**Accepted Ingestion (HTTP 202 Accepted)**:
```bash
curl -s -X POST "http://localhost:8000/v1/ingest/pulseforge" \
  -H "X-API-Key: CHANGE_ME_INGEST_KEY_PROD_MIN_24_CHARS" \
  -H "Content-Type: application/json" \
  -d '{"vendor":"PulseForge","plant_id":"PLANT_01","events":[{"event_id":"PF-LIVE-901","machine_id":"EQ-001","line_id":"LINE-A","event_time":"2026-04-18T08:59:00Z","event_type":"RECOVERY_SIGNAL","severity":"info","vibration_mm_s":2.1,"temperature_c":71.0,"machine_state":"running","sensor_health":0.99,"vendor_confidence":0.95}]}'
```
```json
{
  "batch_id": "060dff56-8a5f-4a39-bc10-ff3487c53e05",
  "vendor": "PulseForge",
  "status": "ACCEPTED",
  "received": 1,
  "accepted": 1,
  "duplicate": 0,
  "revision": 0,
  "rejected": 0,
  "records": [
    {
      "record_id": "PF-LIVE-901",
      "disposition": "ACCEPTED",
      "reason": null
    }
  ]
}
```

**Repeat Call with Identical Payload (HTTP 202 Accepted — DUPLICATE)**:
```json
{
  "batch_id": "8b5ec1be-5eeb-410a-ae83-b78997ef2c6e",
  "vendor": "PulseForge",
  "status": "ACCEPTED",
  "received": 1,
  "accepted": 0,
  "duplicate": 1,
  "revision": 0,
  "rejected": 0,
  "records": [
    {
      "record_id": "PF-LIVE-901",
      "disposition": "DUPLICATE",
      "reason": null
    }
  ]
}
```

> [!IMPORTANT]
> **Asynchronous Processing & Queue Counts**:
> Ingestion endpoints validate envelopes, record payloads, and enqueue asynchronous normalization jobs into PostgreSQL (`Job` table) with status `PENDING`, returning HTTP 202 immediately. Background workers process these jobs and enqueue machine recomputations.
> In the prior assessment report, ingesting `PF-LIVE-901` did not increment the `SUCCEEDED` queue counter because the worker daemon was not running during curl execution; the job remained queued as `PENDING`. When the worker loop runs or `drain_queue()` is invoked, the job transitions to `SUCCEEDED` and triggers machine recomputation.

### 2. Batch Status & Record Lineage

**GET `/v1/ingestion/batches/{batch_id}`**:
```bash
curl -s -X GET "http://localhost:8000/v1/ingestion/batches/060dff56-8a5f-4a39-bc10-ff3487c53e05" \
  -H "X-API-Key: CHANGE_ME_READ_KEY_PROD_MIN_24_CHARS"
```
```json
{
  "batch_id": "060dff56-8a5f-4a39-bc10-ff3487c53e05",
  "vendor": "PulseForge",
  "source_filename": null,
  "received_at": "2026-10-14T13:29:28.067341Z",
  "status": "COMPLETED",
  "counts": {
    "received": 1,
    "accepted": 1,
    "duplicate": 0,
    "revision": 0,
    "rejected": 0
  },
  "records": [
    {
      "raw_record_id": "a91b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d",
      "record_id": "PF-LIVE-901",
      "revision": 1,
      "processing_state": "PROCESSED",
      "received_at": "2026-10-14T13:29:28.067341Z",
      "rejection_reasons": []
    }
  ]
}
```

**GET `/v1/ingestion/records/{vendor}/{record_id}`**:
```bash
curl -s -X GET "http://localhost:8000/v1/ingestion/records/PulseForge/PF-LIVE-901" \
  -H "X-API-Key: CHANGE_ME_READ_KEY_PROD_MIN_24_CHARS"
```
```json
{
  "id": "a91b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d",
  "batch_id": "060dff56-8a5f-4a39-bc10-ff3487c53e05",
  "vendor": "PulseForge",
  "record_id": "PF-LIVE-901",
  "revision": 1,
  "processing_state": "PROCESSED",
  "rejection_reasons": [],
  "signals": [
    {
      "signal_id": "764273e9-a687-432d-9653-ec9cf434e7fb",
      "machine_id": "EQ-001",
      "event_time": "2026-04-18T08:59:00Z",
      "canonical_event_type": "NOMINAL",
      "dimension": "NONE",
      "severity": 0,
      "is_effective": true,
      "duplicate_of": null
    }
  ]
}
```

### 3. Operational Machine Views

**Single Machine View: GET `/v1/machines/EQ-001/status?as_of=2026-04-18T09:00:00Z`**:
```bash
curl -s -X GET "http://localhost:8000/v1/machines/EQ-001/status?as_of=2026-04-18T09:00:00Z" \
  -H "X-API-Key: CHANGE_ME_READ_KEY_PROD_MIN_24_CHARS"
```
```json
{
  "machine_id": "EQ-001",
  "plant_id": "PLANT_01",
  "line_id": "LINE-A",
  "machine_name": "EQ-001",
  "machine_type": "CNC Mill",
  "criticality": "high",
  "derived_status": "WATCH",
  "attention_level": 1,
  "needs_attention": false,
  "reason_codes": [
    "INSPECTION_MINOR_DEFECT",
    "RECOVERING"
  ],
  "explanation": [
    {
      "reason_code": "INSPECTION_MINOR_DEFECT",
      "attention_contribution": 1,
      "rule_applied": "Latest inspection recorded minor defect or observation.",
      "observed_value": "minor_defect_found",
      "threshold_applied": "minor_defect_found",
      "signal_refs": [
        {
          "vendor": "MaintaFlow",
          "record_id": "MF-501",
          "raw_record_id": "d05eb491-a189-4a94-b15d-fbba6ecfe7df",
          "role": "inspection",
          "event_time": "2026-04-18T08:14:05Z"
        }
      ],
      "contributing_signal_refs": []
    },
    {
      "reason_code": "RECOVERING",
      "attention_contribution": 0,
      "rule_applied": "Alert cleared by newer recovery/nominal signal.",
      "observed_value": "Cleared by PulseForge nominal signal",
      "threshold_applied": "recovery_signal_received",
      "signal_refs": [
        {
          "vendor": "PulseForge",
          "record_id": "PF-1302",
          "raw_record_id": "060dff56-8a5f-4a39-bc10-ff3487c53e05",
          "role": "recovery",
          "event_time": "2026-04-18T08:32:05Z"
        }
      ],
      "contributing_signal_refs": []
    }
  ],
  "latest_relevant_event_time": "2026-04-18T08:32:05Z",
  "last_processed_at": "2026-10-14T20:08:57.373Z",
  "processing_status": "FRESH",
  "source_event_refs": [
    {
      "vendor": "PulseForge",
      "record_id": "PF-1302",
      "raw_record_id": "060dff56-8a5f-4a39-bc10-ff3487c53e05",
      "role": "recovery",
      "event_time": "2026-04-18T08:32:05Z"
    },
    {
      "vendor": "MaintaFlow",
      "record_id": "MF-501",
      "raw_record_id": "d05eb491-a189-4a94-b15d-fbba6ecfe7df",
      "role": "inspection",
      "event_time": "2026-04-18T08:14:05Z"
    }
  ],
  "rules_version": "1.0.0",
  "as_of": "2026-04-18T09:00:00Z"
}
```

> [!NOTE]
> **Time-Travel `as_of` Semantics**:
> Read endpoints accept an optional `as_of` query parameter (ISO 8601 UTC timestamp). When `as_of` is provided, the engine evaluates status in memory from database state without mutations.
> Without `as_of`, the system clock's current time is used. Because the assessment dataset is dated April 18, 2026, reading without `as_of` at the present date marks records as `STALE` (> 30 minutes without telemetry) or `UNKNOWN` with reason `NO_RECENT_DATA`.

### 4. Operational Summaries (Plant, Line, Global)

**GET `/v1/summary` (Global Rollup)**:
```bash
curl -s -X GET "http://localhost:8000/v1/summary?as_of=2026-04-18T09:00:00Z" \
  -H "X-API-Key: CHANGE_ME_READ_KEY_PROD_MIN_24_CHARS"
```
```json
{
  "total_machines": 8,
  "machines_needing_attention": 3,
  "critical_machines": [
    "EQ-002"
  ],
  "status_counts": {
    "CRITICAL": 1,
    "DEGRADED": 2,
    "WATCH": 2,
    "HEALTHY": 2,
    "UNDER_MAINTENANCE": 1
  },
  "processing_status_counts": {
    "FRESH": 3,
    "STALE": 3,
    "PARTIAL": 2,
    "FAILED": 0
  },
  "worst_lines": [
    {
      "line_id": "LINE-A",
      "line_name": "Machining Line 1",
      "plant_id": "PLANT_01",
      "total_machines": 2,
      "machines_needing_attention": 1,
      "critical_machines": [
        "EQ-002"
      ],
      "status_counts": {
        "HEALTHY": 0,
        "WATCH": 1,
        "DEGRADED": 0,
        "CRITICAL": 1,
        "UNDER_MAINTENANCE": 0
      }
    },
    {
      "line_id": "LINE-D",
      "line_name": "Packaging Line 1",
      "plant_id": "PLANT_02",
      "total_machines": 2,
      "machines_needing_attention": 1,
      "critical_machines": [],
      "status_counts": {
        "HEALTHY": 0,
        "WATCH": 1,
        "DEGRADED": 1,
        "CRITICAL": 0,
        "UNDER_MAINTENANCE": 0
      }
    },
    {
      "line_id": "LINE-E",
      "line_name": "Assembly Line 2",
      "plant_id": "PLANT_02",
      "total_machines": 2,
      "machines_needing_attention": 1,
      "critical_machines": [],
      "status_counts": {
        "HEALTHY": 0,
        "WATCH": 0,
        "DEGRADED": 1,
        "CRITICAL": 0,
        "UNDER_MAINTENANCE": 1
      }
    }
  ]
}
```

> [!NOTE]
> **Line Ranking Tie-Breaker**:
> Production lines are ranked by `machines_needing_attention` descending. Ties are broken by `len(critical_machines)` descending, then `line_id` ascending (`LINE-A` with 1 needing attention and 1 critical machine ranks ahead of `LINE-D` and `LINE-E`).

### 5. Health & Metrics Endpoints

**GET `/healthz` & GET `/readyz`**:
```bash
curl -s -X GET "http://localhost:8000/healthz"
{"status":"healthy","database":"connected","timestamp":"2026-10-14T13:29:27.753822Z"}

curl -s -X GET "http://localhost:8000/readyz"
{"status":"ready","database":"connected","schema":"ready","timestamp":"2026-10-14T13:29:27.794273Z"}
```

**GET `/v1/admin/metrics` (Authenticated JSON Operational Metrics)**:
```bash
curl -s -X GET "http://localhost:8000/v1/admin/metrics" \
  -H "X-API-Key: aurik-admin-secret-key-prod-003"
```
```json
{
  "queue_depth_by_status": {
    "SUCCEEDED": 46,
    "PENDING": 0,
    "RUNNING": 0,
    "FAILED_RETRYABLE": 0,
    "DEAD": 0
  },
  "dead_jobs_count": 0,
  "pending_jobs_count": 0,
  "running_jobs_count": 0,
  "rejections_by_vendor": {
    "ThermexWatch": 1
  },
  "unknown_event_count": 1
}
```

**GET `/metrics` (Prometheus Scrape Text Endpoint)**:
```bash
curl -s -X GET "http://localhost:8000/metrics" | head -n 12
```
```
# HELP aurik_queue_depth_gauge Current number of jobs by status
# TYPE aurik_queue_depth_gauge gauge
aurik_queue_depth_gauge{status="SUCCEEDED"} 46.0
aurik_queue_depth_gauge{status="PENDING"} 0.0
aurik_queue_depth_gauge{status="RUNNING"} 0.0
aurik_queue_depth_gauge{status="FAILED_RETRYABLE"} 0.0
aurik_queue_depth_gauge{status="DEAD"} 0.0
# HELP aurik_unknown_events_total Count of unknown or unmapped vendor alert events
# TYPE aurik_unknown_events_total counter
aurik_unknown_events_total 1.0
```

### 6. Authentication Rejection (401 Unauthorized & 403 Forbidden)

**Invalid API Key (HTTP 401 Unauthorized)**:
```bash
curl -s -X GET "http://localhost:8000/v1/machines/status" -H "X-API-Key: invalid-key"
```
```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Invalid or missing API key.",
    "details": null
  },
  "request_id": "c138d87b-aa79-4592-b430-89d5353e6b12"
}
```

**Insufficient Scope (HTTP 403 Forbidden — Read key attempting Ingest)**:
```bash
curl -s -X POST "http://localhost:8000/v1/ingest/pulseforge" \
  -H "X-API-Key: CHANGE_ME_READ_KEY_PROD_MIN_24_CHARS" \
  -H "Content-Type: application/json" -d '{}'
```
```json
{
  "error": {
    "code": "FORBIDDEN",
    "message": "API key lacks required scope: ingest",
    "details": null
  },
  "request_id": "766c1f10-745a-4cb1-80a2-25e4f47055c5"
}
```

---

## 6. Configuration & Security Hardening

### Production Mode (`APP_ENV=production`)
When deployed with `APP_ENV=production`:
- **Fail-Closed Key Validation**: Booting will fail immediately if any key is empty, matches default/example strings, starts with `CHANGE_ME_`/`changeme`/`placeholder`, or is shorter than 24 characters (`ValueError: Insecure API key detected in production mode`).
- **Interactive Documentation Disabled**: Swagger UI (`/docs`), ReDoc (`/redoc`), and the raw OpenAPI JSON (`/openapi.json`) are disabled by default (`ENABLE_DOCS=false`) to prevent schema enumeration.
- **Database Statement & Connection Timeouts**: Enforces `DB_CONNECT_TIMEOUT_SECONDS=10` and `DB_STATEMENT_TIMEOUT_MS=30000`, preventing database connection starvation.
- **Request Body Limit**: Strict 5MB limit enforced on all incoming payloads.
- **CORS Restricted**: Disabled by default for internal microservice traffic; configurable via `CORS_ALLOW_ORIGINS`.
- **Constant-Time HMAC Comparison**: All API key validation uses `hmac.compare_digest` to prevent timing attacks.
- **Information Leakage Prevention**: Unhandled exceptions return a standardized error envelope with a UUID `request_id` and generic error message; stack traces and raw SQL are never emitted to HTTP clients.

### Environment Variables Reference

| Variable | Default Value | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgrespassword@localhost:5432/aurik_monitoring` | PostgreSQL 16+ connection URI using psycopg v3. |
| `APP_ENV` | `development` | Environment mode (`development`, `testing`, `production`). |
| `ENABLE_DOCS` | `null` (auto-detected) | Explicitly enable/disable `/docs` and `/redoc` (defaults to true in dev/test, false in prod). |
| `CORS_ALLOW_ORIGINS` | `""` | Comma-separated list of allowed origins. Defaults to disabled. |
| `API_KEY_INGEST` | `CHANGE_ME_INGEST_KEY_PROD_MIN_24_CHARS` | Secret key for external ingestion endpoints (min 24 chars in prod). |
| `API_KEY_READ` | `CHANGE_ME_READ_KEY_PROD_MIN_24_CHARS` | Secret key for operational read and summary APIs (min 24 chars in prod). |
| `API_KEY_ADMIN` | `CHANGE_ME_ADMIN_KEY_PROD_MIN_24_CHARS` | Administrative key for metrics, DLQ requeue, and recomputations. |
| `ALERT_TTL_MINUTES` | `120` | Rolling time-to-live window for transient sensor telemetry alerts. |
| `FRESHNESS_MINUTES` | `30` | Telemetry freshness window (30m) before an asset is marked `STALE`. |
| `VENDOR_CONFLICT_WINDOW_MINUTES` | `5` | Window within which opposing vendor readings trigger `VENDOR_CONFLICT`. |
| `JOB_LEASE_SECONDS` | `60` | Worker lock lease duration before crash-recovery reclaims the job. |
| `JOB_MAX_ATTEMPTS` | `3` | Maximum retry attempts before routing a failed job to dead-letter (`DEAD`). |
| `MAX_PAYLOAD_BYTES` | `5242880` | Maximum allowed HTTP request body size (5 MB). |
| `DB_CONNECT_TIMEOUT_SECONDS` | `10` | Database connection attempt timeout. |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | Database query statement execution timeout in milliseconds. |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

> [!NOTE]
> **Verification & Docker Truthfulness**:
> - **Local Environment**: The local development machine runs Python 3.12 with PostgreSQL 18. Docker daemon was not installed/running on this Windows workstation during local verification.
> - **CI Pipeline**: All Docker multi-stage builds (`runner` and `test` targets), non-root execution (`appuser:10001`), production image dependency audit (`pip list` asserting exclusion of pytest/ruff/mypy/httpx), and full integration smoke tests (`docker compose up -d --build --wait`, health checks, database seeding, and `scripts/verify_anchors.py`) are strictly executed and verified on PostgreSQL 16 via the GitHub Actions CI pipeline (`docker-compose-smoke`).

---

## 7. Assumptions & Limitations

1. **PostgreSQL as Job Queue**: PostgreSQL row-level locking (`FOR UPDATE SKIP LOCKED`) provides ACID guarantees with zero external broker operational complexity for throughputs up to thousands of events/second. For multi-hundred-thousand events/second, dedicated partitioned streams (e.g., Apache Kafka or AWS Kinesis) would be preferred.
2. **Telemetry Windows**: Telemetry alerts expire after `ALERT_TTL_MINUTES` (60m) unless refreshed by active signals or explicitly cleared by `NOMINAL` recovery signals.
3. **Sensor Degradation Capping**: Per engine rule 4, sensor stream health drops (`SENSOR_DEGRADED`) produce `LOW_SIGNAL_TRUST` and are capped at attention 1, preventing telemetry dropouts from masquerading as catastrophic mechanical failures.

---

## 8. What I Would Do Next for Production

1. **PostgreSQL Table Partitioning**: Implement range partitioning on `raw_records` and `normalized_signals` by monthly `event_time` intervals with automated retention drop policies to cold storage (e.g. S3 Parquet via DuckDB).
2. **WebSocket / Server-Sent Events (SSE)**: Implement real-time push notifications for the operational attention view using PostgreSQL `LISTEN/NOTIFY` or Redis pub/sub.
3. **Distributed Tracing**: Integrate OpenTelemetry (`opentelemetry-instrumentation-fastapi` and `opentelemetry-instrumentation-psycopg`) to propagate distributed trace spans across vendor ingestion and worker queues.
4. **Hardware Key Management**: Migrate from environment variable API keys to HashiCorp Vault or AWS Secrets Manager with automated key rotation.
5. **Per-Vendor Token-Bucket Rate Limiting**: Enforce tiered token-bucket rate limits per API key at the API gateway layer to prevent runaway vendor retries from saturating ingestion.

---

## 9. Documentation Index

- **[`docs/ARCHITECTURE.md`](file:///d:/project/aurik_backend_assesment/docs/ARCHITECTURE.md)**: Mermaid diagrams, pipeline architecture, ER diagram, and security architecture.
- **[`docs/DESIGN_NOTE.md`](file:///d:/project/aurik_backend_assesment/docs/DESIGN_NOTE.md)**: 1-page senior technical lead architectural defense, trade-offs, and rationale.
- **[`docs/RULES.md`](file:///d:/project/aurik_backend_assesment/docs/RULES.md)**: Complete deterministic operational engine rules catalog, reason codes, and attention mappings.
- **[`docs/DATA_FINDINGS.md`](file:///d:/project/aurik_backend_assesment/docs/DATA_FINDINGS.md)**: Detailed findings and dialect discrepancies discovered during initial asset inspection.
- **[`docs/openapi.json`](file:///d:/project/aurik_backend_assesment/docs/openapi.json)**: Complete OpenAPI 3.1 specification schema.
- **[`docs/postman_collection.json`](file:///d:/project/aurik_backend_assesment/docs/postman_collection.json)**: Ready-to-import Postman v2.1 collection covering all endpoints.
- **[`AI_USAGE.md`](file:///d:/project/aurik_backend_assesment/AI_USAGE.md)**: AI usage transparency disclosure and candidate defense placeholder.
