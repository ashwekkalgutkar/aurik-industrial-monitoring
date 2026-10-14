# Architectural Design Note & Technical Rationale

**Author**: Senior Backend Engineer / Candidate  
**Context**: Aurik Tech Lead Assessment — Industrial Equipment Monitoring Backend  
**Document Status**: Final / Defensible Design Record  

---

## 1. Architectural Decisions & Rationale

### Decision 1: PostgreSQL `FOR UPDATE SKIP LOCKED` vs. Kafka / RabbitMQ
* **Chosen Approach**: Built a transactional asynchronous job queue directly on PostgreSQL using `SELECT ... FOR UPDATE SKIP LOCKED` with explicit lease timeouts, exponential backoff, and a dead-letter queue.
* **Why**:
  1. **Transactional Integrity**: Writing an incoming raw record and its normalization job occurs inside a **single ACID database transaction**. There is no dual-write problem, no distributed two-phase commit, and zero chance of a raw record existing without an enqueued normalization job.
  2. **Operational Simplicity**: Introducing Kafka or RabbitMQ would require managing broker clusters, consumer group offsets, schema registries, and outbox pollers for a workload easily sustained by PostgreSQL (thousands of events/sec).
  3. **Zero Data Loss**: If a worker crashes mid-task, the job's `locked_until` lease expires automatically and another worker claims it.
* **Rejected Alternatives**:
  - *In-memory background tasks (`asyncio.create_task` / `BackgroundTasks`)*: Unacceptable in production; tasks are lost on process crash or container redeployment.
  - *Redis / Celery*: Introduces an external non-durable or separate failure domain without transactional consistency across PostgreSQL and Redis.

---

### Decision 2: Two-Layer Idempotency Architecture
* **Chosen Approach**: 
  - **Layer 1 (Ingestion Gateway)**: Evaluates `(vendor, record_id)` with a canonical SHA-256 hash of the normalized JSON payload (coercing integer/float equivalence `18 == 18.0` and stripping batch envelopes). If identical, immediately returns `DUPLICATE` (no-op). If hash differs, increments `revision` and marks older records `SUPERSEDED`.
  - **Layer 2 (Normalization Worker)**: Detects duplicate content sent under different record identifiers (e.g. `TW-8801` vs `TW-8801-R`) using content signature indexing `(source_vendor, machine_id, event_time, canonical_event_type)`. If duplicate content is found, sets `processing_state = DUPLICATE_CONTENT` and marks the generated signal `is_effective = False` with `duplicate_of = original_id`.
* **Why**:
  - Network retries, webhook re-deliveries, and client replays are stopped at Layer 1 in milliseconds without queue overhead.
  - Multi-agent or split-stream vendor glitches that create synthetic new IDs for identical readings are neutralized at Layer 2 without polluting machine operational status views.

---

### Decision 3: Raw-First Immutability vs. Direct Transformation
* **Chosen Approach**: Store every incoming payload in `raw_records` *before* attempting validation, schema mapping, or entity resolution.
* **Why**:
  - In industrial IoT, vendors routinely introduce subtle schema drift, unexpected alert codes, or malformed fields. If transformation fails, rejecting the HTTP request loses the raw telemetry forever.
  - With raw-first storage, unparseable records are saved with `processing_state = REJECTED_INVALID` and specific `rejection_reasons`. Once parser rules are updated, administrators can replay raw records using `/v1/admin/jobs/{job_id}/requeue` without asking vendors to re-transmit historical data.

---

### Decision 4: Pure Functional Status Engine
* **Chosen Approach**: Implemented `evaluate_machine_state(...)` as a 100% pure, deterministic function. It takes machine metadata, chronological signals, and configuration parameters, returning derived status, attention level, and traceable reason codes without touching the database, reading system clocks, or modifying input structures.
* **Why**:
  1. **Testability & Property-Based Verification**: The engine is verified against arbitrary shuffle orders (10-run shuffle property tests prove identical outputs regardless of arrival order).
  2. **Auditability & Time Travel**: Because the engine accepts an explicit `as_of` timestamp parameter, operators can inspect the exact historical operational status of any asset at any prior point in time (`GET /v1/machines/{id}/status?as_of=...`).
  3. **No ML Hallucination**: Industrial safety requires explainable, deterministic thresholds rather than probabilistic "black box" inferences. Every status decision links directly to source vendor record IDs (`source_event_refs`).

---

### Decision 5: Materialized Operational View with Optimistic Versioning
* **Chosen Approach**: Persist derived machine status in `machine_state_view` with an indexed `(plant_id, line_id, derived_status, attention_level)` composite index and a monotonic `state_version` integer.
* **Why**:
  - High-traffic plant dashboard queries (`GET /v1/machines/status`, `/v1/plants/{id}/summary`) run in single-digit milliseconds without scanning thousands of historical normalized signals.
  - Status recomputations use optimistic concurrency checks to ensure concurrent workers never overwrite newer state computations with older ones.

---

## 2. Edge Case Defensibility

| Edge Case | Solution & Rationale |
|---|---|
| **EQ-006 Sensor Degradation** | `SENSOR_DEGRADED` signals (dimension `SENSOR`) indicate telemetry stream health drops rather than equipment failure. Per engine rule 4, they are strictly capped at attention 1, producing `LOW_SIGNAL_TRUST`. `EQ-006` stays `WATCH`, attention 1, `needs_attention = false`. |
| **Out-of-Order Telemetry** | Normalization signals are sorted chronologically by `(event_time, ingested_at)`. Recomputations evaluate the chronological timeline rather than arrival order. |
| **Idle Equipment Power Fluctuations** | Power alerts when machine is in `idle` state are de-escalated to attention 1 (`POWER_ANOMALY`), preventing spurious alerts during planned downtime. |
| **Under-Maintenance Assets** | Machines undergoing active work orders (`status = in_progress`) or service updates are forced into `UNDER_MAINTENANCE` with operational alarms suppressed (`needs_attention = false`), preventing false alerts while technicians work. |
| **Cross-Vendor Conflict** | Discrepancies between different vendors reporting opposing states on the same dimension within a 5-minute window trigger `VENDOR_CONFLICT` (attention 1) for engineering review. |
| **Unmapped Vendor Alert Codes** | Unmapped alert codes are mapped to `CanonicalEventType.UNKNOWN_EVENT` (attention 1) and are **never** cleared by subsequent `NOMINAL` events, ensuring unmapped codes remain visible for root-cause analysis. |

---

## 3. Production Roadmap

If deploying to a multi-region production cluster at scale:
1. **Queue Horizontal Scaling**: Partition the `jobs` table by hash of `machine_id` across worker pools to eliminate lock contention on high-volume assets.
2. **Signal Archival & Partitioning**: Range-partition `normalized_signals` and `raw_records` by monthly `event_time` intervals, dropping or archiving older partitions to cold storage (e.g. S3 Parquet via DuckDB).
3. **Change Data Capture (CDC)**: Stream `machine_state_view` upserts via PostgreSQL logical replication (WAL) or Debezium directly to WebSocket notification hubs for zero-latency operator screen refreshes.
