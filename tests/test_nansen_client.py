import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import nansen_client as nansen
import wallet
import bot

ADDRESS = "4fFKXHGqqBgbnCf8rre6GC7c4uEu7rmJ1Le7PfGp64uN"


class Response:
    def __init__(self, value):
        self.data = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


def balances(rows, last=True):
    return {"data": rows, "pagination": {"is_last_page": last}}


class NansenClientTests(unittest.TestCase):
    def setUp(self):
        nansen._BALANCE_CACHE.clear()
        nansen._PNL_CACHE.clear()
        nansen._DEFI_CACHE.clear()
        nansen._TOKEN_PNL_CACHE.clear()

    def test_official_balance_pages_all_holdings_and_does_not_cut_at_twenty(self):
        requests = []

        def open_url(request, timeout):
            body = json.loads(request.data)
            requests.append((request.full_url, body, timeout))
            if "pnl-summary" in request.full_url:
                return Response({"realized_pnl_usd": 9.5, "realized_pnl_percent": 0.2})
            if "defi-holdings" in request.full_url:
                return Response({"summary": {"total_value_usd": 123, "protocol_count": 2}, "protocols": []})
            if body["pagination"]["page"] == 1:
                return Response(balances([{"token_address": f"mint{i}", "token_symbol": "DUP",
                                          "token_name": f"Token {i}", "token_amount": i + 1,
                                          "price_usd": 2, "value_usd": (i + 1) * 2}
                                         for i in range(20)], False))
            return Response(balances([{"token_address": "mint20", "token_symbol": "DUP",
                                       "token_amount": 1, "price_usd": 2, "value_usd": 2}]))

        info = nansen.get_wallet_info(ADDRESS, "test-secret", cache_seconds=0, opener=open_url, now=1)
        self.assertEqual(len(info["tokens"]), 21)
        self.assertEqual(len({x["mint"] for x in info["tokens"]}), 21)
        self.assertEqual(info["estimated_usd"], "422")
        self.assertEqual(info["portfolio_total_usd"], "422")
        self.assertEqual(info["source"], "nansen")
        self.assertEqual(info["tokens"][0]["pnl_usd"], None)
        self.assertEqual(info["pnl_summary"]["realized_pnl_usd"], 9.5)
        self.assertEqual([r[1]["pagination"]["page"] for r in requests if "current-balance" in r[0]], [1, 2])
        self.assertTrue(all(r[1]["chain"] == "solana" for r in requests if "chain" in r[1]))
        self.assertTrue(all(r[1]["hide_spam_token"] is True for r in requests if "current-balance" in r[0]))
        self.assertEqual(requests[0][0], "https://api.nansen.ai/api/v1/profiler/address/current-balance")

    def test_duplicate_ticker_mints_stay_separate_and_missing_values_remain_null(self):
        info = nansen.normalize_balances(ADDRESS, [
            {"token_address": "mint-a", "token_symbol": "SAME", "token_amount": 2,
             "price_usd": 3, "value_usd": 6},
            {"token_address": "mint-b", "token_symbol": "SAME", "token_amount": 4},
        ])
        self.assertEqual(len(info["tokens"]), 2)
        self.assertEqual(info["estimated_usd"], "6")
        self.assertEqual(info["unpriced_tokens"], 1)
        self.assertIsNone(info["tokens"][1]["price_usd"])
        self.assertIsNone(info["tokens"][1]["value_usd"])
        self.assertIsNone(info["tokens"][1]["pnl_usd"])

    def test_one_hundred_balances_are_paginated_and_top_is_valued_from_same_snapshot(self):
        page_calls = []

        def open_url(request, timeout):
            if "current-balance" in request.full_url:
                body = json.loads(request.data)
                page = body["pagination"]["page"]
                page_calls.append(page)
                start = (page - 1) * 50
                rows = [{"token_address": f"mint-{i}", "token_symbol": "SIGF" if i == 73 else f"T{i}",
                         "token_name": f"Token {i}", "token_amount": 1,
                         "price_usd": 10 if i == 73 else 1, "value_usd": 397.48 if i == 73 else 0.5}
                        for i in range(start, start + 50)]
                return Response({"data": rows, "pagination": {"page": page, "per_page": 50,
                                                                  "is_last_page": page == 2}})
            return Response({})

        info = nansen.get_wallet_info(ADDRESS, "key", cache_seconds=0, opener=open_url, now=5)
        self.assertEqual(len(info["tokens"]), 100)
        self.assertEqual(page_calls, [1, 2])
        self.assertEqual(info["top_token"]["symbol"], "SIGF")
        self.assertEqual(info["top_token"]["value_usd"], "397.48")
        self.assertEqual(info["portfolio_total_usd"], "446.98")

    def test_force_refresh_bypasses_balance_cache(self):
        calls = []

        def open_url(request, timeout):
            calls.append(request.full_url)
            if "current-balance" in request.full_url:
                return Response(balances([{"token_address": "mint", "token_symbol": "T",
                                           "token_amount": 1, "value_usd": 1}]))
            return Response({})

        nansen.get_wallet_info(ADDRESS, "key", cache_seconds=60, opener=open_url, now=10)
        nansen.get_wallet_info(ADDRESS, "key", cache_seconds=60, opener=open_url, now=11, force_refresh=True)
        self.assertEqual(sum("current-balance" in call for call in calls), 2)

    def test_empty_wallet_is_a_valid_zero_holding_result(self):
        info = nansen.normalize_balances(ADDRESS, [])
        self.assertEqual(info["token_count"], 0)
        self.assertEqual(info["estimated_usd"], "0")
        self.assertEqual(info["portfolio_total_usd"], "0")
        self.assertEqual(info["tokens"], [])

    def test_defi_failure_keeps_wallet_total_explicitly_partial(self):
        def open_url(request, timeout):
            if "current-balance" in request.full_url:
                return Response(balances([{"token_address": "mint", "token_symbol": "T",
                                          "token_amount": 1, "price_usd": 5, "value_usd": 5}]))
            if "defi-holdings" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 403, "x", {}, io.BytesIO(b"restricted"))
            return Response({})

        info = nansen.get_wallet_info(ADDRESS, "key", cache_seconds=0, opener=open_url, now=50)
        self.assertEqual(info["estimated_usd"], "5")
        self.assertEqual(info["portfolio_total_usd"], "5")
        self.assertFalse(info["portfolio_total_is_partial"])

    def test_invalid_response_parsing_raises_safe_error(self):
        with self.assertRaises(nansen.NansenError):
            nansen.fetch_balances(ADDRESS, "secret", opener=lambda *_a, **_k: Response({"oops": []}))

    def test_pnl_summary_formats_supported_period_and_keeps_aggregate_separate(self):
        calls = []

        def open_url(request, timeout):
            calls.append(json.loads(request.data))
            return Response({"realized_pnl_usd": -3, "realized_pnl_percent": -0.1, "win_rate": 0.4})

        result = nansen.fetch_pnl_summary(ADDRESS, "secret", now=__import__("datetime").datetime(2026, 10, 3), opener=open_url)
        self.assertEqual(result["realized_pnl_usd"], -3)
        self.assertNotIn("unrealized_pnl_usd", result)
        self.assertEqual(calls[0]["chain"], "solana")
        self.assertEqual(calls[0]["date"]["from"], "2026-07-05")

    def test_token_pnl_returns_only_documented_position_metrics(self):
        request_bodies = []

        def open_url(request, timeout):
            request_bodies.append(json.loads(request.data))
            return Response({"data": [{"token_address": "mint-a", "pnl_usd_unrealised": 12.5,
                                       "roi_percent_unrealised": 0.25, "cost_basis_usd": 50,
                                       "holding_usd": 62.5}]})

        result = nansen.fetch_token_pnl(ADDRESS, "mint-a", "secret", opener=open_url)
        self.assertEqual(result["pnl_usd"], 12.5)
        self.assertEqual(result["pnl_percent"], 0.25)  # caller converts documented fraction to percentage
        self.assertEqual(result["cost_basis_usd"], 50)
        self.assertEqual(request_bodies[0]["filters"]["token_address"], "mint-a")
        self.assertEqual(request_bodies[0]["chain"], "solana")

    def test_token_pnl_missing_mint_is_unavailable(self):
        result = nansen.fetch_token_pnl(ADDRESS, "mint-missing", "secret",
                                        opener=lambda *_a, **_k: Response({"data": []}))
        self.assertIsNone(result)

    def test_missing_api_key_raises_without_request(self):
        with self.assertRaises(nansen.NansenError):
            nansen.get_wallet_info(ADDRESS, " ")

    def test_http_401_403_and_500_have_safe_errors(self):
        for status in (401, 403, 500):
            def open_url(_request, timeout, code=status):
                raise urllib.error.HTTPError("https://api.nansen.ai/path", code, "x", {}, io.BytesIO(b"secret-body"))

            with self.subTest(status=status), self.assertRaisesRegex(nansen.NansenError, str(status)):
                nansen._request(nansen.BALANCE_PATH, {}, "secret-key", opener=open_url)

    def test_http_429_retries_with_backoff_and_then_errors(self):
        calls = []

        def open_url(*_args, **_kwargs):
            calls.append(1)
            raise urllib.error.HTTPError("https://api.nansen.ai/path", 429, "x", {}, io.BytesIO(b"secret"))

        with patch.object(nansen.time, "sleep") as sleep:
            with self.assertRaisesRegex(nansen.NansenError, "429"):
                nansen._request(nansen.BALANCE_PATH, {}, "secret-key", opener=open_url)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_timeout_retries_and_does_not_leak_key(self):
        calls = []

        def open_url(*_args, **_kwargs):
            calls.append(1)
            raise TimeoutError("timed out")

        with patch.object(nansen.time, "sleep"):
            with self.assertLogs(nansen.LOG, "WARNING") as logs:
                with self.assertRaises(nansen.NansenError) as error:
                    nansen._request(nansen.BALANCE_PATH, {}, "key-never-log", opener=open_url)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("key-never-log", str(error.exception) + " ".join(logs.output))

    def test_cache_hit_and_expiry(self):
        calls = []

        def open_url(request, timeout):
            calls.append(request.full_url)
            if "current-balance" in request.full_url:
                return Response(balances([{"token_address": "mint", "token_symbol": "T",
                                          "token_amount": 1, "price_usd": 1, "value_usd": 1}]))
            return Response({})

        first = nansen.get_wallet_info(ADDRESS, "key", cache_seconds=60, opener=open_url, now=10)
        second = nansen.get_wallet_info(ADDRESS, "key", cache_seconds=60, opener=open_url, now=20)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 3)  # holdings + DeFi + aggregate PnL
        nansen.get_wallet_info(ADDRESS, "key", cache_seconds=60, opener=open_url, now=71)
        self.assertEqual(len(calls), 4)  # holdings expired; 24h PnL and DeFi caches did not

    def test_configured_provider_uses_nansen_and_rpc_when_nansen_unavailable(self):
        with patch.dict(os.environ, {"NANSEN_API_KEY": "fake", "NANSEN_ENABLED": "true"}):
            with patch.object(nansen, "get_wallet_info", return_value={"source": "nansen"}):
                with patch.object(wallet, "get_rpc_wallet_info") as rpc:
                    self.assertEqual(wallet.get_wallet_info(ADDRESS, lambda _a: None)["source"], "nansen")
                    rpc.assert_not_called()
            with patch.object(nansen, "get_wallet_info", side_effect=nansen.NansenError("offline")):
                with patch.object(wallet, "get_rpc_wallet_info", return_value={"source": "rpc"}) as rpc:
                    self.assertEqual(wallet.get_wallet_info(ADDRESS, lambda _a: None)["source"], "rpc_fallback")
                    rpc.assert_called_once()

    def test_rpc_only_assets_never_merge_into_nansen_holdings(self):
        nansen_tokens = [{"mint": f"nansen-{i}", "symbol": f"N{i}"} for i in range(10)]
        rpc_tokens = [{"mint": f"rpc-{i}", "symbol": f"R{i}"} for i in range(30)]
        with patch.dict(os.environ, {"NANSEN_API_KEY": "fake", "NANSEN_ENABLED": "true"}):
            with patch.object(nansen, "get_wallet_info", return_value={"source": "nansen", "tokens": nansen_tokens}):
                with patch.object(wallet, "get_rpc_wallet_info", return_value={"source": "rpc", "tokens": rpc_tokens}) as rpc:
                    result = wallet.get_wallet_info(ADDRESS, lambda _a: None)
        self.assertEqual([item["mint"] for item in result["tokens"]], [item["mint"] for item in nansen_tokens])
        rpc.assert_not_called()

    def test_missing_key_and_disabled_nansen_use_rpc_fallback(self):
        for env in ({"NANSEN_API_KEY": "", "NANSEN_ENABLED": "true"},
                    {"NANSEN_API_KEY": "fake", "NANSEN_ENABLED": "false"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=False):
                with patch.object(wallet, "get_rpc_wallet_info", return_value={"source": "rpc"}) as rpc:
                    self.assertEqual(wallet.get_wallet_info(ADDRESS, lambda _a: None)["source"], "rpc_fallback")
                    rpc.assert_called_once()

    def test_configured_public_wallet_is_read_only_default_for_wallet_menu(self):
        with patch.dict(os.environ, {"NANSEN_WALLET_ADDRESS": ADDRESS}):
            result = bot.current_wallet({"wallets": {}}, "22", "11")
            self.assertEqual(result["address"], ADDRESS)
            self.assertTrue(result["configured_default"])
            self.assertFalse(result["monitoring_enabled"])
            self.assertEqual(bot.wallet_markup(result)["inline_keyboard"][0][0]["callback_data"], "wallet_view")

    def test_analyzed_position_uses_nansen_mint_details_and_percent_roi(self):
        holding = {"mint": "mint-a", "amount": "10", "allocation_percent": "12.5",
                   "pnl_usd": None, "pnl_percent": None}
        with patch.dict(os.environ, {"NANSEN_API_KEY": "fake", "NANSEN_ENABLED": "true"}):
            with patch.object(bot.solana_wallet, "get_wallet_info", return_value={"source": "nansen", "tokens": [holding]}):
                with patch.object(nansen, "get_token_pnl", return_value={"pnl_usd": "5", "pnl_percent": "0.25", "cost_basis_usd": "45"}) as pnl:
                    amount, _decimals, position = bot.wallet_token_position(ADDRESS, "mint-a")
        self.assertEqual(amount, 10)
        self.assertEqual(position["pnl_percent"], "25.00")
        self.assertEqual(position["cost_basis_usd"], "45")
        pnl.assert_called_once_with(ADDRESS, "mint-a")

    def test_market_metrics_enrich_all_mints_without_overwriting_nansen_valuation(self):
        info = nansen.normalize_balances(ADDRESS, [
            {"token_address": f"mint-{i}", "token_symbol": f"T{i}", "token_amount": 2,
             "price_usd": 2, "value_usd": 4} for i in range(21)
        ])
        requested = []

        def market_batch(mints):
            requested.extend(mints)
            return {mint: {"priceUsd": "99", "marketCap": 1000,
                           "priceChange": {"h24": 5}} for mint in mints}

        wallet._enrich_nansen_market_data(info, market_batch)
        self.assertEqual(len(requested), 21)
        self.assertEqual(len(info["tokens"]), 21)
        self.assertEqual(info["estimated_usd"], "84")  # Nansen's balance value remains primary
        self.assertEqual(info["tokens"][0]["price_usd"], "2")
        self.assertEqual(info["tokens"][0]["market_cap"], 1000)
        self.assertEqual(info["tokens"][0]["price_change_24h"], 5)


if __name__ == "__main__":
    unittest.main()
