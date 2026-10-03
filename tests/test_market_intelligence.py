import json
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import bot
import market_intelligence as mi

ADDRESS = "So11111111111111111111111111111111111111112"
TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


def history(prices, volumes=None, *, start=None, step=60, liquidity=50_000):
    if start is None:
        start = time.time() - (len(prices) - 1) * step
    volumes = volumes or [10_000] * len(prices)
    return [{"timestamp": start + i * step, "price": price, "volume_24h": volumes[i],
             "market_cap": 100_000, "fdv": 120_000, "liquidity": liquidity, "price_change_24h": 5}
            for i, price in enumerate(prices)]


def pair(price, volume=10_000, liquidity=50_000, change=5):
    return {"priceUsd": str(price), "baseToken": {"address": ADDRESS, "symbol": "ANSEM", "name": "Ansemmas"},
            "priceChange": {"h24": change}, "volume": {"h24": volume}, "marketCap": 100_000,
            "fdv": 120_000, "liquidity": {"usd": liquidity}}


class MarketIntelligenceTests(unittest.TestCase):
    def setUp(self):
        bot.INTELLIGENCE_MARKET_CACHE.clear()
        bot.INTELLIGENCE_WALLET_CACHE.clear()

    def test_return_windows_and_price_acceleration_use_only_observed_history(self):
        rows = history([1 + i * .001 for i in range(100)])
        rows.append(mi.make_snapshot(pair(1.12, 25_000), rows[-1]["timestamp"] + 60))
        result = mi.analyze(rows)
        self.assertIsNotNone(result["metrics"]["return_5m"])
        self.assertIsNotNone(result["metrics"]["return_15m"])
        self.assertIsNotNone(result["metrics"]["price_acceleration"])
        self.assertEqual(result["metrics"]["return_24h"], 5)

    def test_rising_price_with_increasing_volume_scores_and_confirms_breakout(self):
        prices = [1 + i * .001 for i in range(100)]
        rows = history(prices)
        current = mi.make_snapshot(pair(1.12, 25_000), rows[-1]["timestamp"] + 60)
        rows.append(current)
        result = mi.analyze(rows)
        self.assertTrue(result["metrics"]["breakout"])
        self.assertGreater(result["metrics"]["volume_acceleration"], 0)
        self.assertEqual(result["state"], "🔥 BREAKOUT")
        self.assertEqual(result["entry"]["type"], "🔥 BREAKOUT + VOLUME · FLUXO INDISPONÍVEL")
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_breakout_without_volume_confirmation_is_not_confirmed(self):
        rows = history([1 + i * .001 for i in range(100)])
        rows.append(mi.make_snapshot(pair(1.12, 10_000), rows[-1]["timestamp"] + 60))
        result = mi.analyze(rows)
        self.assertTrue(result["metrics"]["false_breakout_risk"])
        self.assertNotEqual(result["state"], "🔥 BREAKOUT")
        self.assertNotEqual(result["entry"]["type"], "🔥 BREAKOUT + VOLUME · FLUXO INDISPONÍVEL")

    def test_falling_price_with_increasing_volume_raises_exit_risk(self):
        prices = [1.0] * 70 + [1 - i * .015 for i in range(1, 31)]
        rows = history(prices, [10_000] * 99 + [30_000])
        rows.append(mi.make_snapshot(pair(.52, 40_000), rows[-1]["timestamp"] + 60))
        result = mi.analyze(rows)
        self.assertGreater(result["exit_risk"], 50)
        self.assertTrue(result["exit_signals"])

    def test_vertical_pump_is_flagged_as_late_entry(self):
        prices = [1.0] * 80 + [1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0,
                               2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.0]
        rows = history(prices)
        rows.append(mi.make_snapshot(pair(3.1, 80_000), rows[-1]["timestamp"] + 60))
        result = mi.analyze(rows)
        self.assertTrue(result["entry"]["late"])
        self.assertEqual(result["entry"]["type"], "🔴 ENTRADA TARDIA")
        self.assertLess(result["entry_score"], result["score"])

    def test_pullback_is_detected_without_fabricating_target_levels(self):
        prices = [1 + i * .01 for i in range(70)] + [1.69, 1.67, 1.65, 1.63, 1.61, 1.60, 1.61]
        rows = history(prices, [10_000] * (len(prices) - 1) + [11_000])
        rows.append(mi.make_snapshot(pair(1.62, 12_000), rows[-1]["timestamp"] + 60))
        result = mi.analyze(rows)
        self.assertIn("PULLBACK", result["entry"]["type"])
        self.assertIsNone(result["entry"]["tp2"])

    def test_support_loss_and_drawdown_raise_exit_risk(self):
        prices = [1.0] * 75 + [1.1, 1.08, 1.06, 1.04, 1.02, 1.0, .98, .96, .94, .92,
                              .90, .88, .86, .84, .82, .80, .78, .76, .74, .72,
                              .70, .68, .66, .64, .62]
        rows = history(prices)
        rows.append(mi.make_snapshot(pair(.58, 20_000), rows[-1]["timestamp"] + 60))
        result = mi.analyze(rows)
        self.assertTrue(result["metrics"]["support_lost"])
        self.assertGreaterEqual(result["exit_risk"], 40)

    def test_insufficient_or_absent_data_never_creates_entry_levels(self):
        empty = mi.analyze([])
        self.assertEqual(empty["state"], "⚪ SEM DADOS")
        short = mi.analyze(history([1, 1.1, 1.2]), pair(1.3))
        self.assertTrue(short["insufficient"])
        self.assertIn("Dados insuficientes", short["entry"]["type"])
        self.assertNotIn("zone", short["entry"])

    def test_low_liquidity_caps_score_and_marks_high_risk(self):
        rows = history([1 + i * .001 for i in range(100)], liquidity=100)
        rows.append(mi.make_snapshot(pair(1.2, liquidity=100), rows[-1]["timestamp"] + 60))
        result = mi.analyze(rows)
        self.assertEqual(result["state"], "☠️ ALTO RISCO")
        self.assertLessEqual(result["score"], 35)
        self.assertEqual(result["entry"]["type"], "Dados insuficientes")

    def test_snapshot_retention_and_signal_outcomes_are_bounded_and_observed(self):
        rows_by_address = {}
        snap1 = mi.append_snapshot(rows_by_address, ADDRESS, pair(1), 1_000_000, retention_hours=1, interval_seconds=60)
        self.assertIsNotNone(snap1)
        self.assertIsNone(mi.append_snapshot(rows_by_address, ADDRESS, pair(1.01), 1_000_030, interval_seconds=60))
        snap2 = mi.append_snapshot(rows_by_address, ADDRESS, pair(1.1), 1_000_060, interval_seconds=60)
        self.assertIsNotNone(snap2)
        signals = []
        result = mi.analyze(rows_by_address[ADDRESS], pair(1.1), timestamp=1_000_060)
        mi.record_signal(signals, ADDRESS, result, snap1)
        mi.update_signal_outcomes(signals, ADDRESS, {"timestamp": 1_000_400, "price": 1.2})
        self.assertEqual(signals[0]["outcomes"]["5m"], 20.0)
        self.assertEqual(signals[0]["max_after"], 1.2)

    def test_market_history_round_trips_with_existing_bot_state(self):
        path = Path(__file__).resolve().parent / ".intelligence-state-test.json"
        try:
            path.write_text(json.dumps({"offset": 3, "watches": {}, "market_history": {ADDRESS: [
                {"timestamp": 1_791_058_000, "price": 1.2, "volume_24h": 3, "market_cap": 4,
                 "fdv": 5, "liquidity": 6, "price_change_24h": 7}]}, "signals": []}), encoding="utf-8")
            with patch.object(bot, "STATE_FILE", path):
                state = bot.load_state()
            self.assertEqual(state["market_history"][ADDRESS][0]["price"], 1.2)
            self.assertEqual(state["offset"], 3)
        finally:
            path.unlink(missing_ok=True)

    def test_scanner_uses_cached_history_and_opportunities_apply_score_cutoff(self):
        prices = [1 + i * .003 for i in range(100)]
        rows = history(prices)
        entry = {"address": ADDRESS, "symbol": "ANSEM", "name": "Ansemmas", "subscribers": ["11"]}
        scan, markup = bot.intelligence_scan_text([entry], {ADDRESS: rows})
        self.assertIn("SCANNER", scan)
        self.assertIn("ANSEM", scan)
        self.assertTrue(markup["inline_keyboard"])
        no_opps, _ = bot.intelligence_scan_text([entry], {ADDRESS: []}, opportunities=True)
        self.assertIn("Nenhum token monitorado", no_opps)

    def test_market_api_failure_for_manual_analysis_returns_friendly_message(self):
        state = {"watches": {}, "market_history": {}, "signals": []}
        with patch.object(bot, "get_market_data", side_effect=bot.MarketDataError("offline")):
            with patch.object(bot, "send_message") as send:
                bot.handle_update({"message": {"chat": {"id": 11}, "from": {"id": 22}, "text": f"/analisar {ADDRESS}"}},
                                  state, TOKEN, 10, set())
        self.assertEqual(send.call_args.args[2], "⚠️ Dados de mercado indisponíveis.")

    def test_menu_routes_and_shortcut_commands_are_available(self):
        self.assertEqual(bot.parse_message("/analisar " + ADDRESS)[0], "/analyze")
        self.assertEqual(bot.parse_message("/scanner")[0], "/scanner")
        self.assertEqual(bot.parse_message("/oportunidades")[0], "/opportunities")
        self.assertIn("intel_scanner", str(bot.main_menu_markup()))
        self.assertIn("intel_performance", str(bot.main_menu_markup()))

    def test_smart_alert_sends_only_on_relevant_state_change_and_keeps_short_header(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "subscribers": ["11"], "anchor_price": 100,
                                       "last_alert": 0}}, "market_history": {}, "signals": []}
        pair_value = pair(101, liquidity=100)
        bot.STOP.clear()
        with patch.object(bot, "get_gecko_pairs", return_value={ADDRESS: pair_value}):
            with patch.object(bot, "send_message") as send:
                bot.check_prices(state, TOKEN, 1000, 1800)
                bot.check_prices(state, TOKEN, 1000, 1800)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2].splitlines()[0], "🟢🤑 +1.00% • ANSEM")
        self.assertIn("ALTO RISCO", send.call_args.args[2])

    def test_full_analysis_command_returns_real_context_and_observed_history(self):
        rows = history([1 + i * .001 for i in range(100)])
        state = {"watches": {ADDRESS: {"address": ADDRESS, "symbol": "ANSEM", "name": "Ansemmas",
                                       "subscribers": ["11"]}},
                 "market_history": {ADDRESS: rows}, "signals": []}
        with patch.object(bot, "get_market_data", return_value=pair(1.12, 25_000)):
            with patch.object(bot, "send_message") as send:
                bot.handle_update({"message": {"chat": {"id": 11}, "from": {"id": 22},
                                                "text": f"/analisar {ADDRESS}"}}, state, TOKEN, 10, set())
        text = send.call_args.args[2]
        self.assertIn("ANSEM", text)
        self.assertIn("Score:", text)
        self.assertIn("Confiança dos dados", text)
        self.assertIn("não são probabilidade de lucro", text)

    def test_analysis_includes_public_wallet_position_when_balance_is_available(self):
        state = {"watches": {}, "wallets": {"22": {"address": ADDRESS, "chat_id": "11"}},
                 "market_history": {}, "signals": []}
        with patch.object(bot, "get_market_data", return_value=pair(1.12)):
            with patch.object(bot.solana_wallet, "get_token_balance", return_value=(Decimal("2.5"), 6)):
                text, _ = bot.intelligence_single_screen(state, 11, ADDRESS, user_id="22")
        self.assertIn("💼 SUA POSIÇÃO", text)
        self.assertIn("Quantidade: 2.5", text)
        self.assertIn("PnL: indisponível", text)

    def test_telegram_intelligence_callbacks_open_scanner_analysis_and_performance(self):
        rows = history([1 + i * .001 for i in range(100)])
        entry = {"address": ADDRESS, "symbol": "ANSEM", "name": "Ansemmas", "subscribers": ["11"]}
        state = {"watches": {ADDRESS: entry}, "market_history": {ADDRESS: rows}, "signals": [], "wallets": {}}

        def callback(data):
            return {"callback_query": {"id": "cb", "from": {"id": 22}, "data": data,
                    "message": {"message_id": 7, "chat": {"id": 11}}}}

        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message") as edit:
            bot.handle_callback_update(callback("intel_scanner"), state, TOKEN, 10, set())
            self.assertIn("SCANNER", edit.call_args.args[3])
            bot.handle_callback_update(callback("intel_analyze_menu"), state, TOKEN, 10, set())
            self.assertIn("Escolha um token", edit.call_args.args[3])
            with patch.object(bot, "get_market_data", return_value=pair(1.12, 25_000)):
                bot.handle_callback_update(callback(f"ia:{ADDRESS}"), state, TOKEN, 10, set())
            self.assertIn("ANSEM", edit.call_args.args[3])
            bot.handle_callback_update(callback("intel_performance"), state, TOKEN, 10, set())
            self.assertIn("PERFORMANCE", edit.call_args.args[3])

    def test_scanner_includes_priced_public_wallet_assets_without_fake_history(self):
        mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        state = {"watches": {}, "wallets": {"22": {"address": ADDRESS, "chat_id": "11"}},
                 "market_history": {}, "signals": []}
        bot.INTELLIGENCE_WALLET_CACHE.pop(ADDRESS, None)
        wallet_info = {"tokens": [{"mint": mint, "amount": "5", "name": "USD Coin", "symbol": "USDC",
                                    "price_usd": "1", "market_cap": 1000000, "fdv": 1100000,
                                    "liquidity_usd": 500000, "volume_24h": 100000,
                                    "price_change_24h": .1}]}
        with patch.object(bot.solana_wallet, "get_wallet_info", return_value=wallet_info):
            entries = bot.intelligence_entries_for_chat(state, 11, "22")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["address"], mint)
        text, _ = bot.intelligence_scan_text(entries, {})
        self.assertIn("USDC", text)
        self.assertIn("Histórico insuficiente", text)
        opportunity, _ = bot.intelligence_scan_text(entries, {}, opportunities=True)
        self.assertIn("Nenhum token monitorado", opportunity)


if __name__ == "__main__":
    unittest.main()
