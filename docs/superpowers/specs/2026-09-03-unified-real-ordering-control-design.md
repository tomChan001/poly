# Unified Real Ordering Control and Operational Runtime Design

Date: 2026-09-03

## Background

The application currently exposes overlapping controls for the same operational intent:

- `TRADING_MODE` selects read-only, shadow, or limited-auto behavior.
- The startup automation-evidence gate can force opening off.
- The durable `opening_enabled` system control gates the runtime before market evaluation begins.

This creates two user-facing problems. First, disabling order submission also prevents the runtime from evaluating markets, so the Opportunities page can remain empty even while the Review page has approved pairs. Second, the header can simultaneously show messages such as “opening switch OFF”, “automatic execution not ready”, and “real orders disabled”, which looks like three manual switches.

Risk settings have a related operational gap. Backend endpoints exist, but the active policy is stored only in memory and the current frontend page does not load or save through those endpoints. A restart can therefore lose the selected policy.

## Goals

- Provide exactly one manual control for real order submission.
- Place that control on the Integrations page under a clear “Real ordering” section.
- Keep discovery, review, native market-data loading, validation, risk evaluation, and opportunity publication running while real ordering is off.
- Submit real orders only when the switch is on and all existing safety checks pass.
- Make runtime health, evaluation state, and real-order permission distinct concepts in the UI.
- Preserve automatic shutdown behavior for incidents and unsafe execution states.
- Make risk-policy changes durable and visible after restart.
- Remove `TRADING_MODE` and automation-evidence prerequisites from normal operator control of real ordering.

## Non-goals

- This change does not weaken venue validation, balance checks, fee checks, minimum ROI, capital limits, paired-order protections, idempotency, reconciliation, partial-fill handling, or incident response.
- This change does not add a separate paper-trading engine or simulated execution history.
- This change does not automatically turn real ordering on at startup.
- This change does not delete existing control or execution audit records.

## Selected approach

Use one durable boolean system control as the sole operator-owned permission for creating new real orders. The existing `system_control` record named `opening` remains the storage source so existing installations and audit history remain compatible. Internal code may continue to call the field `opening_enabled` where renaming would create migration risk, but the UI and operator-facing text call it “Real ordering”.

The switch defaults to off for a new database. Once a durable control record exists, its stored value is authoritative across restarts. `OPENING_ENABLED=false` remains only a seed default for installations without that record.

`TRADING_MODE` no longer participates in runtime evaluation, order authorization, readiness, or displayed status. A legacy environment value may be ignored during compatibility rollout and should be removed from examples and active configuration types once references are migrated.

## Runtime data flow

Each live runtime cycle performs the following work regardless of the real-order switch:

1. Validate enabled integrations and credentials required for market evaluation.
2. Load the exact enabled and reviewed pair definitions.
3. Load the latest durable risk policy.
4. Read native order books, balances, and fee data from the selected venues.
5. Run freshness, spread, fee, balance, ROI, capital, and pair-consistency checks.
6. Publish accepted and rejected opportunity results for the Opportunities page.

For an accepted opportunity, behavior then branches:

- Real ordering off: stop before capital reservation, execution authorization/history creation, or any venue order-submission API call.
- Real ordering on: continue through the existing controlled execution path, including a final permission recheck immediately before the first external write.

An opportunity observed while real ordering is off must not be permanently marked as executed or processed. This allows the next cycle after enabling the switch to execute the current opportunity if it is still fresh and still satisfies every check. Normal idempotency and duplicate-order protection continue to apply once execution begins.

Recovery and reconciliation for an order that was already submitted must continue even when real ordering is off. Turning the switch off prevents new openings; it must not abandon an in-flight order, prevent status reconciliation, or block a safety hedge required by the existing partial-fill policy.

## Real-order control API

Reuse the existing system-control endpoint:

- `PUT /api/system-control/opening`

Enabling or disabling requires the existing local operator authorization and persists through the operational control store. It no longer requires `TRADING_MODE=LIMITED_AUTO` or automation-evidence readiness.

The request includes an audit reason. The frontend supplies clear operator reasons for manual enable and disable actions. Automatic incident actions continue to write their own diagnostic reasons.

If persistence fails, the endpoint reports an error and the effective value remains unchanged. The frontend must retain the previous toggle state and display the failure rather than optimistically leaving an incorrect state on screen.

## Integrations page

Add a “Trading execution” panel above the venue/provider cards. It contains one switch labeled “Real ordering”.

The panel explains the two states:

- Off: market evaluation and opportunity display continue; no new real order is submitted.
- On: eligible opportunities may submit real orders after all safety checks pass.

The toggle is the single manual control and does not open a second confirmation dialog. While the request is pending, disable repeated interaction and show progress. On success, update the application-wide state immediately so the header and page agree. On failure, restore or retain the previous value and show a concise error.

No separate “opening”, “automatic execution ready”, or “real orders” switches are introduced elsewhere.

## Status model

The application header separates operational health from permission:

- Evaluation status describes whether the runtime can load configuration and evaluate markets.
- Real-order status mirrors the one durable switch.

Expected healthy messages are:

- Switch off: “Market evaluation running · Real ordering off”.
- Switch on: “Market evaluation running · Real ordering on”.

Switch off is a normal safe operating state and must not be shown as “runtime not ready”. “Runtime not ready” is reserved for actual failures such as missing required integration configuration, inability to load the active policy, or a failed runtime dependency.

The visible `READ ONLY`, `SHADOW`, and `LIMITED AUTO` mode labels are removed. This avoids presenting inactive legacy configuration as another control.

## Durable risk policy

Add a PostgreSQL-backed risk-policy repository and a new migration after the current migration set. The table stores immutable policy versions, including the configured limits, creation time, and version identifier.

Startup behavior is:

1. Load the latest stored policy.
2. If no policy exists, insert the application default as version 1.
3. Use that version as the active policy for runtime evaluation.

Saving from `PUT /api/settings/risk` inserts a new immutable version rather than overwriting history. `GET /api/settings/risk` returns the active values and version metadata. The next runtime cycle reads or receives the newly active policy without requiring a process restart.

The Risk Settings page loads the backend policy instead of using display-only local defaults. Saving uses the backend endpoint, reports pending and error states, and displays the active saved version. Percentage fields remain human-readable percentages in the UI while the API continues using ratios from 0 to 1.

## Failure and safety behavior

The following protections remain mandatory:

- Exact reviewed-pair matching.
- Fresh native order books.
- Venue fee and balance validation.
- Minimum ROI and configured capital limits.
- Paired-order execution and idempotency safeguards.
- Unknown-outcome reconciliation.
- Partial-fill response and emergency hedge handling.
- Automatic real-order shutdown for critical incidents, fee mismatches, reconciliation mismatches, and other existing stop conditions.

The execution service performs a final durable-control check immediately before an external order write. This closes the window where an operator or incident turns real ordering off after opportunity evaluation but before submission.

If the risk policy or required evaluation configuration cannot be loaded, the runtime reports not ready and submits no order. If opportunity publication fails, order submission for that cycle must not proceed with an unobservable decision.

## Migration and compatibility

- Reuse the existing `system_control.opening` record and audit trail.
- Do not reset an existing stored real-order value during deployment.
- Seed new installations with real ordering off.
- Stop using `TRADING_MODE` in startup gating, runtime branching, execution authorization, supervisor behavior, API validation, and frontend status.
- Remove mode-related example configuration and visible mode text after all runtime references are migrated.
- Preserve recovery of existing execution records created before this change.

## Test coverage

Backend tests must demonstrate:

- With real ordering off, accepted and rejected opportunities are still published and no venue submission occurs.
- After an off-to-on transition, the next still-eligible cycle can submit without restarting.
- Enabling through the control API no longer depends on a mode or automation-evidence gate.
- The execution layer rechecks the switch before the network write.
- Incident and reconciliation failures still disable real ordering.
- Recovery of already-submitted or uncertain executions continues while new ordering is off.
- The PostgreSQL risk repository seeds a default, creates immutable versions, loads the latest version after restart, and feeds the next runtime cycle.

Frontend tests must demonstrate:

- The Integrations page displays exactly one real-order toggle and calls the control API.
- A failed toggle request preserves the old state and displays an error.
- Off displays evaluation running plus real ordering off, not runtime not ready.
- On displays evaluation running plus real ordering on.
- The Risk Settings page loads and saves the backend policy and displays its active version.
- Existing discovery, review, opportunities, execution history, and provider-management behavior remains functional.

## Acceptance criteria

The feature is complete when an operator can leave real ordering off and still see the full discovery-to-opportunity workflow, then turn on the single Integrations-page switch and allow only currently valid opportunities to enter the protected real-order path. The status UI must show this distinction clearly, risk settings must survive restart, and all existing automatic safety shutdowns must remain effective.
