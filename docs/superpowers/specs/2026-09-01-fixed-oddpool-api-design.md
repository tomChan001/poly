# Fixed Oddpool API Integration Design

**Date:** 2026-09-01

## Context

The integration screen currently treats the Oddpool base URL as operator-editable. The backend stores that value and the Oddpool adapter calls `GET /api/opportunities` with a bearer token. That contract does not match Oddpool's current official documentation.

Oddpool documents the production base URL as `https://api.oddpool.com`, the cross-venue arbitrage endpoint as `GET /arbitrage/current`, and authentication through the `X-API-Key` header. The documented response is a top-level array of arbitrage rows with Kalshi, Polymarket, and optionally Opinion venue blocks.

Official references:

- <https://docs.oddpool.com/>
- <https://docs.oddpool.com/arbitrage/current>
- <https://docs.oddpool.com/authentication>

## Goals

- Make the Oddpool production base URL fixed and backend-enforced.
- Remove the editable Oddpool URL control from the operator workflow while still displaying the effective endpoint.
- Use the documented Oddpool authentication header, endpoint, and response shape.
- Convert official arbitrage rows into the existing discovery-only `OddpoolOpportunity` model.
- Preserve the existing safety boundary: Oddpool data discovers candidates but never supplies executable prices, rules, balances, or order state.

## Non-goals

- Do not make Kalshi or Polymarket endpoints fixed.
- Do not add Opinion as an executable venue.
- Do not use Oddpool prices in quote evaluation or order submission.
- Do not add WebSocket ingestion, historical-data ingestion, or reference-data API support.
- Do not enable live trading or weaken any existing mapping, rule, market-data, capital, or automation gate.

## Design

### Endpoint ownership

The backend owns a single canonical Oddpool base URL:

```text
https://api.oddpool.com
```

Oddpool integration updates must submit that exact URL. Any other URL is rejected with a clear validation error, and the canonical value is what the repository stores and API responses return. This prevents a modified client from redirecting the API key to an arbitrary host.

The frontend initializes the Oddpool configuration with the canonical URL and renders it as read-only explanatory text instead of an editable URL input. Kalshi and Polymarket keep their existing editable endpoint fields.

### Official request contract

Both the connection probe and discovery client call:

```text
GET https://api.oddpool.com/arbitrage/current
X-API-Key: <write-only API key>
```

The API key remains in the operating-system credential store. It is never returned to the browser, logged, placed in an audit payload, or persisted in the integration configuration table.

HTTP response bodies are not surfaced to the UI because they may contain account or plan details. A 401 or 403 is reported as an authenticated Oddpool request failure with only the status code. Transport failures are reported as unreachable. HTTP 429 responses use the already-defined bounded delay sequence of 1, 2, 4, 8, 16, then 60 seconds; retries stop at the configured attempt limit so one poll cannot block indefinitely.

### Response adaptation

Add source DTOs matching the documented top-level list and venue blocks. The adapter converts qualifying rows into the existing `OddpoolOpportunity` model so discovery services and execution safety logic remain isolated from vendor schema changes.

Only rows containing usable Kalshi and Polymarket identifiers and whose `buy_yes_market`/`buy_no_market` pair is exactly Kalshi plus Polymarket are imported. Rows requiring Opinion are ignored because Opinion is outside the supported execution boundary.

Each qualifying row maps as follows:

| Official field | Internal field |
|---|---|
| `event_id` + `outcome_key` | Stable source candidate ID |
| `event_title` | Opportunity title |
| `label` | Opportunity outcome label |
| `timestamp` | `updated_at`, normalized to UTC |
| `resolution_time` | `resolves_at`, normalized to UTC |
| `gross_cents / 100` | Decimal-string gross spread |
| `fee_cents / 100` | Decimal-string estimated fees |
| `buy_yes_market` / `buy_no_market` | Venue leg outcomes |
| Selected venue `yes_ask` / `no_ask` | Display-only leg prices |
| Kalshi `market_ticker` | Kalshi native market reference |
| `polymarket_event_slug` | Polymarket native event reference |
| Polymarket `condition_id` and token IDs | Source evidence retained for cross-checking |

The stable source ID does not include the current buy direction, so a price-direction change refreshes the same candidate instead of creating a duplicate. Native rule fetching and exact-equivalence review remain authoritative. Oddpool's displayed prices and fee estimate stay evidence-only.

The adapter must not guess missing identifiers, timestamps, directions, or prices. Invalid rows are rejected individually with sanitized structured errors so one malformed row does not discard the rest of the response.

### Internal market references and native metadata resolution

Extend the normalized Oddpool leg with an explicit `market_ref`. For Kalshi this is the documented `market_ticker`; for Polymarket it is the documented `polymarket_event_slug`. `market_url` becomes display evidence rather than the mechanism used to recover an identifier. Existing fixtures are updated to supply explicit references instead of relying on URL path parsing.

`NativePairMetadataResolver` consumes `market_ref` directly and continues to retrieve full rules and settlement metadata from Kalshi and Polymarket before a pair can be reviewed or quoted. The Polymarket condition and side-token IDs from Oddpool are retained as source evidence and compared with the native metadata result, but they do not replace that native lookup. A mismatch rejects the row instead of silently choosing either source. Human-readable native links may still be derived for display.

This targeted boundary change avoids coupling the service layer to Oddpool's vendor DTO while eliminating the current dependency on an undocumented `market_url` response field.

### UI behavior

The Oddpool panel displays:

- the fixed endpoint `https://api.oddpool.com`;
- the existing enabled switch and environment label;
- the write-only API key field and credential fingerprint;
- save, clear-key, and connection-test actions.

There is no editable Oddpool address input. The API remains authoritative even if a custom client attempts to send a different URL.

## Data flow

1. The operator enters an Oddpool API key and enables the integration.
2. The frontend submits the canonical URL with the existing integration payload.
3. The backend validates the URL, saves only public configuration in PostgreSQL, and writes the API key to the credential store.
4. Connection testing calls the official arbitrage endpoint with `X-API-Key` and returns a sanitized result.
5. The discovery loop fetches official arbitrage rows and adapts supported Kalshi/Polymarket rows to internal opportunities.
6. The existing pair discovery service resolves native rules and identifiers, creates or refreshes review drafts, and leaves them non-executable until all existing gates pass.

## Error handling

- A noncanonical Oddpool URL receives a validation error and is not persisted.
- Missing or invalid API keys leave Oddpool not ready.
- Oddpool 401/403 responses expose only the HTTP status and never the response body.
- Oddpool 429 responses follow the bounded retry schedule and terminate after the configured attempt limit.
- A response that is not a top-level list fails the poll as a contract error.
- Malformed individual rows are recorded as candidate errors while valid rows continue through discovery.
- Rows involving unsupported venue pairs are skipped without creating mappings.
- Any native metadata lookup failure leaves the candidate pending or rejected according to existing discovery behavior; it never falls back to Oddpool data as execution evidence.

## Testing

Backend tests must prove:

- the canonical Oddpool URL is accepted and a different host is rejected;
- the probe and discovery client request `/arbitrage/current` with `X-API-Key` and never send `Authorization: Bearer`;
- an official sample response maps cents, timestamps, venue direction, native identifiers, and display prices correctly;
- candidate IDs remain stable when buy direction changes;
- Opinion-only or malformed rows do not create executable-pair drafts;
- secrets and Oddpool response bodies remain absent from API responses and logs;
- Oddpool display prices never enter quote evaluation.

Frontend tests must prove:

- the canonical Oddpool endpoint is visible;
- there is no editable textbox named `Oddpool API 地址`;
- save and test-connection flows continue to work with a write-only API key;
- Kalshi and Polymarket endpoint fields remain editable.

Run the focused backend and frontend tests, followed by the repository's standard test, lint, type-check, and build commands before completion.

## Operational constraint

Oddpool's product pages describe arbitrage-bot use cases, while its current terms also state that the service may not be used for trading automation. This integration remains discovery-only by architecture, but the operator must obtain clarification or permission from Oddpool before relying on its data in a live automated trading workflow. No code change in this scope treats test success as permission to trade.
