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

    def test_native_telegram_command_menu_is_portuguese_and_contains_only_requested_commands(self):
        with patch.object(bot, "telegram", return_value=True) as request:
            bot.configure_bot_commands(TOKEN)
        self.assertEqual(request.call_args.args[1], "setMyCommands")
        commands = request.call_args.args[2]["commands"]
        self.assertEqual([command["command"] for command in commands], ["start", "price", "list", "watch", "unwatch", "help"])
        self.assertEqual([command["description"] for command in commands],
                         ["Abrir menu", "Consultar preço", "Minha lista", "Adicionar alerta", "Remover alerta", "Ajuda"])


class AddressAndParserTests(unittest.TestCase):
    def test_base58_public_key_validation_and_parser(self):
        self.assertTrue(bot.is_valid_solana_address(ADDRESS))
        self.assertTrue(bot.is_valid_solana_address("CwSNUU54NLJt4qbGg5S8TUzJYxjYAfL4VrPr7zvJpump"))
        for invalid in (ADDRESS + " ", ADDRESS[:4] + "0" + ADDRESS[5:], "abc", "1" * 31):
            self.assertFalse(bot.is_valid_solana_address(invalid))
        self.assertEqual(bot.parse_message(ADDRESS), ("/price", ADDRESS))
        self.assertEqual(bot.parse_message("/price " + ADDRESS), ("/price", ADDRESS))
        self.assertEqual(bot.parse_message("/preco " + ADDRESS), ("/price", ADDRESS))
        self.assertEqual(bot.parse_message("/monitorar " + ADDRESS), ("/watch", ADDRESS))
        self.assertEqual(bot.parse_message("/adicionar " + ADDRESS), ("/watch", ADDRESS))
        self.assertEqual(bot.parse_message("/remover " + ADDRESS), ("/unwatch", ADDRESS))
        self.assertEqual(bot.parse_message("/lista"), ("/list", ""))


class MarketDataTests(unittest.TestCase):
    def test_professional_number_formatting_and_percent_icons(self):
        self.assertEqual(bot.money("1.25"), "$1.25")
        self.assertEqual(bot.money("0.001254"), "$0.001254")
        self.assertEqual(bot.money("0.00000123"), "$0.00000123")
        self.assertEqual(bot.money("1.23e-24"), "$0.00000000000000000000000123")
        self.assertEqual(bot.compact(950), "$950")
        self.assertEqual(bot.compact(12400), "$12.4K")
        self.assertEqual(bot.compact(1250000), "$1.25M")
        self.assertEqual(bot.compact(1250000000), "$1.25B")
        self.assertEqual(bot.percent(12.45), "🟢🤑 +12.45%")
        self.assertEqual(bot.percent(-12.45), "🔴🫠 -12.45%")
        self.assertEqual(bot.percent(0.001), "⚪ 0.00%")
        self.assertEqual(bot.change_icon(12.45), "💰")
        self.assertEqual(bot.change_icon(-12.45), "💰")

    def test_fdv_is_labelled_when_market_cap_is_missing(self):
        pair = dict(DEX_PAIR, marketCap=None)
        summary = bot.pair_summary(pair)
        self.assertIn("💎 FDV", summary)
        self.assertNotIn("💎 Market Cap", summary)

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
        for expected in ("Wrapped SOL", "SOL", "$150.25", "+1.50%", "$100K", "$25K", "$900K", "$1M", bot.short_address(ADDRESS), "Solana", "Capitalização"):
            self.assertIn(expected, summary)
        self.assertNotIn(ADDRESS, summary)

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
            with patch.object(bot, "send_message", side_effect=lambda *args, **kwargs: sent.append(args[2])):
                bot.handle_update({"message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/price " + ADDRESS}}, state, TOKEN, 10, set())
        self.assertEqual(sent, ["Não consegui consultar o preço agora. As fontes de mercado estão temporariamente indisponíveis. Tente novamente em alguns segundos."])


class TelegramCommandTests(unittest.TestCase):
    def update(self, text, chat=11, user=22):
        return {"message": {"chat": {"id": chat}, "from": {"id": user}, "text": text}}

    def callback(self, data, chat=11, user=22, callback_id="cb-1"):
        return {"callback_query": {"id": callback_id, "from": {"id": user}, "data": data,
                                   "message": {"message_id": 99, "chat": {"id": chat}}}}

    def test_start_help_price_by_address_and_bare_address(self):
        sent, state = [], {"watches": {}}
        with patch.object(bot, "get_market_data", return_value=DEX_PAIR) as market:
            with patch.object(bot, "send_message", side_effect=lambda *args, **kwargs: sent.append(args[2])):
                bot.handle_update(self.update("/start"), state, TOKEN, 10, set())
                bot.handle_update(self.update("/help"), state, TOKEN, 10, set())
                bot.handle_update(self.update("/price " + ADDRESS), state, TOKEN, 10, set())
                bot.handle_update(self.update(ADDRESS), state, TOKEN, 10, set())
        self.assertIn("0x_LSR CRYPTO ALERTS", sent[0])
        self.assertIn("/price", sent[1])
        self.assertIn("SOL", sent[2])
        self.assertIn("SOL", sent[3])
        self.assertEqual(market.call_count, 2)

    def test_start_menu_is_clean_portuguese_and_contains_no_technical_commands(self):
        with patch.object(bot, "send_message") as send:
            bot.handle_update(self.update("/start"), {"watches": {}}, TOKEN, 10, set())
        text, markup = send.call_args.args[2:4]
        self.assertIn("Seu painel de tokens Solana", text)
        for command in ("/price", "/watch", "/unwatch", "/list", "/help"):
            self.assertNotIn(command, text)
        labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
        self.assertEqual(labels, ["💰 Consultar preço", "🔔 Meus alertas", "➕ Adicionar token", "📋 Minha lista", "💼 Minha carteira", "ℹ️ Ajuda"])

    def test_menu_alerts_list_and_back_callbacks_edit_same_message_and_answer_every_callback(self):
        state = {"watches": {
            ADDRESS: {"address": ADDRESS, "name": "Wrapped SOL", "symbol": "SOL", "subscribers": ["11"], "last_price": 1.0},
            "9" + ADDRESS: {"address": "9" + ADDRESS, "name": "Rizzmas", "symbol": "RIZZ", "subscribers": ["11"], "last_price": 2.0},
        }}
        with patch.object(bot, "answer_callback") as answer, patch.object(bot, "edit_message") as edit, patch.object(bot, "save_state"):
            for index, data in enumerate(("alerts", "watch_list", "menu", "help", "price_menu", "remove_menu", "add_begin")):
                bot.handle_callback_update(self.callback(data, callback_id=f"cb-{index}"), state, TOKEN, 10, set())
        self.assertEqual(answer.call_count, 7)
        self.assertEqual(edit.call_count, 7)
        alert_call = edit.call_args_list[0]
        self.assertIn("Limite: ±10%", alert_call.args[3])
        list_call = edit.call_args_list[1]
        self.assertIn("RIZZ", list_call.args[3])
        self.assertNotIn(ADDRESS, list_call.args[3])
        self.assertIn("◀️ Voltar", str(list_call.args[4]))

    def test_refresh_token_callback_edits_the_existing_message(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "symbol": "SOL", "subscribers": ["11"]}}}
        lookup_id = bot.token_callback_id(ADDRESS)
        bot.PRICE_LOOKUPS[lookup_id] = ADDRESS
        with patch.object(bot, "answer_callback") as answer, patch.object(bot, "get_market_data", return_value=DEX_PAIR) as market, patch.object(bot, "edit_message") as edit:
            bot.handle_callback_update(self.callback(f"u:{lookup_id}:p"), state, TOKEN, 10, set())
        answer.assert_called_once()
        market.assert_called_once_with(ADDRESS)
        edit.assert_called_once()
        self.assertEqual(edit.call_args.args[2], 99)

    def test_watch_list_unwatch_persist_and_scope_by_chat(self):
        state = {"offset": 0, "watches": {}}
        sent = []
        with patch.object(bot, "get_market_data", return_value=DEX_PAIR):
            with patch.object(bot, "save_state") as save:
                with patch.object(bot, "send_message", side_effect=lambda *args, **kwargs: sent.append(args[2])):
                    bot.handle_update(self.update("/watch " + ADDRESS), state, TOKEN, 8, set())
                    bot.handle_update(self.update("/list"), state, TOKEN, 8, set())
                    bot.handle_update(self.update("/list", chat=99), state, TOKEN, 8, set())
                    bot.handle_update(self.update("/unwatch " + ADDRESS), state, TOKEN, 8, set())
        self.assertIn("TOKEN ADICIONADO", sent[0])
        self.assertIn("Wrapped SOL", sent[1])
        self.assertNotIn(ADDRESS, sent[1])
        self.assertEqual(sent[2], "📋 MINHA LISTA\n\nSua lista ainda está vazia.")
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
        self.assertEqual(send.call_args.args[2].splitlines()[0], "🟢🤑 +20.00% • SOL")
        self.assertIn("🟢🤑 Wrapped SOL", send.call_args.args[2])
        self.assertEqual(state["watches"][ADDRESS]["last_price"], 140.0)
        self.assertEqual(state["watches"][ADDRESS]["anchor_price"], 120.0)

    def test_alert_visual_uses_directional_emoji_pair_for_positive_and_negative_moves(self):
        for price, header, direction in ((110.48, "+10.48%", "🟢🤑"), (89.52, "-10.48%", "🔴🫠")):
            with self.subTest(price=price):
                pair = dict(DEX_PAIR, priceUsd=str(price))
                state = {"watches": {ADDRESS: {"address": ADDRESS, "subscribers": ["11"], "anchor_price": 100, "last_alert": 0}}}
                bot.STOP.clear()
                with patch.object(bot, "get_gecko_pairs", return_value={ADDRESS: pair}):
                    with patch.object(bot, "send_message") as send:
                        bot.check_prices(state, TOKEN, 10, 1800)
                alert = send.call_args.args[2]
                self.assertEqual(alert.splitlines()[0], f"{direction} {header} • SOL")
                self.assertIn(f"\n\n━━━━━━━━━━━━━━━━━━━━\n\n{direction} Wrapped SOL", alert)

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
        with patch.object(bot, "send_message", side_effect=lambda *args, **kwargs: sent.append(args[2])) as send:
            bot.handle_update(self.update("/list"), {"watches": watches}, TOKEN, 10, set())
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(len(text) <= 4096 for text in sent))
        self.assertEqual(len(send.call_args.args[3]["inline_keyboard"]), 13)

    def test_price_without_argument_opens_only_this_chats_inline_token_menu(self):
        state = {"watches": {
            ADDRESS: {"address": ADDRESS, "symbol": "SOL", "subscribers": ["11"]},
            "9" + ADDRESS: {"address": "9" + ADDRESS, "symbol": "OTHER", "subscribers": ["99"]},
        }}
        with patch.object(bot, "send_message") as send:
            bot.handle_update(self.update("/price"), state, TOKEN, 10, set())
        self.assertIn("CONSULTAR PREÇO", send.call_args.args[2])
        self.assertIn("Selecione um token", send.call_args.args[2])
        markup = send.call_args.args[3]
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        self.assertEqual(len(buttons), 3)  # token + refresh + back
        self.assertIn("SOL", buttons[0]["text"])
        self.assertNotIn("OTHER", str(markup))
        callback_data = buttons[0]["callback_data"]
        self.assertLessEqual(len(callback_data.encode()), 64)
        self.assertNotIn(ADDRESS, callback_data)

    def test_token_selection_callback_answers_and_sends_current_price_with_link_buttons(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "symbol": "SOL", "subscribers": ["11"]}}}
        with patch.object(bot, "answer_callback") as answer:
            with patch.object(bot, "get_market_data", return_value=DEX_PAIR) as market:
                with patch.object(bot, "edit_message") as edit:
                    bot.handle_callback_update(self.callback("p:" + bot.token_callback_id(ADDRESS)), state, TOKEN, 10, set())
        answer.assert_called_once_with(TOKEN, "cb-1", None)
        market.assert_called_once_with(ADDRESS)
        self.assertIn("$150.25", edit.call_args.args[3])
        markup = edit.call_args.args[4]
        self.assertIn("🔄 Atualizar", str(markup))
        self.assertIn("◀️ Voltar", str(markup))
        self.assertIn("https://dexscreener.com/solana/example", str(markup))
        self.assertIn("geckoterminal.com/solana/tokens/", str(markup))

    def test_refresh_callback_answers_and_rebuilds_price_menu(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "symbol": "SOL", "subscribers": ["11"]}}}
        with patch.object(bot, "answer_callback") as answer:
            with patch.object(bot, "edit_message") as edit:
                bot.handle_callback_update(self.callback("p_refresh:0"), state, TOKEN, 10, set())
        answer.assert_called_once()
        self.assertIn("CONSULTAR PREÇO", edit.call_args.args[3])

    def test_remove_callback_confirms_then_removes_only_current_chat(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "name": "Wrapped SOL", "subscribers": ["11", "99"]}}}
        callback_key = bot.token_callback_id(ADDRESS)
        with patch.object(bot, "answer_callback") as answer:
            with patch.object(bot, "edit_message") as edit:
                bot.handle_callback_update(self.callback("r:" + callback_key), state, TOKEN, 10, set())
                self.assertIn("Deseja remover Wrapped SOL", edit.call_args.args[3])
                confirm_markup = edit.call_args.args[4]
                confirm_data = confirm_markup["inline_keyboard"][0][0]["callback_data"]
                bot.handle_callback_update(self.callback(confirm_data, callback_id="cb-2"), state, TOKEN, 10, set())
        self.assertEqual(answer.call_count, 2)
        self.assertEqual(state["watches"][ADDRESS]["subscribers"], ["99"])

    def test_add_callback_flow_previews_confirms_and_persists_token(self):
        state = {"watches": {}, "awaiting_add": {}, "pending_add": {}}
        with patch.object(bot, "save_state") as save:
            with patch.object(bot, "answer_callback") as answer:
                with patch.object(bot, "send_message") as send, patch.object(bot, "edit_message") as edit:
                    bot.handle_callback_update(self.callback("add_begin"), state, TOKEN, 10, set())
                    self.assertIn("Envie o endereço", edit.call_args.args[3])
                    with patch.object(bot, "get_market_data", return_value=DEX_PAIR) as market:
                        bot.handle_update(self.update(ADDRESS), state, TOKEN, 10, set())
                        preview_markup = send.call_args.args[3]
                        confirm_data = preview_markup["inline_keyboard"][0][0]["callback_data"]
                        self.assertIn("TOKEN ENCONTRADO", send.call_args.args[2])
                        bot.handle_callback_update(self.callback(confirm_data, callback_id="cb-2"), state, TOKEN, 10, set())
                    self.assertIn("Adicionado aos seus alertas", edit.call_args.args[3])
        self.assertEqual(market.call_count, 2)
        self.assertEqual(answer.call_count, 2)
        self.assertEqual(state["watches"][ADDRESS]["subscribers"], ["11"])
        self.assertGreaterEqual(save.call_count, 3)

    def test_callback_is_answered_even_when_action_is_stale_or_unknown(self):
        with patch.object(bot, "answer_callback") as answer:
            with patch.object(bot, "edit_message"):
                bot.handle_callback_update(self.callback("unknown_action"), {"watches": {}}, TOKEN, 10, set())
        answer.assert_called_once_with(TOKEN, "cb-1", None)

    def test_list_empty_and_price_token_not_found_have_clear_responses(self):
        with patch.object(bot, "send_message") as send:
            bot.handle_update(self.update("/list"), {"watches": {}}, TOKEN, 10, set())
        self.assertIn("lista ainda está vazia", send.call_args_list[0].args[2])
        with patch.object(bot, "get_market_data", return_value=None):
            with patch.object(bot, "send_message") as missing:
                bot.handle_update(self.update("/price " + ADDRESS), {"watches": {}}, TOKEN, 10, set())
        self.assertIn("não encontrado", missing.call_args.args[2])


class PollingTests(unittest.TestCase):
    def test_state_migration_keeps_existing_watch_reference_and_subscribers(self):
        path = Path(__file__).resolve().parent / ".state-migration-test.json"
        legacy = {"offset": 41, "watches": {ADDRESS.lower(): {"address": ADDRESS, "subscribers": ["11"], "anchor_price": "0.5", "last_alert": 123}}}
        try:
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with patch.object(bot, "STATE_FILE", path):
                migrated = bot.load_state()
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(migrated["offset"], 41)
        self.assertIn(ADDRESS, migrated["watches"])
        entry = migrated["watches"][ADDRESS]
        self.assertEqual(entry["subscribers"], ["11"])
        self.assertEqual(entry["anchor_price"], "0.5")
        self.assertEqual(entry["last_alert"], 123)
        self.assertIn("timestamp", entry)

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
        self.assertEqual(get_updates["allowed_updates"], ["message", "callback_query"])
        self.assertEqual(state["offset"], 33)


if __name__ == "__main__":
    unittest.main()
