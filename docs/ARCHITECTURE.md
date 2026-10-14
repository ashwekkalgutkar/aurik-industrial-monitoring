# System Architecture & Technical Specifications

The Aurik Industrial Equipment Monitoring system ingests inconsistent, multi-vendor operational and maintenance telemetry, applies two-layer idempotency, normalizes dialect differences into canonical signals, processes state transitions asynchronously via a PostgreSQL-backed job queue, and serves a deterministic, explainable operational attention view.

---

## 1. High-Level Architecture Overview

```mermaid
graph TD
    subgraph External Vendors
        PF[PulseForge Agent]
        TW[ThermexWatch Hub]
        MF[MaintaFlow API]
        WO[CMMS Work Orders]
    end

    subgraph Ingestion Layer (FastAPI)
        Auth[Role-Based Auth / Scopes]
        EnvVal[Envelope Structural Validator]
        RawStore[(raw_records)]
        L1Dedup{Layer 1 Hash Check}
        BatchStore[(ingestion_batches)]
    end

    subgraph Asynchronous Queue
        Jobs[(jobs Table)]
        Worker[Background Worker Process]
    end

    subgraph Processing & Normalization
        Adapters[Vendor Adapters]
        L2Dedup{Layer 2 Content Check}
        SigStore[(normalized_signals)]
        Engine[Deterministic Status Engine]
        StateView[(machine_state_view)]
    end

    subgraph Serving Layer
        ReadAPI[Operational Read API]
        Prom[Prometheus Metrics]
    end

    PF -->|HTTP POST| Auth
    TW -->|HTTP POST| Auth
    MF -->|HTTP POST| Auth
    WO -->|HTTP POST| Auth

    Auth --> EnvVal
    EnvVal --> L1Dedup
    L1Dedup -->|New Record| RawStore
    L1Dedup -->|Revision| RawStore
    L1Dedup -->|Duplicate| BatchStore
    RawStore --> Jobs

    Worker -->|SELECT ... FOR UPDATE SKIP LOCKED| Jobs
    Jobs --> Adapters
    Adapters --> L2Dedup
    L2Dedup --> SigStore
    SigStore --> Engine
    Engine --> StateView

    StateView --> ReadAPI
    Jobs --> Prom
```

---

## 2. Ingestion & Two-Layer Idempotency Sequence

```mermaid
sequenceDiagram
    autonumber
    participant Client as Vendor / Ingestion Client
    participant API as Ingestion API (FastAPI)
    participant Raw as raw_records Table
    participant Queue as jobs Table
    participant Worker as Background Worker
    participant Sig as normalized_signals Table
    participant Engine as Operational Status Engine
    participant View as machine_state_view Table

    Client->>API: POST /v1/ingest/{vendor} (Batch Payload)
    API->>API: Verify API Key Scope (Constant-Time HMAC)
    API->>API: Compute Canonical Payload Hash (SHA-256)
    
    alt Layer 1: Same Vendor + Record ID
        API->>Raw: Lookup latest revision by (vendor, record_id)
        opt Same Hash
            API-->>Client: 202 Accepted (Disposition: DUPLICATE / No-Op)
        end
        opt Different Hash
            API->>Raw: Insert Revision N+1, mark older raw records SUPERSEDED
            API->>Queue: Enqueue NORMALIZE_PAYLOAD
            API-->>Client: 202 Accepted (Disposition: REVISION)
        end
    else Layer 1: New Record ID
        API->>Raw: Insert RawRecord (Revision 1, State: QUEUED)
        API->>Queue: Enqueue NORMALIZE_PAYLOAD
        API-->>Client: 202 Accepted (Disposition: ACCEPTED)
    end

    Worker->>Queue: Claim job via SELECT ... FOR UPDATE SKIP LOCKED
    Worker->>Worker: Execute Vendor Normalization Adapter

    alt Layer 2: Semantic Content Deduplication
        Worker->>Sig: Check (vendor, machine_id, event_time, canonical_type)
        opt Match Found with Different Record ID
            Worker->>Raw: Set processing_state = DUPLICATE_CONTENT
            Worker->>Sig: Insert NormalizedSignal (is_effective = false, duplicate_of = original_id)
        end
        opt Clean Signal
            Worker->>Raw: Set processing_state = PROCESSED
            Worker->>Sig: Insert NormalizedSignal (is_effective = true)
            Worker->>Queue: Enqueue RECOMPUTE_MACHINE (dedupe_key = machine_id)
        end
    end

    Worker->>Engine: Evaluate Machine State (Pure Function)
    Engine-->>View: Upsert machine_state_view (Optimistic Version Lock)
```

---

## 3. Asynchronous Job State Machine

The queue is implemented on PostgreSQL without external brokers (Redis/RabbitMQ/Kafka) using transactional row locking:

```mermaid
stateDiagram-v2
    [*] --> PENDING: Enqueue Job
    PENDING --> RUNNING: Claimed by Worker (SKIP LOCKED)
    RUNNING --> SUCCEEDED: Handler Returns Success
    RUNNING --> FAILED_RETRYABLE: Error Caught (attempts < max_attempts)
    FAILED_RETRYABLE --> RUNNING: Exponential Backoff Elapsed
    RUNNING --> DEAD: Max Retries Exhausted (DLQ)
    DEAD --> PENDING: Manual Requeue via Admin API
```

- **Lease Timeout & Crash Recovery**: Every claimed job receives a `locked_until` timestamp. If a worker pod crashes mid-execution, the lease expires and another worker automatically re-claims the job.
- **Pending Deduplication**: Recompute jobs use `dedupe_key = machine_id` with a partial index `WHERE status = 'PENDING'`. High ingestion bursts collapse into a single pending recomputation per machine.

---

## 4. Pure Deterministic Status Engine

```mermaid
flowchart TD
    Signals[Chronological Normalized Signals] --> Filter[Filter Effective Signals <= as_of]
    Filter --> MaintRes{Rule 6: Maintenance Resolutions}
    MaintRes -->|Resolved Prior Alerts| Suppress[Mark RESOLVED_BY_MAINTENANCE]
    MaintRes -->|Unresolved| RecCheck{Rule 1: NOMINAL Signal?}
    
    RecCheck -->|Newer NOMINAL Exists| RecClearing[Clear Alert -> RECOVERING]
    RecCheck -->|Active Alert| Limits{Rule 2: Physical / Rated Limits}

    Limits --> AttnCalc[Calculate Attention Contribution]
    AttnCalc --> SensorCheck{Rule 4: SENSOR Dimension?}
    SensorCheck -->|SENSOR_DEGRADED| CapAttn[Cap at Attention 1, LOW_SIGNAL_TRUST]
    SensorCheck -->|Other Dimensions| CritCheck{Rule 9: High Criticality Asset?}
    CritCheck -->|Base Attn >= 2| Bump[Attention + 1, Capped at 4]
    CritCheck -->|Attn < 2| NoBump[Keep Base Attention]

    CapAttn --> StatusMap{Rule 10: Status Mapping}
    NoBump --> StatusMap
    Bump --> StatusMap

    StatusMap --> MaintOverride{Rule 8: Active Maintenance?}
    MaintOverride -->|Yes| UnderMaint[Status: UNDER_MAINTENANCE, Attention: 0, NeedsAttention: False]
    MaintOverride -->|No| FinalResult[Status: HEALTHY / WATCH / DEGRADED / CRITICAL]
```

---

## 5. Relational Schema & Entity Relationships

```mermaid
erDiagram
    PLANTS ||--o{ LINES : contains
    PLANTS ||--o{ MACHINES : houses
    LINES ||--o{ MACHINES : routes
    MACHINES ||--o| MACHINE_STATE_VIEW : materializes
    MACHINES ||--o{ NORMALIZED_SIGNALS : measures
    INGESTION_BATCHES ||--o{ RAW_RECORDS : groups
    RAW_RECORDS ||--o{ NORMALIZED_SIGNALS : produces
    JOBS ||..|| RAW_RECORDS : processes

    PLANTS {
        string plant_id PK
        string name
    }
    LINES {
        string line_id PK
        string plant_id FK
        string line_name
        string operating_window
    }
    MACHINES {
        string machine_id PK
        string plant_id FK
        string line_id FK
        string machine_type
        string criticality
        date installed_date
        float rated_max_temp_c
        float rated_max_vibration_mm_s
        float baseline_power_kw
        string asset_status
    }
    INGESTION_BATCHES {
        string id PK
        string vendor
        timestamp received_at
        jsonb counts
        string status
    }
    RAW_RECORDS {
        string id PK
        string batch_id FK
        string vendor
        string record_id
        int revision
        jsonb payload
        string payload_sha256
        string processing_state
        timestamp received_at
    }
    NORMALIZED_SIGNALS {
        string id PK
        string raw_record_id FK
        string machine_id FK
        string plant_id
        string line_id
        timestamp event_time
        string canonical_event_type
        string dimension
        int severity
        float vibration_mm_s
        float temperature_c
        float power_kw
        boolean is_effective
    }
    MACHINE_STATE_VIEW {
        string machine_id PK, FK
        string plant_id
        string line_id
        string derived_status
        int attention_level
        boolean needs_attention
        text[] reason_codes
        jsonb explanation
        timestamp latest_relevant_event_time
        timestamp last_processed_at
        string processing_status
    }
    JOBS {
        string id PK
        string type
        jsonb payload
        string dedupe_key
        string status
        int attempts
        timestamp run_after
        timestamp locked_until
    }
```
