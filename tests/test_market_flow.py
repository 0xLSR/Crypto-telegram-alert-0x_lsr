import json
import logging
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import bot
import market_flow as flow
import market_intelligence as intelligence

NOW = 1_800_000_000.0
ADDRESS = "CwSNUU54NLJt4qbGg5S8TUzJYxjYAfL4VrPr7zvJpump"


def trade(identifier, seconds_ago, side, usd):
    return {"id": identifier, "timestamp": NOW-seconds_ago, "side": side, "usd": usd}


class TradeParsingTests(unittest.TestCase):
    def test_parse_documented_gecko_fields_and_oriented_kind(self):
        response = {"data": [{"id": "trade-1", "type": "trade", "attributes": {
            "block_timestamp": "2026-12-15T08:00:00Z", "volume_in_usd": "123.45", "kind": "buy",
            "from_token_address": "quote", "to_token_address": ADDRESS, "tx_hash": "sig"}}]}
        rows = flow.parse_trades(response, ADDRESS)
        self.assertEqual(rows[0]["side"], "buy")
        self.assertEqual(rows[0]["usd"], 123.45)

    def test_parse_never_infers_unknown_side(self):
        response = {"data": [{"id": "trade-1", "type": "trade", "attributes": {
            "block_timestamp": NOW, "volume_in_usd": "30", "kind": "swap?"}}]}
        self.assertEqual(flow.parse_trades(response, ADDRESS)[0]["side"], "unknown")

    def test_parse_rejects_invalid_shape_and_missing_trade_values(self):
        with self.assertRaises(ValueError):
            flow.parse_trades([], ADDRESS)
        response = {"data": [{"type": "trade", "attributes": {"kind": "buy", "volume_in_usd": -1}}]}
        self.assertEqual(flow.parse_trades(response, ADDRESS), [])


class ClassificationAndAggregationTests(unittest.TestCase):
    def test_small_trade_relative_to_deep_pool(self):
        self.assertEqual(flow.classify_trade(10, 1_000_000, 100_000), "small")

    def test_medium_trade_relative_to_pool_volume(self):
        self.assertEqual(flow.classify_trade(20, 1_000_000, 10_000), "medium")

    def test_large_trade_relative_to_pool_liquidity(self):
        self.assertEqual(flow.classify_trade(12_000, 1_000_000, 10_000_000), "large")

    def test_very_large_trade_category(self):
        self.assertEqual(flow.classify_trade(60_000, 1_000_000, 10_000_000), "very_large")

    def test_same_usd_trade_gets_different_relative_classification_by_liquidity(self):
        self.assertEqual(flow.classify_trade(1000, 1_000_000, 1_000_000), "medium")
        self.assertEqual(flow.classify_trade(1000, 10_000, 1_000_000), "very_large")

    def test_buy_flow_sums_usd_not_transaction_count(self):
        summary = flow.summarize([trade("a", 60, "buy", 100), trade("b", 30, "buy", 400),
                                  trade("c", 20, "sell", 50)], NOW)
        self.assertEqual(summary["windows"]["15m"]["buy_usd"], 500)
        self.assertEqual(summary["windows"]["15m"]["net_usd"], 450)

    def test_sell_flow_and_negative_net(self):
        summary = flow.summarize([trade("a", 60, "buy", 100), trade("b", 30, "sell", 400)], NOW)
        self.assertEqual(summary["windows"]["15m"]["net_usd"], -300)

    def test_balanced_flow_is_zero(self):
        summary = flow.summarize([trade("a", 60, "buy", 100), trade("b", 30, "sell", 100)], NOW)
        self.assertEqual(summary["windows"]["15m"]["net_usd"], 0)

    def test_summarize_shows_windows_containing_complete_available_trade_data(self):
        summary = flow.summarize([trade("a", 60, "buy", 1)], NOW)
        self.assertIn("5m", summary["windows"])
        self.assertIn("24h", summary["windows"])
        self.assertFalse(flow.summarize([], NOW)["available"])

    def test_acceleration_of_buys(self):
        state, _event = flow.update_flow(None, [trade("old", 420, "buy", 1000),
                                                 trade("new", 60, "buy", 3000)], now=NOW,
                                         liquidity_usd=1_000_000)
        self.assertEqual(state["summary"]["acceleration"]["buy"], 3)

    def test_acceleration_of_sells(self):
        state, _event = flow.update_flow(None, [trade("old", 420, "sell", 1000),
                                                 trade("new", 60, "sell", 3000)], now=NOW,
                                         liquidity_usd=1_000_000)
        self.assertEqual(state["summary"]["acceleration"]["sell"], 3)

    def test_increasing_large_buy_sequence(self):
        items = [trade("a", 180, "buy", 20_000), trade("b", 120, "buy", 30_000), trade("c", 60, "buy", 40_000)]
        state, event = flow.update_flow(None, items, now=NOW, liquidity_usd=500_000)
        self.assertEqual(state["summary"]["sequence"], "buy")
        self.assertIsNone(event)  # Startup loads a baseline; it does not replay historical alerts.

    def test_increasing_large_sell_sequence(self):
        items = [trade("a", 180, "sell", 20_000), trade("b", 120, "sell", 30_000), trade("c", 60, "sell", 40_000)]
        state, _event = flow.update_flow(None, items, now=NOW, liquidity_usd=500_000)
        self.assertEqual(state["summary"]["sequence"], "sell")

    def test_price_flow_divergence_is_reported_as_possible_context(self):
        rows = [trade("a", 60, "sell", 1000), trade("b", 30, "sell", 1000)]
        _state, _event = flow.update_flow(None, rows, now=NOW, liquidity_usd=1_000_000,
                                          previous_price=1, current_price=1.1)
        summary = flow.summarize(rows, NOW)
        self.assertTrue(summary["available"])
        state, _event = flow.update_flow(None, rows, now=NOW, liquidity_usd=1_000_000,
                                         previous_price=1, current_price=1.1)
        self.assertEqual(state["summary"]["divergence"], "price_up_selling")

    def test_breakout_with_positive_flow_marks_possible_confirmation(self):
        snapshots = [{"timestamp": NOW-600+i*60, "price": p, "volume_24h": 50_000,
                      "market_cap": 500_000, "fdv": 500_000, "liquidity": 100_000}
                     for i, p in enumerate([1, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08, 1.09])]
        pair = {"priceUsd": "1.2", "volume": {"h24": 80_000}, "marketCap": 600_000,
                "fdv": 600_000, "liquidity": {"usd": 100_000}}
        flow_summary = {"available": True, "windows": {"15m": {"buy_usd": 10_000, "sell_usd": 1_000,
                                                                     "net_usd": 9_000, "known_trades": 4}}}
        result = intelligence.analyze(snapshots, pair, timestamp=NOW, flow=flow_summary)
        self.assertEqual(result["metrics"]["flow_breakout_status"], "possible_confirmation")

    def test_breakout_with_selling_or_zero_flow_marks_possible_false_breakout(self):
        snapshots = [{"timestamp": NOW-600+i*60, "price": 1+i*0.01, "volume_24h": 50_000,
                      "market_cap": 500_000, "fdv": 500_000, "liquidity": 100_000} for i in range(10)]
        pair = {"priceUsd": "1.2", "volume": {"h24": 80_000}, "marketCap": 600_000,
                "fdv": 600_000, "liquidity": {"usd": 100_000}}
        bearish = {"available": True, "windows": {"15m": {"buy_usd": 1, "sell_usd": 1000,
                                                                  "net_usd": -999, "known_trades": 4}}}
        result = intelligence.analyze(snapshots, pair, timestamp=NOW, flow=bearish)
        self.assertEqual(result["metrics"]["flow_breakout_status"], "possible_false_breakout")

    def test_low_liquidity_flow_cannot_dominate_score(self):
        pair = {"priceUsd": "1", "volume": {"h24": 10_000}, "marketCap": 20_000,
                "fdv": 20_000, "liquidity": {"usd": 100}}
        flow_summary = {"available": True, "windows": {"15m": {"buy_usd": 1_000_000,
                    "sell_usd": 0, "net_usd": 1_000_000, "known_trades": 10}}}
        result = intelligence.analyze([], pair, timestamp=NOW, flow=flow_summary)
        self.assertLessEqual(result["score"], 35)
        self.assertIn("whale_flow", result["components"])

    def test_insufficient_or_unknown_directional_data_does_not_change_score(self):
        pair = {"priceUsd": "1", "volume": {"h24": 1000}, "marketCap": 10_000,
                "fdv": 10_000, "liquidity": {"usd": 10_000}}
        result = intelligence.analyze([], pair, timestamp=NOW,
                                      flow={"available": True, "windows": {}})
        self.assertNotIn("whale_flow", result["components"])

    def test_api_unavailable_does_not_create_flow_alert(self):
        state, event = flow.update_flow(None, [], now=NOW, liquidity_usd=10_000)
        self.assertFalse(state["available"])
        self.assertIsNone(event)

    def test_trade_deduplication_ignores_repeated_api_rows(self):
        item = trade("sig", 60, "buy", 100)
        state, _event = flow.update_flow(None, [item], now=NOW, liquidity_usd=1_000_000)
        next_state, event = flow.update_flow(state, [item], now=NOW+60, liquidity_usd=1_000_000)
        self.assertEqual(len(next_state["recent_trades"]), 1)
        self.assertIsNone(event)

    def test_smart_flow_cooldown_is_separate_state(self):
        state, first_event = flow.update_flow(None, [trade("baseline", 800, "buy", 100)], now=NOW,
                                               liquidity_usd=1_000_000, cooldown_seconds=1800)
        self.assertIsNone(first_event)
        state, event = flow.update_flow(state, [trade("a", 30, "buy", 20_000)], now=NOW+60,
                                         liquidity_usd=1_000_000, cooldown_seconds=1800)
        self.assertIsNotNone(event)
        next_state, event2 = flow.update_flow(state, [trade("b", 30, "buy", 20_000)], now=NOW+90,
                                               liquidity_usd=1_000_000, cooldown_seconds=1800)
        self.assertIsNone(event2)
        self.assertEqual(next_state["last_alert_at"], NOW+60)

    def test_new_large_buy_and_sell_create_directional_events_after_baseline(self):
        for side in ("buy", "sell"):
            baseline, _ = flow.update_flow(None, [trade("baseline-"+side, 800, "buy", 100)],
                                           now=NOW, liquidity_usd=1_000_000)
            _state, event = flow.update_flow(baseline, [trade("large-"+side, 30, side, 20_000)],
                                             now=NOW+60, liquidity_usd=1_000_000)
            self.assertEqual(event["side"], side)
            self.assertEqual(event["type"], "large_trade")

    def test_unknown_direction_never_generates_directional_alert(self):
        baseline, _ = flow.update_flow(None, [trade("baseline-unknown", 800, "buy", 100)],
                                       now=NOW, liquidity_usd=1_000_000)
        _state, event = flow.update_flow(baseline, [trade("unknown-large", 30, "unknown", 20_000)],
                                         now=NOW+60, liquidity_usd=1_000_000)
        self.assertIsNone(event)

    def test_first_trade_response_establishes_baseline_without_replaying_old_alerts(self):
        state, event = flow.update_flow(None, [trade("historical-large", 300, "buy", 50_000)],
                                        now=NOW, liquidity_usd=1_000_000)
        self.assertIsNone(event)
        self.assertTrue(state["initialized"])
        self.assertTrue(state["available"])


class WatchlistIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.pair = {"priceUsd": "1", "baseToken": {"address": ADDRESS, "name": "Test", "symbol": "T"},
                     "priceChange": {"h24": 0}, "marketCap": 100_000, "fdv": 100_000,
                     "liquidity": {"usd": 50_000}, "volume": {"h24": 10_000},
                     "poolAddress": "pool-address", "dataSource": "GeckoTerminal"}
        bot.STOP.clear()
        bot.FLOW_CURSOR = 0

    def test_watch_entry_automatically_queries_pool_trades(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "symbol": "T", "anchor_price": 1,
                                      "last_alert": 0, "subscribers": ["11"]}}}
        with patch.object(bot, "get_gecko_pairs", return_value={ADDRESS: self.pair}), \
             patch.object(bot, "get_gecko_trades", return_value=[{"id": "new", "timestamp": time.time()-30,
                                                                    "side": "buy", "usd": 10}]) as fetch, \
             patch.object(bot, "send_message"):
            bot.check_prices(state, "test-token", 100, 1800)
        fetch.assert_called_once_with("pool-address", ADDRESS)
        self.assertTrue(state["watches"][ADDRESS]["flow"]["available"])

    def test_unwatched_token_no_longer_generates_smart_flow_request(self):
        state = {"watches": {}}
        with patch.object(bot, "get_gecko_trades") as fetch:
            bot.check_prices(state, "test-token", 10, 1800)
        fetch.assert_not_called()

    def test_temporary_trade_api_error_does_not_abort_price_monitor(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "anchor_price": 1,
                                      "last_alert": 0, "subscribers": []}}}
        with patch.object(bot, "get_gecko_pairs", return_value={ADDRESS: self.pair}), \
             patch.object(bot, "get_gecko_trades", side_effect=bot.GeckoTerminalError("HTTP 503")), \
             patch.object(bot, "send_message"):
            bot.check_prices(state, "test-token", 10, 1800)
        self.assertEqual(state["watches"][ADDRESS]["last_price"], 1.0)
        self.assertFalse(state["watches"][ADDRESS]["flow"]["available"])

    def test_watch_state_migration_preserves_bounded_flow_data(self):
        path = Path(__file__).resolve().parent / ".flow-state-test.json"
        try:
            path.write_text(json.dumps({"offset": 5, "watches": {ADDRESS: {
                "address": ADDRESS, "subscribers": ["11"], "flow": {"recent_trades": [trade("a", 20, "buy", 5)]}}}}),
                encoding="utf-8")
            with patch.object(bot, "STATE_FILE", path):
                result = bot.load_state()
            self.assertEqual(result["offset"], 5)
            self.assertEqual(result["watches"][ADDRESS]["flow"]["recent_trades"][0]["side"], "buy")
        finally:
            path.unlink(missing_ok=True)

    def test_endpoint_orients_trades_to_the_target_token(self):
        with patch.object(bot, "http_json", return_value={"data": []}) as request:
            bot.get_gecko_trades("pool/id", ADDRESS)
        self.assertIn("/networks/solana/pools/pool%2Fid/trades?token=" + ADDRESS, request.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
