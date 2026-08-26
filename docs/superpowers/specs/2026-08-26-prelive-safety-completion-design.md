# Pre-Live Trading Safety Completion Design

## Purpose

Complete every high-value trading and risk-control behavior that can be implemented and verified with fixtures, fake venue ports, PostgreSQL, and public read APIs. The result is a fail-closed pre-live system; it does not claim profitability or authorize a real exchange order.

## Scope and delivery order

This program is delivered in six independently testable increments:

1. Durable market/rule/settlement identity.
2. Fee-aware depth optimization and capital reservation.
3. Structured evaluation and rejection evidence.
4. Persistent operational controls and automation gating.
5. Reconciliation, partial-hedge supervision, and notifications.
6. Native-book freshness/sequence safety and complete verification.

## 1. Durable market, rule, and settlement identity

`ExecutablePair` gains the execution-critical native metadata currently discarded during discovery:

- expected and worst-case settlement timestamps;
- market category/series used for fee lookup;
- minimum tick and minimum quantity per venue;
- normalized rule hashes and native metadata version/fingerprint;
- market-open status evidence timestamp.

Oddpool's `resolves_at` is discovery evidence only. Native venue settlement fields are authoritative. The maximum-settlement-days policy compares the worst-case native settlement time with the evaluation time.

Discovery computes a material fingerprint from both venue IDs, outcomes, full rule text/hashes, settlement timestamps, categories, and quantity/tick constraints. Any fingerprint change resets an `EXACT` pair to `PENDING_REVIEW`, even when Oddpool's `updated_at` did not change. Unchanged fingerprints remain idempotent.

## 2. Fee-aware optimization and capital reservation

The live evaluator must use the existing `FeeEngine`; zero fees are never an implicit default. Fee rules are selected by venue, market category, order type, and effective version. Missing rules return `FEE_UNKNOWN` and reject execution.

The optimizer evaluates cumulative depth breakpoints. For every candidate quantity it calculates:

- per-venue swept cost;
- per-venue estimated fee;
- explicit cost and risk buffer;
- conservative profit and ROI;
- remaining per-venue usable balance;
- per-trade, per-event, and portfolio exposure.

A quantity is eligible only when each venue's swept cost plus fee fits that venue's usable balance. First-level prices are never used as a balance approximation.

Capital reservations become durable and idempotent by correlation ID. A transaction reserves both venues or neither. Reservations are created before execution authorization, released on confirmed rejection, and converted to unsettled capital on matched fills.

## 3. Structured evaluation evidence

Pair evaluation returns an `EvaluationResult` rather than `None`. It contains:

- pair ID and evaluation ID;
- allowed/rejected state;
- all structured rejection codes;
- rule, book, balance, fee, policy, and settlement evidence versions;
- selected quote when allowed.

The opportunity store and API retain both accepted and rejected evaluations. Required rejection codes include `MAPPING_NOT_EXACT`, `RULE_VERSION_CHANGED`, `SETTLEMENT_TOO_LATE`, `MARKET_NOT_OPEN`, `STALE_BOOK`, `BOOK_SEQUENCE_UNSAFE`, `INSUFFICIENT_DEPTH`, `FEE_UNKNOWN`, `ROI_BELOW_THRESHOLD`, `BALANCE_SHORTAGE`, `EVENT_LIMIT`, `PORTFOLIO_LIMIT`, `UNHEDGED_POSITION_EXISTS`, and `OPENING_DISABLED`.

The UI displays the rejection codes and supporting timestamps instead of silently hiding candidates.

## 4. Persistent controls and automation gating

`SystemControl`, the active risk-policy version, and automation evidence are loaded from PostgreSQL. Process restart must preserve a disabled kill switch and must not restore permissive defaults.

Runtime startup applies the same automation gate used by the control API. `limited_auto` plus `opening_enabled=true` is insufficient by itself: missing or failing evidence forces opening off and records the gate reasons. Default configuration becomes `read_only` and `opening_enabled=false` for a fresh installation.

Changing credentials, account type, material pair metadata, risk limits, fee rules, or reconciliation state invalidates the relevant readiness evidence. No API call can manually mark acceptance metrics as passed; evidence is computed from persisted observations.

## 5. Reconciliation, partial hedge, and notifications

The runtime wires the existing reconciliation, fee-reconciliation, emergency-hedge, and notification services into one execution supervisor.

After each submission and on recovery it:

1. queries both orders/trades using stable identifiers;
2. deduplicates actual fills by venue fill ID;
3. records actual quantity, price, fee, and timestamps;
4. reconciles platform balance/open orders against the local ledger;
5. compares estimated and actual fees;
6. transitions to `PAIRED`, `PARTIALLY_HEDGED`, or `EXCEPTION`;
7. disables new opening for unresolved or mismatched state;
8. creates a durable incident and notification.

In read-only and shadow modes, emergency actions are simulated and recorded. In a future authorized live mode, the same decision object may be passed to a real emergency port only after the automation gate and configured loss limit pass. This implementation does not send a real emergency order during verification.

Notifications use a durable outbox with idempotency keys. A local log sink is always available; a webhook sink is optional. Execution, partial hedge, reconciliation failure, credential invalidation, and capital shortage each produce distinct event types.

## 6. Order-book safety

REST snapshots remain a bootstrap/fallback. Each snapshot records venue timestamp when supplied, local receipt time, content/sequence identity, and market-open status. The system never substitutes evaluation time for a missing venue capture timestamp and then claims the book is fresh; missing required freshness evidence rejects execution.

Streaming book state tracks numeric sequence continuity where the venue provides it. A sequence gap marks the book unsafe until a fresh REST snapshot replaces it. When only hash identities are available, a snapshot can be deduplicated but cannot satisfy numeric sequence-continuity acceptance metrics.

The configured synchronization limit separates maximum individual age from maximum cross-venue arrival gap; the latter defaults to 500 milliseconds rather than inheriting the two-second age limit.

## Persistence and audit model

New durable projections store evaluation evidence, capital reservations, operational controls, risk policies, outbox events, incidents, and actual execution economics. Existing normalized ledger/audit tables remain append-oriented. JSON snapshots may be retained for UI projections, but the values used for safety gates must be queryable and reproducible from persisted evidence.

Every state-changing service accepts an actor or process identity and writes an audit event. Secret values are never included in snapshots or audit payloads.

## Testing strategy

Every behavior is developed test-first:

- regression tests for multi-level depth exceeding a venue balance;
- regression tests for native rule changes with unchanged Oddpool timestamps;
- settlement-boundary and missing-settlement tests;
- fee-category and effective-version tests, including fail-closed unknown fees;
- atomic/idempotent reservation tests against PostgreSQL;
- startup gate and restart-persistence tests;
- structured rejection coverage for every code;
- order-book age, arrival-gap, and sequence-gap tests;
- execution fault matrix covering reject, timeout, duplicate fill, partial fill, restart, reconciliation mismatch, and simulated emergency action;
- outbox idempotency and secret-redaction tests;
- frontend unit and Playwright coverage for rejection and incident views.

The final verification set is backend pytest with real PostgreSQL integration, Ruff, mypy, frontend Vitest, lint, production build, and Chromium desktop/mobile Playwright.

## Acceptance criteria

- Fees, settlement dates, native depth, per-venue balances, and all configured exposure limits participate in every executable quote.
- Material native metadata changes always invalidate prior `EXACT` review.
- Every rejected candidate has persisted structured reasons and replayable evidence.
- Restart cannot clear a kill switch, reset risk policy, bypass automation gates, or lose an unresolved execution.
- Partial or unknown outcomes stop opening, persist an incident, reconcile both venues, and create an idempotent notification.
- Sequence/freshness uncertainty fails closed.
- All verification uses fake trading ports or read-only endpoints; no real order is placed and no profitability claim is made.

