# Architecture Decision Records

ADRs preserve durable implementation decisions and their consequences. Plans may reference proposed ADRs, but public contracts must not rely on them until their status is Accepted.

## Status values

- `Proposed`: under discussion and not authoritative;
- `Accepted`: current architecture rule;
- `Superseded`: replaced by a newer ADR;
- `Rejected`: considered but not selected.

## Naming

Use `NNNN-short-title.md`. Never rewrite the reasoning of an accepted ADR to make history look cleaner; supersede it with a new ADR.

## Index

| ADR | Status | Decision |
| --- | --- | --- |
| [0001](0001-async-first-lifecycle.md) | Accepted | Use an async-first lifecycle |
| [0002](0002-context-service-access.md) | Accepted | Offer dynamic and explicit service access |
| [0003](0003-behavioral-compatibility.md) | Superseded | Target behavior rather than syntax compatibility |
| [0004](0004-python-toolchain.md) | Accepted | Support Python 3.11 with a uv-based toolchain |
| [0005](0005-cordis-core-equivalence.md) | Accepted | Target Cordis Core concept, API and behavior equivalence |
