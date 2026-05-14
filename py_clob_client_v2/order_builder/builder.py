from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import time
from typing import Union

from .helpers import (
    to_token_decimals,
    round_down,
    round_normal,
    round_up,
    decimal_places,
)
from .constants import BUY, SELL
from ..config import get_contract_config
from ..constants import ZERO_ADDRESS, BYTES32_ZERO
from ..signer import Signer
from ..clob_types import (
    OrderArgsV1,
    OrderArgsV2,
    MarketOrderArgsV1,
    MarketOrderArgsV2,
    CreateOrderOptions,
    TickSize,
    RoundConfig,
    OrderSummary,
    OrderType,
)
from ..order_utils import (
    ExchangeOrderBuilderV1,
    ExchangeOrderBuilderV2,
    SignatureTypeV1,
    SignatureTypeV2,
    Side,
)
from ..order_utils.model.order_data_v1 import OrderDataV1, SignedOrderV1
from ..order_utils.model.order_data_v2 import OrderDataV2, SignedOrderV2

ROUNDING_CONFIG: dict = {
    "0.1":    RoundConfig(price=1, size=2, amount=3),
    "0.01":   RoundConfig(price=2, size=2, amount=4),
    "0.001":  RoundConfig(price=3, size=2, amount=5),
    "0.0001": RoundConfig(price=4, size=2, amount=6),
}

_SIGNER_WARM_UP_HASH = b"\x00" * 32

# Backend market-order precision constraints:
# maker amount max 2 decimals, taker amount max 4 decimals.
MARKET_ORDER_PRECISION = RoundConfig(price=0, size=2, amount=4)

# Coarse fallback applied to all BUY limit orders:
# maker amount max 2 decimals, taker amount max 4 decimals.
BUY_LIMIT_ORDER_PRECISION = RoundConfig(price=0, size=4, amount=2)
TOKEN_DECIMALS = Decimal("1000000")


def _decimal_quantizer(sig_digits: int) -> Decimal:
    return Decimal("1").scaleb(-sig_digits)


def _to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _round_down_decimal(value, sig_digits: int) -> Decimal:
    return _to_decimal(value).quantize(
        _decimal_quantizer(sig_digits), rounding=ROUND_FLOOR
    )


def _round_up_decimal(value, sig_digits: int) -> Decimal:
    return _to_decimal(value).quantize(
        _decimal_quantizer(sig_digits), rounding=ROUND_CEILING
    )


def _to_token_decimals_decimal(value: Decimal) -> int:
    return int(value * TOKEN_DECIMALS)


class OrderBuilder:
    def __init__(
        self,
        signer: Signer,
        signature_type: SignatureTypeV2 = None,
        funder: str = None,
    ):
        self.signer = signer

        # Signature type used to sign orders, defaults to EOA
        self.signature_type = (
            signature_type if signature_type is not None else SignatureTypeV2.EOA
        )

        # Address which holds funds. Defaults to the signer address.
        # Used for Polymarket proxy wallets and other smart contract wallets.
        self.funder = funder if funder is not None else (self.signer.address() if self.signer else None)
        self._exchange_order_builders = {}
        self._warm_up_signer()

    def _warm_up_signer(self) -> None:
        if self.signer is not None:
            self.signer.sign(_SIGNER_WARM_UP_HASH)

    def _get_exchange_order_builder(self, version: int, neg_risk: bool):
        neg_risk_key = bool(neg_risk)
        builder_key = (version, neg_risk_key)
        builder = self._exchange_order_builders.get(builder_key)
        if builder is not None:
            return builder

        contract_config = get_contract_config(self.signer.get_chain_id())
        if version == 1:
            exchange_address = (
                contract_config.neg_risk_exchange
                if neg_risk_key
                else contract_config.exchange
            )
            builder = ExchangeOrderBuilderV1(
                exchange_address,
                self.signer.get_chain_id(),
                self.signer,
            )
        elif version == 2:
            exchange_address = (
                contract_config.neg_risk_exchange_v2
                if neg_risk_key
                else contract_config.exchange_v2
            )
            builder = ExchangeOrderBuilderV2(
                exchange_address,
                self.signer.get_chain_id(),
                self.signer,
            )
        else:
            raise ValueError(f"unsupported order version {version}")

        self._exchange_order_builders[builder_key] = builder
        return builder

    def get_order_amounts(
        self, side, size: float, price: float, round_config: RoundConfig
    ):
        """Returns (Side, maker_amount, taker_amount) for a limit order."""
        if isinstance(side, Side):
            side = BUY if side == Side.BUY else SELL
        raw_price = round_normal(price, round_config.price)

        if side == BUY:
            raw_taker_amt = _round_down_decimal(size, BUY_LIMIT_ORDER_PRECISION.size)
            raw_maker_amt = _round_up_decimal(
                raw_taker_amt * _to_decimal(raw_price),
                BUY_LIMIT_ORDER_PRECISION.amount,
            )

            return (
                Side.BUY,
                _to_token_decimals_decimal(raw_maker_amt),
                _to_token_decimals_decimal(raw_taker_amt),
            )

        elif side == SELL:
            raw_maker_amt = round_down(size, round_config.size)
            raw_taker_amt = raw_maker_amt * raw_price
            if decimal_places(raw_taker_amt) > round_config.amount:
                raw_taker_amt = round_up(raw_taker_amt, round_config.amount + 4)
                if decimal_places(raw_taker_amt) > round_config.amount:
                    raw_taker_amt = round_down(raw_taker_amt, round_config.amount)

            return Side.SELL, to_token_decimals(raw_maker_amt), to_token_decimals(raw_taker_amt)

        else:
            raise ValueError(f"order_args.side must be '{BUY}' or '{SELL}'")

    def get_market_order_amounts(
        self, side, amount: float, price: float, round_config: RoundConfig
    ):
        """Returns (Side, maker_amount, taker_amount) for a market order."""
        if isinstance(side, Side):
            side = BUY if side == Side.BUY else SELL
        # V2 change: market orders use round_down for price (v1 used round_normal)
        raw_price = round_down(price, round_config.price)

        market_precision = MARKET_ORDER_PRECISION

        if side == BUY:
            raw_maker_amt = round_down(amount, market_precision.size)
            raw_taker_amt = raw_maker_amt / raw_price
            if decimal_places(raw_taker_amt) > market_precision.amount:
                raw_taker_amt = round_up(raw_taker_amt, market_precision.amount + 4)
                if decimal_places(raw_taker_amt) > market_precision.amount:
                    raw_taker_amt = round_down(raw_taker_amt, market_precision.amount)
            if raw_taker_amt == 0 and raw_maker_amt > 0:
                raw_taker_amt = 1 / (10**market_precision.amount)

            return Side.BUY, to_token_decimals(raw_maker_amt), to_token_decimals(raw_taker_amt)

        elif side == SELL:
            raw_maker_amt = round_down(amount, market_precision.size)
            raw_taker_amt = raw_maker_amt * raw_price
            if decimal_places(raw_taker_amt) > market_precision.amount:
                raw_taker_amt = round_up(raw_taker_amt, market_precision.amount + 4)
                if decimal_places(raw_taker_amt) > market_precision.amount:
                    raw_taker_amt = round_down(raw_taker_amt, market_precision.amount)
            if raw_taker_amt == 0 and raw_maker_amt > 0:
                raw_taker_amt = 1 / (10**market_precision.amount)

            return Side.SELL, to_token_decimals(raw_maker_amt), to_token_decimals(raw_taker_amt)

        else:
            raise ValueError(f"order_args.side must be '{BUY}' or '{SELL}'")

    def build_order(
        self,
        order_args: Union[OrderArgsV1, OrderArgsV2],
        options: CreateOrderOptions,
        version: int = 2,
        fee_rate_bps: int = None,
    ) -> Union[SignedOrderV1, SignedOrderV2]:
        """
        Creates and signs a limit order.
        version=2 (default) uses the V2 exchange contract.
        version=1 uses the V1 exchange contract (legacy).
        """
        round_config = ROUNDING_CONFIG[options.tick_size]
        side, maker_amount, taker_amount = self.get_order_amounts(
            order_args.side,
            order_args.size,
            order_args.price,
            round_config,
        )

        ts = str(time.time_ns() // 1_000_000)

        if version == 1:
            if self.signature_type == SignatureTypeV2.POLY_1271:
                raise ValueError("signature type POLY_1271 is not supported for v1 orders")
            resolved_fee_rate_bps = (
                fee_rate_bps if fee_rate_bps is not None
                else getattr(order_args, "fee_rate_bps", 0)
            )
            order_data = OrderDataV1(
                maker=self.funder,
                taker=getattr(order_args, "taker", ZERO_ADDRESS),
                tokenId=order_args.token_id,
                makerAmount=str(maker_amount),
                takerAmount=str(taker_amount),
                side=side,
                feeRateBps=str(resolved_fee_rate_bps),
                nonce=str(getattr(order_args, "nonce", 0)),
                signer=self.signer.address(),
                expiration=str(order_args.expiration),
                signatureType=SignatureTypeV1(int(self.signature_type)),
            )
            builder = self._get_exchange_order_builder(version, options.neg_risk)
            return builder.build_signed_order(order_data)

        elif version == 2:
            order_data = OrderDataV2(
                maker=self.funder,
                tokenId=order_args.token_id,
                makerAmount=str(maker_amount),
                takerAmount=str(taker_amount),
                side=side,
                signer=self.signer.address(),
                signatureType=self.signature_type,
                timestamp=ts,
                metadata=getattr(order_args, "metadata", BYTES32_ZERO),
                builder=order_args.builder_code,
                expiration=str(getattr(order_args, "expiration", 0)),
            )
            builder = self._get_exchange_order_builder(version, options.neg_risk)
            return builder.build_signed_order(order_data)

        else:
            raise ValueError(f"unsupported order version {version}")

    def build_market_order(
        self,
        order_args: Union[MarketOrderArgsV1, MarketOrderArgsV2],
        options: CreateOrderOptions,
        version: int = 2,
        fee_rate_bps: int = None,
    ) -> Union[SignedOrderV1, SignedOrderV2]:
        """
        Creates and signs a market order.
        version=2 (default) uses the V2 exchange contract.
        version=1 uses the V1 exchange contract (legacy).
        """
        round_config = ROUNDING_CONFIG[options.tick_size]
        side, maker_amount, taker_amount = self.get_market_order_amounts(
            order_args.side,
            order_args.amount,
            order_args.price,
            round_config,
        )

        ts = str(time.time_ns() // 1_000_000)

        if version == 1:
            if self.signature_type == SignatureTypeV2.POLY_1271:
                raise ValueError("signature type POLY_1271 is not supported for v1 orders")
            resolved_fee_rate_bps = (
                fee_rate_bps if fee_rate_bps is not None
                else getattr(order_args, "fee_rate_bps", 0)
            )
            order_data = OrderDataV1(
                maker=self.funder,
                taker=getattr(order_args, "taker", ZERO_ADDRESS),
                tokenId=order_args.token_id,
                makerAmount=str(maker_amount),
                takerAmount=str(taker_amount),
                side=side,
                feeRateBps=str(resolved_fee_rate_bps),
                nonce=str(getattr(order_args, "nonce", 0)),
                signer=self.signer.address(),
                expiration="0",
                signatureType=SignatureTypeV1(int(self.signature_type)),
            )
            builder = self._get_exchange_order_builder(version, options.neg_risk)
            return builder.build_signed_order(order_data)

        elif version == 2:
            order_data = OrderDataV2(
                maker=self.funder,
                tokenId=order_args.token_id,
                makerAmount=str(maker_amount),
                takerAmount=str(taker_amount),
                side=side,
                signer=self.signer.address(),
                signatureType=self.signature_type,
                timestamp=ts,
                metadata=getattr(order_args, "metadata", BYTES32_ZERO),
                builder=order_args.builder_code,
            )
            builder = self._get_exchange_order_builder(version, options.neg_risk)
            return builder.build_signed_order(order_data)

        else:
            raise ValueError(f"unsupported order version {version}")

    def calculate_buy_market_price(
        self,
        positions: list,
        amount_to_match: float,
        order_type: OrderType,
    ) -> float:
        if not positions:
            raise Exception("no match")

        total = 0
        for p in reversed(positions):
            size = p["size"] if isinstance(p, dict) else p.size
            price = p["price"] if isinstance(p, dict) else p.price
            total += float(size) * float(price)
            if total >= amount_to_match:
                return float(price)

        if order_type == OrderType.FOK:
            raise Exception("no match")

        p0 = positions[0]
        return float(p0["price"] if isinstance(p0, dict) else p0.price)

    def calculate_sell_market_price(
        self,
        positions: list,
        amount_to_match: float,
        order_type: OrderType,
    ) -> float:
        if not positions:
            raise Exception("no match")

        total = 0
        for p in reversed(positions):
            size = p["size"] if isinstance(p, dict) else p.size
            price = p["price"] if isinstance(p, dict) else p.price
            total += float(size)
            if total >= amount_to_match:
                return float(price)

        if order_type == OrderType.FOK:
            raise Exception("no match")

        p0 = positions[0]
        return float(p0["price"] if isinstance(p0, dict) else p0.price)
