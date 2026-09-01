# Kalshi API Key Guidance Design

## Goal

Explain, at the point of configuration and in the project README, how an operator obtains the Kalshi API Key ID and RSA private key without weakening the existing write-only secret boundary.

## Official workflow

The guidance links to Kalshi's official API-key documentation and tells the operator to:

1. Sign in to the matching Kalshi production or demo account.
2. Open `Account & security → API Keys`.
3. Select `Create Key`.
4. Copy the displayed API Key ID into this application's `Key ID` field.
5. Open the downloaded `.key` file as text and paste its complete PEM content into `RSA 私钥`.

The private key must include its PEM header and footer. Kalshi does not retain the private key for later retrieval, so the operator must store it securely when it is generated. The Key ID is not the downloaded filename, account password, or private-key text.

Official reference: <https://docs.kalshi.com/getting_started/api_keys>

## Configuration-page presentation

The Kalshi integration card displays a concise help block before the fields. It contains:

- the numbered account-settings path;
- a link labelled `Kalshi 官方 API Key 获取说明` that opens in a new tab;
- a one-time-download warning;
- the exact mapping from Kalshi output to this application's two fields;
- a reminder to paste the entire PEM value rather than a local file path.

No upload control or automatic filesystem read is added. The existing password input remains write-only and stored in the operating-system credential store.

## README presentation

The README receives a `Kalshi API 凭证` section with the same workflow, the expected PEM shape, and these troubleshooting notes:

- production credentials must be used with the production endpoint and demo credentials with the demo endpoint;
- HTTP 401 usually means the Key ID and private key do not belong to the same generated key, the wrong environment is selected, or the PEM content is incomplete;
- creating a replacement key requires saving both its new Key ID and new private key together.

## Testing and acceptance

Frontend tests verify that the integration page renders the official link, the account-settings path, the one-time warning, and the Key ID/private-key field mapping. Existing secret-handling tests continue to prove that the RSA private key input is a password field and stored secrets are never returned to the browser.

Acceptance requires:

- both the configuration page and README contain the guidance;
- the documentation link targets the official Kalshi domain;
- no private-key value, filename, or local path is logged or persisted in public configuration;
- existing frontend tests, lint, and production build pass.
