# AI Usage & Co-Pilot Disclosure

## Overview
This document transparently outlines the assistance provided by AI tooling (specifically Antigravity, Google DeepMind's advanced agentic coding assistant) during the implementation and hardening of the Aurik Tech Lead Assessment submission.

---

## 1. Areas Where AI Was Used

1. **Repetitive Boilerplate & Scaffolding**:
   - Generation of vendor CSV mapping functions, Pydantic v2 schemas, and SQLAlchemy model declarations.
   - Comprehensive test case scaffolding across 143 unit, property, and integration tests.
2. **Deterministic Engine Exploration & Edge Cases**:
   - Drafting property-based permutation and shuffle tests to verify order-independence of the pure functional status engine.
   - Synthesizing edge case tests (expired lease reclamation, backoff retry progression, duplicate content suppression).
3. **OpenAPI & Postman Schema Generation**:
   - Exporting OpenAPI 3.1 definitions and Postman Collection v2.1.0 JSON schemas from FastAPI endpoint signatures.
   - Structuring Markdown tables for the engine rules catalog and data lineage tracking.

---

## 2. Decisions That Were My Own (Candidate / Author to Complete)

<!--
[CANDIDATE / AUTHOR PLACEHOLDER]
Please replace this placeholder section with your own personal engineering decisions, architectural trade-offs, and domain rule choices.
Do not let AI generate your personal claims here.

Topics you may want to articulate in your own voice for your interview defense:
1. Architectural Choices:
   - Why you chose PostgreSQL FOR UPDATE SKIP LOCKED vs external brokers (Kafka/RabbitMQ/Celery).
   - How you structured the Two-Layer Idempotency model (Gateway raw hash vs Worker semantic content hash).
   - Why the derived status engine was designed as a pure, side-effect-free functional pipeline.
2. Domain Engine & Business Rules:
   - Reasoning behind equipment threshold escalations, recovery signals, and conflict windows.
   - Resolution of EQ-006 sensor degradation vs operational attention.
3. Production Hardening Trade-offs:
   - Security constraints in production mode (key length, disabling Swagger UI).
   - Docker containerization choices (multi-stage non-root images, one-shot migrations vs startup races).
-->

*[Author placeholder: Insert your own architectural decisions and defense notes here]*

---

## 3. Human Review & Verification Methodology

Every line of code and test case in this repository was subjected to verification:
1. **Automated Static Analysis**: Run `ruff check .` and `mypy app/ tests/` in strict mode to ensure zero type coercion gaps, unused imports, or lint violations.
2. **Anchor Reconciliation**: Verified that the seed pipeline reproduces the exact anchor expectations for machines `EQ-001` through `EQ-008` as of `2026-04-18T09:00:00Z`.
3. **Property Testing**: Permutation invariance tested by shuffling signal arrival order to prove that derived equipment statuses are strictly deterministic.
