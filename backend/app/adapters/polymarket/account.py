from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

import httpx
from eth_account import Account
from eth_utils import is_address, to_checksum_address

ProfileLookup = Callable[[str, int], Awaitable[dict[str, str] | None]]


class PolymarketAccountType(StrEnum):
    MAGIC_PROXY = "magic_proxy"
    GNOSIS_SAFE = "gnosis_safe"
    DEPOSIT_WALLET = "deposit_wallet"
    EOA = "eoa"


class PolymarketAccountError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class PolymarketAccountProfile:
    account_type: PolymarketAccountType
    owner_address: str
    funder_address: str
    signature_type: int
    chain_id: int


class PolymarketAccountResolver:
    _SIGNATURE_TYPES: ClassVar[dict[PolymarketAccountType, int]] = {
        PolymarketAccountType.MAGIC_PROXY: 1,
        PolymarketAccountType.GNOSIS_SAFE: 2,
        PolymarketAccountType.DEPOSIT_WALLET: 3,
        PolymarketAccountType.EOA: 0,
    }

    def __init__(
        self,
        profile_lookup: ProfileLookup | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        gamma_base_url: str = "https://gamma-api.polymarket.com",
        timeout_seconds: float = 5.0,
    ) -> None:
        self._profile_lookup = profile_lookup
        self._http_client = http_client
        self._gamma_base_url = gamma_base_url
        self._timeout_seconds = timeout_seconds

    async def resolve(
        self,
        *,
        account_type: str | PolymarketAccountType,
        private_key: str,
        owner_address: str | None,
        funder_address: str | None,
        signature_type: int | str | None,
        chain_id: int | str,
    ) -> PolymarketAccountProfile:
        normalized_type = PolymarketAccountType(account_type)
        normalized_chain_id = int(chain_id)
        derived_owner = Account.from_key(private_key).address
        expected_signature_type = self._SIGNATURE_TYPES[normalized_type]

        provided_owner = _optional_address(owner_address, "OWNER_INVALID")
        if provided_owner is not None and provided_owner != derived_owner:
            raise PolymarketAccountError(
                "OWNER_MISMATCH",
                f"configured owner {provided_owner} does not match signer {derived_owner}",
            )

        if signature_type is not None and int(signature_type) != expected_signature_type:
            raise PolymarketAccountError(
                "SIGNATURE_TYPE_MISMATCH",
                f"{normalized_type.value} requires signature_type={expected_signature_type}",
            )

        normalized_funder = await self._resolve_funder(
            normalized_type,
            derived_owner,
            funder_address,
            normalized_chain_id,
        )
        return PolymarketAccountProfile(
            account_type=normalized_type,
            owner_address=derived_owner,
            funder_address=normalized_funder,
            signature_type=expected_signature_type,
            chain_id=normalized_chain_id,
        )

    async def _resolve_funder(
        self,
        account_type: PolymarketAccountType,
        owner_address: str,
        funder_address: str | None,
        chain_id: int,
    ) -> str:
        provided_funder = _optional_address(funder_address, "FUNDER_INVALID")
        if account_type is PolymarketAccountType.MAGIC_PROXY:
            profile = await self._lookup_profile(owner_address, chain_id)
            proxy_wallet = _profile_proxy_wallet(profile)
            if provided_funder is not None and provided_funder != proxy_wallet:
                raise PolymarketAccountError(
                    "FUNDER_MISMATCH",
                    f"configured funder {provided_funder} does not match proxy wallet {proxy_wallet}",
                )
            return proxy_wallet

        if account_type is PolymarketAccountType.EOA:
            if provided_funder is not None and provided_funder != owner_address:
                raise PolymarketAccountError(
                    "FUNDER_MISMATCH",
                    f"configured funder {provided_funder} does not match signer {owner_address}",
                )
            return owner_address

        if provided_funder is None:
            raise PolymarketAccountError(
                "FUNDER_REQUIRED",
                f"{account_type.value} requires an explicit funder address",
            )
        return provided_funder

    async def _lookup_profile(self, owner_address: str, chain_id: int) -> dict[str, str] | None:
        if self._profile_lookup is not None:
            return await self._profile_lookup(owner_address, chain_id)
        return await self._default_profile_lookup(owner_address)

    async def _default_profile_lookup(self, owner_address: str) -> dict[str, str] | None:
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    "/public-profile",
                    params={"address": owner_address},
                )
            else:
                async with httpx.AsyncClient(
                    base_url=self._gamma_base_url,
                    timeout=self._timeout_seconds,
                ) as client:
                    response = await client.get(
                        "/public-profile",
                        params={"address": owner_address},
                    )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PolymarketAccountError(
                "PROFILE_LOOKUP_FAILED",
                "unable to read Polymarket public profile",
            ) from exc
        if not isinstance(payload, dict):
            raise PolymarketAccountError(
                "PROFILE_LOOKUP_FAILED",
                "unable to read Polymarket public profile",
            )
        return payload


def _optional_address(value: str | None, invalid_code: str) -> str | None:
    if value is None:
        return None
    if not is_address(value):
        raise PolymarketAccountError(invalid_code, f"invalid address: {value}")
    return to_checksum_address(value)


def _profile_proxy_wallet(profile: dict[str, str] | None) -> str:
    if not profile:
        raise PolymarketAccountError(
            "PROFILE_LOOKUP_FAILED",
            "profile lookup did not return a Polymarket profile",
        )
    value = profile.get("proxyWallet") or profile.get("proxy_wallet")
    if not value or not is_address(value):
        raise PolymarketAccountError(
            "PROFILE_LOOKUP_FAILED",
            "profile lookup did not return a valid proxy wallet",
        )
    return to_checksum_address(value)
