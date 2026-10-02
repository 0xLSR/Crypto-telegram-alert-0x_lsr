import io
import logging
import os
import unittest
import urllib.parse
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import bot


class TelegramApiTests(unittest.TestCase):
    def test_http_error_logs_status_and_body_without_token(self):
        token = "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJK"
        encoded_token = urllib.parse.quote(token, safe="")
        body = f'{{"ok":false,"description":"bad request at bot{token}/getMe or {encoded_token}"}}'.encode()
        error = HTTPError(
            f"https://api.telegram.org/bot{token}/getMe",
            401,
            "Unauthorized",
            {},
            io.BytesIO(body),
        )
        with self.assertLogs(bot.LOG, level=logging.ERROR) as captured:
            with patch.object(bot.urllib.request, "urlopen", side_effect=error):
                with self.assertRaisesRegex(bot.BotError, "HTTP 401"):
                    bot.http_json(f"https://api.telegram.org/bot{token}/getMe")

        output = "\n".join(captured.output)
        self.assertIn("401", output)
        self.assertIn("bad request", output)
        self.assertNotIn(token, output)
        self.assertNotIn(encoded_token, output)
        self.assertIn("[REDACTED]", output)

    def test_network_error_is_specific_and_redacted(self):
        token = "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJK"
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        network_error = URLError(f"simulated DNS failure involving {token}")

        with self.assertLogs(bot.LOG, level=logging.ERROR) as captured:
            with patch.object(bot.urllib.request, "urlopen", side_effect=network_error):
                with self.assertRaises(bot.TelegramError) as raised:
                    bot.http_json(url)

        self.assertIn("simulated DNS failure", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(token, "\n".join(captured.output))

    def test_dexscreener_network_error_has_separate_type(self):
        with patch.object(
            bot.urllib.request,
            "urlopen",
            side_effect=URLError("simulated DexScreener network failure"),
        ):
            with self.assertRaises(bot.DexScreenerError) as raised:
                bot.http_json("https://api.dexscreener.com/latest/dex/search?q=SOL")
        self.assertIn("simulated DexScreener network failure", str(raised.exception))

    def test_telegram_error_includes_code_and_description_redacted(self):
        token = "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJK"
        response = {
            "ok": False,
            "error_code": 401,
            "description": f"Unauthorized {token}",
        }
        with patch.object(bot, "http_json", return_value=response):
            with self.assertRaises(bot.BotError) as raised:
                bot.telegram(token, "getMe")

        self.assertIn("401", str(raised.exception))
        self.assertIn("Unauthorized", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))

    def test_preflight_validates_token_then_deletes_webhook_preserving_updates(self):
        token = "test-token"
        with patch.object(
            bot,
            "telegram",
            side_effect=[{"id": 55, "username": "alert_bot"}, True],
        ) as telegram:
            bot.validate_telegram(token)

        self.assertEqual(
            telegram.call_args_list,
            [
                unittest.mock.call(token, "getMe"),
                unittest.mock.call(
                    token, "deleteWebhook", {"drop_pending_updates": False}
                ),
            ],
        )

    def test_help_keeps_requested_commands(self):
        help_output = bot.help_text()
        for command in ("/help", "/price", "/watch", "/unwatch", "/list"):
            self.assertIn(command, help_output)
        with patch.object(bot, "send_message") as send_message:
            bot.handle_update(
                {"message": {"chat": {"id": 1}, "from": {"id": 2}, "text": "/start"}},
                {"watches": {}},
                "test-token",
                10.0,
                set(),
            )
        self.assertIn("Bot de alertas Solana ativo", send_message.call_args.args[2])

    def test_price_watch_unwatch_and_list_respond_immediately(self):
        address = "So11111111111111111111111111111111111111112"
        pair = {
            "chainId": "solana",
            "baseToken": {"symbol": "SOL", "address": address},
            "priceUsd": "150",
            "priceChange": {"h24": 1.5},
            "liquidity": {"usd": 100000},
            "volume": {"h24": 25000},
            "fdv": 1000000,
            "url": "https://dexscreener.com/solana/example",
        }
        state = {"offset": 0, "watches": {}}
        sent = []

        def receive(update_text):
            bot.handle_update(
                {"message": {"chat": {"id": 1}, "from": {"id": 2}, "text": update_text}},
                state,
                "test-token",
                10.0,
                set(),
            )

        with patch.object(bot, "get_pair", return_value=pair) as get_pair:
            with patch.object(bot, "send_message", side_effect=lambda _token, _chat, text: sent.append(text)):
                receive("/price SOL")
                receive(f"/price {address}")
                receive(f"/watch {address}")
                receive("/list")
                receive(f"/unwatch {address}")

        self.assertEqual(get_pair.call_count, 3)
        self.assertIn("Preço: $150", sent[0])
        self.assertIn("Preço: $150", sent[1])
        self.assertIn("Monitoramento ativado", sent[2])
        self.assertIn(address, sent[3])
        self.assertEqual(sent[4], "Monitoramento removido.")
        self.assertEqual(state["watches"], {})

    def test_price_checks_skip_dexscreener_when_no_tokens_are_watched(self):
        with patch.object(bot, "get_pair") as get_pair:
            bot.check_prices({"watches": {}}, "test-token", 10.0, 1800.0)
        get_pair.assert_not_called()

    def test_dexscreener_monitoring_failure_does_not_escape_price_cycle(self):
        address = "So11111111111111111111111111111111111111112"
        state = {"watches": {address.lower(): {"address": address, "subscribers": []}}}
        with patch.object(bot, "http_json", side_effect=bot.DexScreenerError("temporary outage")):
            bot.check_prices(state, "test-token", 10.0, 1800.0)

    def test_continuous_poll_uses_telegram_long_poll_for_low_latency(self):
        self.assertEqual(bot.TELEGRAM_POLL_TIMEOUT_SECONDS, 25)

    def test_main_processes_pending_update_and_saves_offset(self):
        state = {"offset": 0, "watches": {}}
        saved_states = []
        update = {
            "update_id": 34,
            "message": {
                "chat": {"id": 1},
                "from": {"id": 2},
                "text": "/help",
            },
        }
        calls = []

        def fake_telegram(token, method, payload=None):
            calls.append((token, method, payload))
            if method == "getUpdates":
                bot.STOP.set()
                return [update]
            return None

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}):
            with patch.object(bot, "validate_telegram", side_effect=lambda _token: calls.append(("preflight",))):
                with patch.object(bot, "telegram", side_effect=fake_telegram):
                    with patch.object(bot, "load_state", return_value=state):
                        with patch.object(bot, "save_state", side_effect=lambda value: saved_states.append(dict(value))):
                            with patch.object(bot, "send_message") as send_message:
                                with patch.object(bot.signal, "signal"):
                                    self.assertEqual(bot.main(), 0)

        self.assertEqual(calls[0], ("preflight",))
        self.assertEqual(calls[1][1], "getUpdates")
        self.assertEqual(calls[1][2]["timeout"], 25)
        self.assertEqual(calls[1][2]["offset"], 0)
        self.assertTrue(send_message.called)
        self.assertEqual(state["offset"], 35)
        self.assertIn({"offset": 35, "watches": {}}, saved_states)


if __name__ == "__main__":
    unittest.main()
