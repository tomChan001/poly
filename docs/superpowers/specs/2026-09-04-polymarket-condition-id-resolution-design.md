# Polymarket Condition-ID Resolution Fix

## Problem

Oddpool candidates identify a Polymarket event with both an event slug and native
market identifiers. For multi-market events, the event slug is not necessarily a
market slug. The current resolver queries Gamma by `slug` first, so a valid market
can produce an empty candidate list and fail with `Polymarket condition ID must
resolve to one market`.

The reported Denver Outlaws candidate demonstrates this case: its Oddpool
condition ID and token ID resolve to one active, open Polymarket market, while the
event slug resolves to no Gamma market or event.

## Design

When an Oddpool Polymarket leg includes `source_condition_id`, the native metadata
resolver will query Gamma's `/markets` endpoint with `condition_ids` instead of
`slug`. It will then retain the existing unique-condition check and token-ID check.
This preserves the fail-closed behavior if Gamma returns zero markets, multiple
markets, a different condition ID, or a market without the expected token.

The existing slug lookup and event-market fallback remain unchanged for legacy
inputs that do not include a source condition ID. No UI, runtime-status, trading,
or persistence behavior changes are included.

## Data Flow

1. Oddpool supplies an opportunity with a Polymarket event slug, condition ID,
   and selected token ID.
2. The resolver queries Gamma `/markets?condition_ids=<condition-id>`.
3. The resolver requires exactly one returned market with that condition ID.
4. The resolver requires the expected token ID to appear exactly once in the
   native market's CLOB token list.
5. Only then does it construct the executable-pair draft.

Legacy opportunities without a condition ID continue through the existing market
slug, event slug, and exact-title resolution path.

## Error Handling

HTTP and payload errors continue to reject only the affected Oddpool candidate.
Identifier mismatches remain errors; the resolver never substitutes a market based
on title when Oddpool supplied native identifiers.

## Testing

Add a regression test proving that a candidate whose event slug is not a market
slug is resolved through `condition_ids`. The test will also assert that the
resolved native condition and token IDs are preserved. Existing tests cover the
slug fallback and mismatch rejection and must remain green.
