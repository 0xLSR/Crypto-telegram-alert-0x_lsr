import io
import logging
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import bot


class TelegramApiTests(unittest.TestCase):
    def test_http_error_logs_status_and_body_without_token(self):
        token = "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJK"
        body = f'{{"ok":false,"description":"bad request at bot{token}/getMe"}}'.encode()
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
        self.assertIn("[REDACTED]", output)

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


if __name__ == "__main__":
    unittest.main()
