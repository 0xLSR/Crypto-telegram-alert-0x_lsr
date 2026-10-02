import io
import json
import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import bot


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
ADDRESS = "So11111111111111111111111111111111111111112"
GECKO_RESPONSE = {
    "data": {"id": "solana_token", "type": "token", "attributes": {
        "address": ADDRESS, "name": "Wrapped SOL", "symbol": "SOL", "price_usd": "150.25",
        "fdv_usd": "1000000", "market_cap_usd": "900000", "total_reserve_in_usd": "100000",
        "volume_usd": {"h24": "25000"}},
        "relationships": {"top_pools": {"data": [{"id": "solana_pool", "type": "pool"}]}}},
    "included": [{"id": "solana_pool", "type": "pool", "attributes": {
        "address": "PoolAddress", "price_change_percentage": {"h24": "1.5"},
        "volume_usd": {"h24": "25000"}, "reserve_in_usd": "100000"}}],
}
DEX_PAIR = {
    "chainId": "solana", "baseToken": {"address": ADDRESS, "name": "Wrapped SOL", "symbol": "SOL"},
    "priceUsd": "150.25", "priceChange": {"h24": "1.5"}, "liquidity": {"usd": 100000},
    "volume": {"h24": 25000}, "marketCap": 900000, "fdv": 1000000,
    "url": "https://dexscreener.com/solana/example",
}


class TokenValidationTests(unittest.TestCase):
    def test_missing_empty_whitespace_and_malformed_tokens_fail_without_network(self):
        for value in (None, "", " bad-token ", "not-a-bot-token"):
            environment = {} if value is None else {"TELEGRAM_BOT_TOKEN": value}
            with self.subTest(value="absent" if value is None else "invalid"):
                with patch.dict(os.environ, environment, clear=True):
                    with patch.object(bot, "validate_telegram") as validate:
                        self.assertEqual(bot.main(), 2)
                validate.assert_not_called()

    def test_telegram_401_is_explicit_and_never_logs_token(self):
        raw = json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
        error = HTTPError(f"https://api.telegram.org/bot{TOKEN}/getMe", 401, "Unauthorized", {}, io.BytesIO(raw))
        with self.assertLogs(bot.LOG, level=logging.ERROR) as logs:
            with patch.object(bot.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(bot.TelegramUnauthorized):
                    bot.telegram(TOKEN, "getMe")
        self.assertIn("401", "\n".join(logs.output))
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_api_401_json_error_is_typed_as_unauthorized(self):
        with patch.object(bot, "http_json", return_value={"ok": False, "error_code": 401, "description": "Unauthorized"}):
            with self.assertRaises(bot.TelegramUnauthorized):
                bot.telegram(TOKEN, "getMe")

    def test_preflight_getme_logs_username_and_removes_webhook_without_dropping_updates(self):
        with patch.object(bot, "telegram", side_effect=[{"id": 42, "username": "sample_bot"}, True]) as request:
            with self.assertLogs(bot.LOG, level=logging.INFO) as logs:
                bot.validate_telegram(TOKEN)
        self.assertIn("@sample_bot", "\n".join(logs.output))
        self.assertEqual(request.call_args_list[1].args, (TOKEN, "deleteWebhook", {"drop_pending_updates": False}))


class AddressAndParserTests(unittest.TestCase):
    def test_base58_public_key_validation_and_parser(self):
        self.assertTrue(bot.is_valid_solana_address(ADDRESS))
        self.assertTrue(bot.is_valid_solana_address("CwSNUU54NLJt4qbGg5S8TUzJYxjYAfL4VrPr7zvJpump"))
        for invalid in (ADDRESS + " ", ADDRESS[:4] + "0" + ADDRESS[5:], "abc", "1" * 31):
            self.assertFalse(bot.is_valid_solana_address(invalid))
        self.assertEqual(bot.parse_message(ADDRESS), ("/price", ADDRESS))
        self.assertEqual(bot.parse_message("/price " + ADDRESS), ("/price", ADDRESS))


class MarketDataTests(unittest.TestCase):
    def test_geckoterminal_documented_endpoint_and_response_shape(self):
        with patch.object(bot, "http_json", return_value=GECKO_RESPONSE) as request:
            pair = bot.get_gecko_pair(ADDRESS)
        self.assertIn("/api/v2/networks/solana/tokens/" + ADDRESS + "?include=top_pools", request.call_args.args[0])
        self.assertEqual(pair["priceUsd"], "150.25")
        self.assertEqual(pair["priceChange"]["h24"], "1.5")
        self.assertEqual(pair["marketCap"], "900000")
        self.assertEqual(pair["fdv"], "1000000")
        self.assertEqual(pair["dataSource"], "GeckoTerminal")

    def test_geckoterminal_success_is_primary_and_summary_contains_market_metrics(self):
        with patch.object(bot, "get_gecko_pair", return_value=bot.normalize_gecko_token(GECKO_RESPONSE["data"], GECKO_RESPONSE["included"])) as gecko:
            with patch.object(bot, "get_dexscreener_pair") as dex:
                pair = bot.get_market_data(ADDRESS)
        gecko.assert_called_once_with(ADDRESS)
        dex.assert_not_called()
        summary = bot.pair_summary(pair)
        for expected in ("Wrapped SOL", "SOL", "$150.25", "+1.50%", "$100,000", "$25,000", "$900,000", ADDRESS, "geckoterminal.com"):
            self.assertIn(expected, summary)

    def test_gecko_failure_falls_back_to_dexscreener(self):
        with patch.object(bot, "get_gecko_pair", side_effect=bot.GeckoTerminalError("offline")):
            with patch.object(bot, "get_dexscreener_pair", return_value=DEX_PAIR) as dex:
                self.assertEqual(bot.get_market_data(ADDRESS), DEX_PAIR)
        dex.assert_called_once_with(ADDRESS)

    def test_both_sources_down_raise_friendly_market_error(self):
        with patch.object(bot, "get_gecko_pair", side_effect=bot.GeckoTerminalError("offline")):
            with patch.object(bot, "get_dexscreener_pair", side_effect=bot.DexScreenerError("offline")):
                with self.assertRaises(bot.MarketDataError):
                    bot.get_market_data(ADDRESS)

    def test_gecko_unavailable_user_gets_requested_friendly_message(self):
        state, sent = {"watches": {}}, []
        with patch.object(bot, "get_market_data", side_effect=bot.MarketDataError("offline")):
            with patch.object(bot, "send_message", side_effect=lambda _token, _chat, text: sent.append(text)):
                bot.handle_update({"message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/price " + ADDRESS}}, state, TOKEN, 10, set())
        self.assertEqual(sent, ["Não consegui consultar o preço agora. As fontes de mercado estão temporariamente indisponíveis. Tente novamente em alguns segundos."])


class TelegramCommandTests(unittest.TestCase):
    def update(self, text, chat=11, user=22):
        return {"message": {"chat": {"id": chat}, "from": {"id": user}, "text": text}}

    def test_start_help_price_by_address_and_bare_address(self):
        sent, state = [], {"watches": {}}
        with patch.object(bot, "get_market_data", return_value=DEX_PAIR) as market:
            with patch.object(bot, "send_message", side_effect=lambda _token, _chat, text: sent.append(text)):
                bot.handle_update(self.update("/start"), state, TOKEN, 10, set())
                bot.handle_update(self.update("/help"), state, TOKEN, 10, set())
                bot.handle_update(self.update("/price " + ADDRESS), state, TOKEN, 10, set())
                bot.handle_update(self.update(ADDRESS), state, TOKEN, 10, set())
        self.assertIn("Bot de alertas", sent[0])
        self.assertIn("/price", sent[1])
        self.assertIn("SOL", sent[2])
        self.assertIn("SOL", sent[3])
        self.assertEqual(market.call_count, 2)

    def test_watch_list_unwatch_persist_and_scope_by_chat(self):
        state = {"offset": 0, "watches": {}}
        sent = []
        with patch.object(bot, "get_market_data", return_value=DEX_PAIR):
            with patch.object(bot, "save_state") as save:
                with patch.object(bot, "send_message", side_effect=lambda _token, _chat, text: sent.append(text)):
                    bot.handle_update(self.update("/watch " + ADDRESS), state, TOKEN, 8, set())
                    bot.handle_update(self.update("/list"), state, TOKEN, 8, set())
                    bot.handle_update(self.update("/list", chat=99), state, TOKEN, 8, set())
                    bot.handle_update(self.update("/unwatch " + ADDRESS), state, TOKEN, 8, set())
        self.assertIn("Monitoramento ativado", sent[0])
        self.assertIn("Wrapped SOL", sent[1])
        self.assertIn("+1.50%", sent[1])
        self.assertIn(ADDRESS, sent[1])
        self.assertEqual(sent[2], "📊 Seus tokens monitorados:\nVocê ainda não está monitorando nenhum token.")
        self.assertEqual(sent[3], "Monitoramento removido.")
        self.assertEqual(state["watches"], {})
        self.assertEqual(save.call_count, 2)

    def test_periodic_monitor_refreshes_price_and_respects_alert_threshold_and_cooldown(self):
        pair = dict(DEX_PAIR)
        pair["priceUsd"] = "120"
        second_pair = dict(pair)
        second_pair["priceUsd"] = "140"
        state = {"watches": {ADDRESS: {"address": ADDRESS, "subscribers": ["11"], "anchor_price": 100, "last_alert": 0}}}
        bot.STOP.clear()
        with patch.object(bot, "get_gecko_pairs", side_effect=[{ADDRESS: pair}, {ADDRESS: second_pair}]) as request:
            with patch.object(bot, "get_dexscreener_pair") as fallback:
                with patch.object(bot, "send_message") as send:
                    bot.check_prices(state, TOKEN, 10, 1800)
                    bot.check_prices(state, TOKEN, 10, 1800)
        request.assert_called_with([ADDRESS])
        fallback.assert_not_called()
        send.assert_called_once()
        self.assertEqual(state["watches"][ADDRESS]["last_price"], 140.0)
        self.assertEqual(state["watches"][ADDRESS]["anchor_price"], 120.0)

    def test_periodic_monitor_falls_back_to_dex_after_gecko_failure(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "subscribers": [], "anchor_price": 100, "last_alert": 0}}}
        bot.STOP.clear()
        with patch.object(bot, "get_gecko_pairs", side_effect=bot.GeckoTerminalError("offline")):
            with patch.object(bot, "get_dexscreener_pair", return_value=DEX_PAIR) as fallback:
                bot.check_prices(state, TOKEN, 1000, 1800)
        fallback.assert_called_once_with(ADDRESS)
        self.assertEqual(state["watches"][ADDRESS]["last_price"], 150.25)

    def test_list_paginates_100_watches_under_telegram_message_limit(self):
        watches = {}
        for index in range(100):
            address = f"{index:03d}" + ADDRESS
            watches[address] = {"address": address, "name": f"Token {index}", "symbol": f"T{index}", "last_price": 0.000123, "subscribers": ["11"]}
        sent = []
        with patch.object(bot, "send_message", side_effect=lambda _token, _chat, text: sent.append(text)):
            bot.handle_update(self.update("/list"), {"watches": watches}, TOKEN, 10, set())
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(text) <= 4096 for text in sent))


class PollingTests(unittest.TestCase):
    def test_six_hour_workflow_settings_and_token_secret_wiring_are_preserved(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "telegram-bot.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "0 0,6,12,18 * * *"', workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("timeout-minutes: 360", workflow)
        self.assertIn('RUN_FOR_SECONDS: "21000"', workflow)
        self.assertIn("TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}", workflow)

    def test_401_preflight_stops_instead_of_retrying_forever(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}, clear=True):
            with patch.object(bot, "validate_telegram", side_effect=bot.TelegramUnauthorized("HTTP 401 Unauthorized")):
                with patch.object(bot, "load_state", return_value={"offset": 0, "watches": {}}):
                    with patch.object(bot.signal, "signal"):
                        with self.assertLogs(bot.LOG, level=logging.ERROR) as logs:
                            self.assertEqual(bot.main(), 2)
        self.assertIn("TELEGRAM_BOT_TOKEN inválido ou rejeitado pelo Telegram.", "\n".join(logs.output))
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_initial_poll_uses_safe_offset_zero(self):
        state = {"offset": 987654321, "watches": {}}
        update = {"update_id": 32, "message": {"chat": {"id": 1}, "from": {"id": 2}, "text": "/help"}}
        calls = []
        def fake_telegram(token, method, payload=None):
            calls.append((method, payload))
            if method == "getUpdates":
                bot.STOP.set()
                return [update]
            return True
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}, clear=True):
            with patch.object(bot, "validate_telegram"):
                with patch.object(bot, "telegram", side_effect=fake_telegram):
                    with patch.object(bot, "load_state", return_value=state):
                        with patch.object(bot, "save_state"):
                            with patch.object(bot, "send_message"):
                                with patch.object(bot.signal, "signal"):
                                    self.assertEqual(bot.main(), 0)
        get_updates = next(payload for method, payload in calls if method == "getUpdates")
        self.assertEqual(get_updates["offset"], 0)
        self.assertEqual(get_updates["timeout"], 1)
        self.assertEqual(state["offset"], 33)


if __name__ == "__main__":
    unittest.main()
