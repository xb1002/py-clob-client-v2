from unittest import TestCase
from unittest.mock import patch

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    CreateOrderOptions,
    MarketOrderArgsV1,
    OrderArgsV1,
    PartialCreateOrderOptions,
)
from py_clob_client_v2.constants import AMOY
from py_clob_client_v2.order_builder.constants import BUY


HOST = "https://clob.example.com"
TOKEN_ID = "123"
PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class TestClientOrderOptions(TestCase):
    def _make_v1_client(self) -> ClobClient:
        client = ClobClient(host=HOST, chain_id=AMOY, key=PRIVATE_KEY)
        client._ClobClient__cached_version = 1
        return client

    def test_create_order_uses_complete_options_without_market_lookups(self):
        client = self._make_v1_client()
        options = PartialCreateOrderOptions(
            tick_size="0.01",
            neg_risk=False,
            fee_rate_bps=123,
        )

        with (
            patch.object(client, "_get") as mock_get,
            patch.object(client.builder, "build_order", return_value="signed") as mock_build,
        ):
            result = client.create_order(
                OrderArgsV1(token_id=TOKEN_ID, price=0.5, size=10, side=BUY),
                options,
            )

        self.assertEqual(result, "signed")
        mock_get.assert_not_called()
        self.assertEqual(mock_build.call_args.args[1], CreateOrderOptions("0.01", False))
        self.assertEqual(mock_build.call_args.kwargs["version"], 1)
        self.assertEqual(mock_build.call_args.kwargs["fee_rate_bps"], 123)

    def test_create_market_order_uses_complete_options_without_market_lookups(self):
        client = self._make_v1_client()
        options = PartialCreateOrderOptions(
            tick_size="0.01",
            neg_risk=False,
            fee_rate_bps=321,
        )

        with (
            patch.object(client, "_get") as mock_get,
            patch.object(client.builder, "build_market_order", return_value="signed") as mock_build,
        ):
            result = client.create_market_order(
                MarketOrderArgsV1(token_id=TOKEN_ID, amount=10, side=BUY, price=0.5),
                options,
            )

        self.assertEqual(result, "signed")
        mock_get.assert_not_called()
        self.assertEqual(mock_build.call_args.args[1], CreateOrderOptions("0.01", False))
        self.assertEqual(mock_build.call_args.kwargs["version"], 1)
        self.assertEqual(mock_build.call_args.kwargs["fee_rate_bps"], 321)
