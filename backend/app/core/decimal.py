from decimal import Decimal, InvalidOperation

type DecimalInput = str | int


def parse_decimal(value: DecimalInput, *, non_negative: bool = True) -> Decimal:
    """Parse API values without allowing binary floating-point contamination."""
    if isinstance(value, (bool, float)):
        raise TypeError("float and boolean values are not accepted; use a decimal string")
    if not isinstance(value, (str, int)):
        raise TypeError("decimal value must be a string or integer")

    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc

    if not parsed.is_finite():
        raise ValueError("decimal value must be finite")
    if non_negative and parsed < 0:
        raise ValueError("decimal value must be non-negative")
    return parsed


def parse_price(value: DecimalInput) -> Decimal:
    price = parse_decimal(value)
    if price > Decimal(1):
        raise ValueError("price must be between 0 and 1")
    return price
