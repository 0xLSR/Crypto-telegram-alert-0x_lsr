import io
import logging
import os
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import bot


ADDRESS = "So11111111111111111111111111111111111111112"
PAIR = {
    "chainId": "solana",
    "baseToken": {"address": ADDRESS, "name": "Wrapped SOL", "symbol": "SOL"},
    "priceUsd": "150.25",
    "priceChange": {"h24": "1.5"},
    "liquidity": {"usd": 100000},
    "volume": {"h24": 25000},
    "marketCap": 1000000,
    "url": "https://dexscreener.com/solana/example",
}


class SolanaAddressTests(unittest.TestCase):
    def test_valid_base58_public_keys(self):
        self.assertTrue(bot.is_valid_solana_address(ADDRESS))
        self.assertTrue(bot.is_valid_solana_address("CwSNUU54NLJt4qbGg5S8TUzJYxjYAfL4VrPr7zvJpump"))

    def test_rejects_whitespace_bad_alphabet_and_wrong_decoded_length(self):
        for address in (ADDRESS + " ", " " + ADDRESS, ADDRESS[:5] + "0" + ADDRESS[6:], "abc", "1" * 31):
            with self.subTest(address=address):
                self.assertFalse(bot.is_valid_solana_address(address))

    def test_parser_commands_aliases_and_bare_address(self):
        cases = {
            "/price " + ADDRESS: ("/price", ADDRESS),
            "/preço " + ADDRESS: ("/price", ADDRESS),
            "/watch " + ADDRESS: ("/watch", ADDRESS),
            "/add " + ADDRESS: ("/watch", ADDRESS),
            "/unwatch " + ADDRESS: ("/unwatch", ADDRESS),
            "/remove " + ADDRESS: ("/unwatch", ADDRESS),
            "/list": ("/list", ""),
            "/lista": ("/list", ""),
            ADDRESS: ("/price", ADDRESS),
            "/help@my_bot": ("/help", ""),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(bot.parse_message(raw), expected)
        self.assertEqual(bot.parse_message("not an address"), (None, ""))


class DexScreenerTests(unittest.TestCase):
    def test_address_lookup_uses_token_pairs_and_normalizes_response(self):
        with patch.object(bot, "http_json", return_value=[PAIR]) as request:
            result = bot.get_pair(ADDRESS)
        self.assertIn("/token-pairs/v1/solana/", request.call_args.args[0])
        self.assertEqual(result, PAIR)
        output = bot.pair_summary(result)
        for value in ("SOL", "$150.25", "+1.50%", "$100,000", "$25,000", "$1,000,000", ADDRESS, PAIR["url"]):
            self.assertIn(value, output)

    def test_symbol_search_uses_solana_and_returns_best_exact_match(self):
        with patch.object(bot, "http_json", return_value={"pairs": [PAIR]}) as request:
            result = bot.get_pair("SOL")
        self.assertIn("/latest/dex/search?q=SOL", request.call_args.args[0])
        self.assertEqual(result["baseToken"]["symbol"], "SOL")

    def test_api_errors_are_dex_specific_and_token_redacted(self):
        with patch.object(bot.urllib.request, "urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(bot.DexScreenerError, "offline"):
                bot.http_json("https://api.dexscreener.com/token-pairs/v1/solana/" + ADDRESS)


class TelegramCommandTests(unittest.TestCase):
    def setUp(self):
        bot.LOG.setLevel(logging.CRITICAL)

    def receive(self, text, state, chat_id=1, user_id=7):
        bot.handle_update({"message": {"chat": {"id": chat_id}, "from": {"id": user_id}, "text": text}}, state, "unused", 7.5, set())

    def test_immediate_commands_auto_price_watch_scoped_list_and_remove(self):
        state = {"offset": 0, "watches": {}}
        sent = []
        with patch.object(bot, "get_pair", return_value=PAIR) as lookup:
            with patch.object(bot, "send_message", side_effect=lambda _token, _chat, text: sent.append(text)):
                self.receive("/start", state)
                self.receive("/help", state)
                self.receive(ADDRESS, state)
                self.receive("/preço " + ADDRESS, state)
                self.receive("/add " + ADDRESS, state)
                self.receive("/list", state, chat_id=1)
                self.receive("/list", state, chat_id=2)
                self.receive("/remove " + ADDRESS, state)
        self.assertIn("Bot de alertas Solana ativo", sent[0])
        self.assertIn("/price", sent[1])
        self.assertIn(PAIR["url"], sent[2])
        self.assertIn(PAIR["url"], sent[3])
        self.assertIn("Monitoramento ativado", sent[4])
        self.assertIn("SOL", sent[5])
        self.assertIn("7.5%", sent[5])
        self.assertEqual(sent[6], "Seus tokens monitorados:\nnenhum")
        self.assertEqual(sent[7], "Monitoramento removido.")
        self.assertEqual(lookup.call_count, 3)

    def test_dex_failure_gets_requested_friendly_reply_and_polling_error_is_not_swallowed(self):
        state = {"watches": {}}
        sent = []
        with patch.object(bot, "get_pair", side_effect=bot.DexScreenerError("HTTP 503")):
            with patch.object(bot, "send_message", side_effect=lambda _token, _chat, text: sent.append(text)):
                self.receive("/price " + ADDRESS, state)
        self.assertEqual(sent, ["DexScreener está temporariamente indisponível. Tente novamente em instantes."])

    def test_invalid_watch_address_never_calls_dex(self):
        with patch.object(bot, "get_pair") as lookup:
            with patch.object(bot, "send_message") as send:
                self.receive("/watch " + ADDRESS[:6] + "0" + ADDRESS[7:], {"watches": {}})
        lookup.assert_not_called()
        self.assertIn("endereço Solana válido", send.call_args.args[2])

    def test_list_is_chat_scoped_and_contains_cached_price(self):
        state = {"watches": {ADDRESS: {"address": ADDRESS, "symbol": "SOL", "last_price": 150, "subscribers": ["1"]}}}
        with patch.object(bot, "send_message") as send:
            self.receive("/lista", state, chat_id=1)
            self.receive("/list", state, chat_id=2)
        self.assertIn("SOL", send.call_args_list[0].args[2])
        self.assertIn("$150", send.call_args_list[0].args[2])
        self.assertEqual(send.call_args_list[1].args[2], "Seus tokens monitorados:\nnenhum")


class StateAndPollingTests(unittest.TestCase):
    def test_state_loader_repairs_bad_offset_and_discards_invalid_watch(self):
        path = Path(__file__).resolve().parent / ".state-test.json"
        try:
            path.write_text('{"offset":"garbage","watches":{"bad":{"address":"not-valid"}}}', encoding="utf-8")
            with patch.object(bot, "STATE_FILE", path):
                state = bot.load_state()
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(state, {"offset": 0, "watches": {}})

    def test_preflight_keeps_pending_updates_and_poll_starts_at_zero(self):
        state = {"offset": 987654321, "watches": {}}
        update = {"update_id": 34, "message": {"chat": {"id": 1}, "from": {"id": 2}, "text": "/help"}}
        calls = []

        def fake_telegram(token, method, payload=None):
            calls.append((method, payload))
            if method == "getUpdates":
                bot.STOP.set()
                return [update]
            return True

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}):
            with patch.object(bot, "validate_telegram"):
                with patch.object(bot, "telegram", side_effect=fake_telegram):
                    with patch.object(bot, "load_state", return_value=state):
                        with patch.object(bot, "save_state"):
                            with patch.object(bot, "send_message"):
                                with patch.object(bot.signal, "signal"):
                                    self.assertEqual(bot.main(), 0)
        poll = next(payload for method, payload in calls if method == "getUpdates")
        self.assertEqual(poll["offset"], 0)
        self.assertEqual(poll["timeout"], 1)
        self.assertEqual(state["offset"], 35)


if __name__ == "__main__":
    unittest.main()
