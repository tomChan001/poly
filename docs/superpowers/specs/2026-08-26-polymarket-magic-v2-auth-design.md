# Polymarket Google/Email (Magic) and CLOB V2 Authentication Design

## Purpose

Support an existing Polymarket account created through Google or email without automating Google login, storing browser cookies, or weakening the current local-secret boundary. The integration must use Polymarket's supported wallet-signing model and must be verifiable without placing a real order.

## Confirmed platform constraints

- Google/email accounts use the Magic/proxy-wallet path. The user exports the signer private key through Polymarket's official Magic reveal flow.
- CLOB orders still require an EIP-712 signer. Google OAuth tokens, the user's email password, and browser session cookies are not trading credentials and must never enter this application.
- Existing Magic/proxy accounts use `POLY_PROXY` (`signature_type=1`) with the Polymarket proxy wallet as the funder.
- The current `py-clob-client` dependency is a V1 client. Production support requires `py-clob-client-v2`.
- The private key and L2 API secret/passphrase remain write-only values in the operating-system credential store. Public configuration may contain wallet addresses, account type, chain ID, endpoint, and API key identifier.

Official references:

- https://help.polymarket.com/en/articles/13364258-how-do-i-export-my-key
- https://docs.polymarket.com/trading/overview
- https://docs.polymarket.com/trading/quickstart
- https://docs.polymarket.com/v2-migration

## Supported account modes

The Polymarket configuration gains an explicit `account_type` field:

| Account type | Signature type | Funder | Intended use |
| --- | ---: | --- | --- |
| `magic_proxy` | 1 | Existing Polymarket proxy wallet | Google/email and legacy Magic accounts |
| `gnosis_safe` | 2 | Existing Safe wallet | Existing Polymarket Safe accounts |
| `deposit_wallet` | 3 | Deposit wallet | New CLOB V2 API users |
| `eoa` | 0 | Signer address | Standalone wallets only |

The UI presents `magic_proxy` first and labels it “Google/邮箱（Magic）”. Advanced modes remain available but collapsed by default.

## Configuration model

Public Polymarket configuration contains:

- `account_type`
- `owner_address` (derived from the submitted private key and returned read-only)
- `funder_address`
- `signature_type` (derived from `account_type`, not freely editable)
- `chain_id` (137 in production)
- `api_key` when existing L2 credentials are supplied

Secret storage contains:

- `private_key` (required for unattended order signing)
- `api_secret` and `passphrase` when existing L2 credentials are supplied

The backend derives the owner address whenever a private key is saved. For `magic_proxy`, it queries Polymarket's public profile endpoint with the owner address and uses the returned `proxyWallet` as the proposed funder. A user-supplied funder is allowed only when it matches the resolved profile. A mismatch fails validation rather than silently switching wallets.

## Backend architecture

### Account profile validation

A focused `PolymarketAccountResolver` owns account-type normalization, owner-address derivation, proxy-wallet discovery, and funder validation. It returns a normalized immutable profile used by both save and connection-test flows.

### CLOB V2 transport

`PolymarketSdkTransport` is migrated behind its existing trading-port boundary to `py-clob-client-v2`. Client construction uses the normalized profile and either:

1. existing API key/secret/passphrase, or
2. `create_or_derive_api_key` when the three L2 values are not all present.

Derived credentials are returned to the integration service for immediate storage in the OS credential store; they are never written to PostgreSQL or API responses.

The transport must expose protocol-level operations needed by the runtime: balance/allowance lookup, FOK order creation, open-order/trade lookup for reconciliation, and actual fill retrieval. A matched response must not fabricate fill price, size, or fee from the request.

### Connection test

The Polymarket connection test performs no order write. It verifies:

- private key parses and derives the configured owner address;
- signature type matches account type;
- funder matches the owner/profile relationship;
- L2 credentials can be derived or authenticated;
- collateral balance and allowance can be read;
- open orders or recent trades can be queried for recovery support.

The result reports structured, non-secret failure codes such as `OWNER_MISMATCH`, `FUNDER_MISMATCH`, `SIGNATURE_TYPE_MISMATCH`, `L2_AUTH_FAILED`, and `ALLOWANCE_INSUFFICIENT`.

## Frontend flow

The Polymarket panel becomes a short guided setup:

1. Select “Google/邮箱（Magic）”.
2. Open the official Magic export link in a new browser tab.
3. Paste the private key into a write-only password field.
4. Save; the backend derives the owner and proxy wallet.
5. Review masked addresses and run “测试连接（不会下单）”.

The UI never asks for a Google password or OAuth token. It displays only secret fingerprints after save. Advanced account types expose the minimum additional address fields required for those modes.

## Failure and safety behavior

- Any account/profile ambiguity leaves Polymarket disabled.
- Partial L2 credentials are rejected; users either provide all three or allow derivation.
- Switching account type invalidates prior derived credentials and requires a new connection test.
- Updating the private key increments the integration version so cached transports are discarded.
- No authentication migration enables opening or changes trading mode.
- Logs and API error bodies pass through existing secret redaction.

## Testing strategy

- Unit tests for account-type/signature mapping, private-key owner derivation, proxy discovery, mismatches, and partial L2 credentials.
- Contract tests around a fake CLOB V2 client for credential derivation, FOK request formation, balance/allowance, actual fills, and recovery queries.
- API tests proving secrets remain write-only and Google/email configuration cannot be enabled until normalized.
- Frontend tests for the guided Magic flow, advanced-mode fallback, secret masking, and “no order” connection-test copy.
- No test or verification step sends a real exchange order. Live-order validation remains a separately authorized canary activity.

## Acceptance criteria

- A Google/email user can configure the integration with the exported Magic key and an automatically verified proxy wallet.
- The runtime constructs a CLOB V2 client with signature type 1 and the correct funder.
- Connection testing succeeds without an order write and reports balance/allowance readiness.
- Wrong key, wrong proxy wallet, partial credentials, or wrong signature type fail closed.
- No raw secret is persisted in PostgreSQL, returned by an API, logged, or rendered after submission.

