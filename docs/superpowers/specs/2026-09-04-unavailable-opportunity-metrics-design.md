# Unavailable Opportunity Metrics Design

## Problem

The Opportunities page currently renders zeroes for every metric on an early
rejection. For example, a pair rejected because its settlement date exceeds the
risk-policy window has not fetched either native order book, yet the API reports
quantity, VWAPs, capital, profit, ROI, and book age as zero. Those zeroes look
like measured market values even though the values were never calculated.

Mapping approval and runtime eligibility are separate decisions. The UI must
continue to show approved mappings that later fail a runtime risk check, but it
must distinguish unavailable metrics from genuine numeric zeroes.

## Data Contract

`OpportunityRecord` will use `None` for an unavailable metric. The corresponding
JSON fields will be `null`, and the frontend `Opportunity` type will accept
`string | null` for decimal metrics and `number | null` for book age.

The runtime will populate fields according to the evidence reached before a
rejection:

- A rejection before native books are loaded, including
  `SETTLEMENT_TOO_LATE`, has null quantity, VWAPs, fees, capital, payout,
  profit, ROI, and book age.
- A rejection after books are loaded retains the measured book age but leaves
  quote-derived fields null when no quote exists.
- A rejection after a quote is calculated retains the quote-derived values and
  measured book age. The existing `BELOW_MINIMUM_QUANTITY` path is the current
  example.
- An accepted opportunity keeps its existing non-null metrics unchanged.

Dates, mapping status, rejection reasons, evidence versions, risk-policy
version, and fee status keep their current behavior.

## Runtime Structure

The rejection builder will accept an optional calculated quote. Small helpers
will convert the optional quote and optional books into record fields, avoiding
sentinel zeroes. The accepted path remains unchanged. Execution evaluation will
still skip every record with rejection reasons, so this change cannot make a
rejected opportunity executable.

The API remains a direct serialization of `OpportunityRecord`; no new endpoint
or status flag is needed.

## Frontend Behavior

Formatting helpers will accept nullable values and return an em dash for null.
Rows will show the first localized rejection reason in the status badge, for
example `已拒绝 · 最晚结算时间超出限制`. Additional reasons remain available
in the detail drawer.

Each field is rendered independently:

- null quantity, price, money, percentage, or age becomes `—`;
- a present numeric value, including a genuine zero, keeps the existing numeric
  formatting;
- stale-age styling is applied only when age is present and above the threshold.

Overview calculations ignore null ROI and capital values. When no calculated
ROI is present, the overview shows `—` rather than `0.00%`; total capital sums
only calculated capital values.

## Compatibility and Error Handling

The frontend and backend are versioned together in this repository, so the
nullable contract is updated atomically. Missing or null fields are treated as
unavailable in the UI instead of being passed through `Number(null)`, which
would recreate the misleading zero.

Unknown rejection codes remain visible as their raw code, preserving the
existing fallback behavior.

## Tests

Backend tests will prove that:

- settlement-window rejection produces null quote fields and null book age;
- stale-book rejection retains its measured age while quote fields are null;
- a below-minimum-quantity rejection retains its calculated quote metrics;
- accepted opportunity metrics are unchanged;
- API serialization emits JSON null for unavailable fields.

Frontend tests will prove that:

- null values render as em dashes in both the row and detail drawer;
- the row badge includes the localized first rejection reason;
- partially available records retain their measured fields;
- ordinary opportunities retain the current formatted numeric output;
- overview calculations ignore unavailable metrics.

The implementation will run focused backend and frontend tests first, followed
by the complete relevant test suites and a production frontend build.
