# Implementation Plans

Plans translate the architecture into bounded implementation phases. Execute them in numeric order unless an accepted ADR changes the dependency.

| Plan | Purpose | Status |
| --- | --- | --- |
| [00 Foundation](00-foundation.md) | Resolve toolchain and public-contract decisions | Complete |
| [01 Semantic core](01-semantic-core.md) | Build the smallest lifecycle-correct runtime | Complete |
| [02 Complete core](02-complete-core.md) | Complete reflection, config, logger and races | Complete |
| [03 Configuration runtime](03-configuration-runtime.md) | Build Loader, Include, Group and HMR | Complete |
| [04 Harness compatibility](04-harness-compatibility.md) | Validate representative DSH composition | Complete |
| [05 Cordis Core equivalence](05-cordis-core-equivalence.md) | Align concepts, public API and observable behavior with vendored Cordis | Complete |

Plan 05 progress is measured by the [Cordis Core compatibility matrix](../cordis-core-compatibility.md).

## Plan format

Every plan contains goal, dependencies, scope, exclusions, ordered work packages, acceptance criteria, verification and open decisions. Checkboxes represent verified outcomes, not code-writing activity.

When work begins, update the plan status and `.agents/STATE.md`. When a plan completes, record verification evidence and update `docs/roadmap.md`.
