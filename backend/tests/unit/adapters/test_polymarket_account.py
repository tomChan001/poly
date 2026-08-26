import httpx
import pytest

from backend.app.adapters.polymarket.account import (
    PolymarketAccountError,
    PolymarketAccountResolver,
    PolymarketAccountType,
)

PRIVATE_KEY = "0x59c6995e998f97a5a0044966f094538c5f7d2b32a3d47ec9d7c2b4e1f7e3b5d1"
OWNER_ADDRESS = "0xeC165c363b4fB6888BD058c6bB1269c77C9b8E81"
PROXY_ADDRESS = "0x1111111111111111111111111111111111111111"
SAFE_ADDRESS = "0x2222222222222222222222222222222222222222"


def profile_lookup(proxy_wallet: str):
    async def lookup(owner_address: str, chain_id: int) -> dict[str, str]:
        assert owner_address == OWNER_ADDRESS
        assert chain_id == 137
        return {"proxyWallet": proxy_wallet}

    return lookup


@pytest.mark.asyncio
async def test_magic_proxy_derives_owner_and_signature_type() -> None:
    resolver = PolymarketAccountResolver(profile_lookup=profile_lookup(PROXY_ADDRESS))

    profile = await resolver.resolve(
        account_type="magic_proxy",
        private_key=PRIVATE_KEY,
        owner_address=None,
        funder_address=None,
        signature_type=None,
        chain_id=137,
    )

    assert profile.account_type is PolymarketAccountType.MAGIC_PROXY
    assert profile.owner_address == OWNER_ADDRESS
    assert profile.funder_address == PROXY_ADDRESS
    assert profile.signature_type == 1
    assert profile.chain_id == 137


@pytest.mark.asyncio
async def test_magic_proxy_rejects_mismatched_funder() -> None:
    resolver = PolymarketAccountResolver(profile_lookup=profile_lookup(PROXY_ADDRESS))

    with pytest.raises(PolymarketAccountError, match="FUNDER_MISMATCH"):
        await resolver.resolve(
            account_type="magic_proxy",
            private_key=PRIVATE_KEY,
            owner_address=None,
            funder_address=SAFE_ADDRESS,
            signature_type=1,
            chain_id=137,
        )


@pytest.mark.asyncio
async def test_gnosis_safe_rejects_mismatched_owner() -> None:
    resolver = PolymarketAccountResolver(profile_lookup=profile_lookup(PROXY_ADDRESS))

    with pytest.raises(PolymarketAccountError, match="OWNER_MISMATCH"):
        await resolver.resolve(
            account_type="gnosis_safe",
            private_key=PRIVATE_KEY,
            owner_address=SAFE_ADDRESS,
            funder_address=PROXY_ADDRESS,
            signature_type=2,
            chain_id=137,
        )


@pytest.mark.asyncio
async def test_default_lookup_reads_gamma_public_profile() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"proxyWallet": PROXY_ADDRESS})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gamma-api.polymarket.com",
    ) as client:
        resolver = PolymarketAccountResolver(http_client=client)
        profile = await resolver.resolve(
            account_type="magic_proxy",
            private_key=PRIVATE_KEY,
            owner_address=None,
            funder_address=None,
            signature_type=None,
            chain_id=137,
        )

    assert profile.funder_address == PROXY_ADDRESS
    assert [request.url.path for request in observed] == ["/public-profile"]
    assert observed[0].url.params["address"] == OWNER_ADDRESS


@pytest.mark.asyncio
async def test_default_lookup_maps_http_failures_to_stable_code() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "upstream down"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gamma-api.polymarket.com",
    ) as client:
        resolver = PolymarketAccountResolver(http_client=client)

        with pytest.raises(PolymarketAccountError, match="PROFILE_LOOKUP_FAILED"):
            await resolver.resolve(
                account_type="magic_proxy",
                private_key=PRIVATE_KEY,
                owner_address=None,
                funder_address=None,
                signature_type=None,
                chain_id=137,
            )
