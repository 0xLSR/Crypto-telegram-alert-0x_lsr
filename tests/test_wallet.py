import json
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import wallet

ADDRESS = "So11111111111111111111111111111111111111112"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLM"


class WalletRpcTests(unittest.TestCase):
    def test_wallet_info_reads_sol_and_spl_balances_and_only_values_quoted_assets(self):
        token_account = {"account": {"data": {"parsed": {"info": {
            "mint": MINT,
            "tokenAmount": {"amount": "1000000", "decimals": 4, "uiAmountString": "100"},
        }}}}}

        def rpc(method, params):
            if method == "getBalance":
                return {"value": 1_284_500_000}
            if method == "getTokenAccountsByOwner":
                return {"value": [token_account] if params[1]["programId"] == wallet.TOKEN_PROGRAMS[0] else []}
            self.fail(f"Unexpected RPC method: {method}")

        def market(query):
            if query == ADDRESS:
                return {"priceUsd": "150", "baseToken": {"name": "Wrapped SOL", "symbol": "SOL"}}
            return {"priceUsd": "0.02", "baseToken": {"name": "USD Coin", "symbol": "USDC"}}

        with patch.object(wallet, "solana_rpc", side_effect=rpc):
            info = wallet.get_wallet_info(ADDRESS, market)
        self.assertEqual(info["sol"], "1.2845")
        self.assertEqual(info["token_count"], 1)
        self.assertEqual(info["tokens"][0]["symbol"], "USDC")
        self.assertEqual(info["tokens"][0]["amount"], "100")
        self.assertEqual(info["estimated_usd"], "194.6750")

    def test_rpc_failure_is_typed_and_does_not_leak_rpc_url(self):
        with patch.object(wallet.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertLogs(wallet.LOG, level=logging.WARNING) as logs:
                with self.assertRaises(wallet.WalletRpcError):
                    wallet.solana_rpc("getBalance", [ADDRESS])
        self.assertIn("[WALLET] RPC error", "\n".join(logs.output))
        self.assertNotIn(wallet.SOLANA_RPC_URL, "\n".join(logs.output))

    def test_transaction_type_remains_generic_when_rpc_cannot_prove_buy_or_sell(self):
        self.assertEqual(wallet.classify_transaction({"meta": {}}), "🔄 Movimentação detectada")
        tx = {"meta": {
            "preTokenBalances": [{"owner": ADDRESS, "mint": MINT, "uiTokenAmount": {"amount": "100", "decimals": 2, "uiAmountString": "1"}}],
            "postTokenBalances": [{"owner": ADDRESS, "mint": MINT, "uiTokenAmount": {"amount": "350", "decimals": 2, "uiAmountString": "3.5"}}],
        }}
        self.assertEqual(wallet.extract_token_changes(tx, ADDRESS), [{"mint": MINT, "amount": "2.5", "decimals": "2"}])


class WalletBotTests(unittest.TestCase):
    def update(self, text, chat=11, user=22):
        return {"message": {"chat": {"id": chat}, "from": {"id": user}, "text": text}}

    def callback(self, data, user=22, chat=11, callback_id="wallet-cb"):
        return {"callback_query": {"id": callback_id, "from": {"id": user}, "data": data,
                                   "message": {"message_id": 99, "chat": {"id": chat}}}}

    def test_start_includes_wallet_section(self):
        with patch.object(bot, "send_message") as send:
            bot.handle_update(self.update("/start"), {"watches": {}, "wallets": {}}, TOKEN, 10, set())
        self.assertIn("💼 Minha carteira", str(send.call_args.args[3]))

    def test_wallet_add_button_opens_public_address_prompt_and_saves_pending_user(self):
        state = {"watches": {}, "wallets": {}, "awaiting_add": {}, "pending_add": {}}
        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message") as edit, patch.object(bot, "save_state"):
            bot.handle_callback_update(self.callback("wallet_add"), state, TOKEN, 10, set())
        self.assertEqual(state["awaiting_wallet"]["22"], "11")
        self.assertIn("endereço público da sua carteira Solana", edit.call_args.args[3])
        self.assertIn("Nunca envie seed phrase ou chave privada", edit.call_args.args[3])

    def test_wallet_registration_validates_public_address_and_stores_no_credentials(self):
        state = {"watches": {}, "wallets": {}, "awaiting_wallet": {"22": "11"}}
        with patch.object(bot.solana_wallet, "get_wallet_signatures") as signatures:
            with patch.object(bot, "save_state"):
                with patch.object(bot, "send_message") as send:
                    bot.handle_update(self.update(ADDRESS), state, TOKEN, 10, set())
        signatures.assert_not_called()
        entry = state["wallets"]["22"]
        self.assertEqual(entry["address"], ADDRESS)
        self.assertEqual(entry["chat_id"], "11")
        self.assertIsNone(entry["last_signature"])
        self.assertIsInstance(entry["registered_at"], float)
        self.assertTrue(entry["monitoring_enabled"])
        self.assertTrue(entry["alerts_enabled"])
        self.assertFalse({"private_key", "secret_key", "seed", "seed_phrase"}.intersection(entry))
        self.assertIn("somente leitura", send.call_args.args[2])

    def test_wallet_registration_rejects_mnemonic_without_logging_or_storing_input(self):
        state = {"watches": {}, "wallets": {}, "awaiting_wallet": {"22": "11"}}
        secret_like = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
        with patch.object(bot.solana_wallet, "get_wallet_signatures") as signatures:
            with patch.object(bot, "send_message") as send:
                bot.handle_update(self.update(secret_like), state, TOKEN, 10, set())
        signatures.assert_not_called()
        self.assertEqual(state["wallets"], {})
        self.assertNotIn(secret_like, send.call_args.args[2])
        self.assertIn("Endereço Solana inválido", send.call_args.args[2])

    def test_wallet_registration_persists_and_survives_state_reload(self):
        path = Path(__file__).resolve().parent / ".wallet-roundtrip-test.json"
        state = {"offset": 5, "watches": {}, "awaiting_wallet": {"22": "11"}, "wallets": {}}
        try:
            with patch.object(bot, "STATE_FILE", path):
                bot.register_wallet(state, "22", 11, ADDRESS)
                reloaded = bot.load_state()
            self.assertEqual(reloaded["offset"], 5)
            self.assertEqual(reloaded["wallets"]["22"]["address"], ADDRESS)
            self.assertTrue(reloaded["wallets"]["22"]["monitoring_enabled"])
            self.assertFalse(reloaded["awaiting_wallet"])
        finally:
            path.unlink(missing_ok=True)

    def test_wallet_screen_renders_balances_and_rpc_failure_is_friendly(self):
        entry = {"address": ADDRESS, "chat_id": "11", "alerts_enabled": True, "history": []}
        state = {"watches": {}, "wallets": {"22": entry}}
        info = {"sol": "1.2845", "sol_price_usd": "150", "estimated_usd": "192.675", "token_count": 0, "tokens": []}
        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message") as edit:
            with patch.object(bot.solana_wallet, "get_wallet_info", return_value=info):
                bot.handle_callback_update(self.callback("wallet_view"), state, TOKEN, 10, set())
        self.assertIn("1.2845 SOL", edit.call_args.args[3])
        self.assertIn("$192.68", edit.call_args.args[3])
        self.assertIn("PnL", edit.call_args.args[3])

        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message") as edit:
            with patch.object(bot.solana_wallet, "get_wallet_info", side_effect=wallet.WalletRpcError("offline")):
                bot.handle_callback_update(self.callback("wallet_refresh"), state, TOKEN, 10, set())
        self.assertIn("Não consegui consultar a blockchain", edit.call_args.args[3])

    def test_wallet_history_alert_toggle_and_remove_are_per_user(self):
        entry = {"address": ADDRESS, "chat_id": "11", "alerts_enabled": True, "history": [{
            "signature": "sig-123", "classification": "🔄 Movimentação detectada", "timestamp": 1, "assets": [],
        }]}
        other = {"address": "CwSNUU54NLJt4qbGg5S8TUzJYxjYAfL4VrPr7zvJpump", "chat_id": "99", "alerts_enabled": True, "history": []}
        state = {"watches": {}, "wallets": {"22": entry, "33": other}}
        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message") as edit:
            bot.handle_callback_update(self.callback("wallet_history"), state, TOKEN, 10, set())
        self.assertIn("HISTÓRICO DA CARTEIRA", edit.call_args.args[3])
        self.assertIn("https://solscan.io/tx/sig-123", str(edit.call_args.args[4]))

        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message") as edit, patch.object(bot, "save_state"):
            bot.handle_callback_update(self.callback("wallet_alert_toggle"), state, TOKEN, 10, set())
        self.assertFalse(state["wallets"]["22"]["alerts_enabled"])
        self.assertTrue(state["wallets"]["33"]["alerts_enabled"])
        self.assertIn("Alertas desativados", edit.call_args.args[3])

        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message"), patch.object(bot, "save_state"):
            bot.handle_callback_update(self.callback("wallet_remove_yes"), state, TOKEN, 10, set())
        self.assertNotIn("22", state["wallets"])
        self.assertIn("33", state["wallets"])

    def test_new_wallet_transaction_is_alerted_once_and_persisted(self):
        wallet_info = {"address": ADDRESS, "chat_id": "11", "last_signature": "old-signature",
                       "monitoring_enabled": True, "alerts_enabled": True, "history": []}
        state = {"watches": {}, "wallets": {"22": wallet_info}}
        signatures = [{"signature": "new-signature", "blockTime": 1_700_000_000}, {"signature": "old-signature", "blockTime": 1_699_999_000}]
        transaction = {"blockTime": 1_700_000_000, "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": []}}
        with patch.object(bot.solana_wallet, "get_wallet_signatures", return_value=signatures):
            with patch.object(bot.solana_wallet, "get_wallet_transaction", return_value=transaction):
                with patch.object(bot, "send_message") as send, patch.object(bot, "save_state"):
                    bot.check_wallets(state, TOKEN)
                    bot.check_wallets(state, TOKEN)
        send.assert_called_once()
        alert = send.call_args.args[2]
        self.assertIn("Movimentação detectada", alert)
        self.assertIn("Valor: indisponível", alert)
        self.assertIn("https://solscan.io/tx/new-signature", str(send.call_args.args[3]))
        self.assertEqual(state["wallets"]["22"]["last_signature"], "new-signature")
        self.assertEqual(len(state["wallets"]["22"]["history"]), 1)

    def test_first_poll_ignores_pre_registration_history_but_alerts_new_signature(self):
        state = {"watches": {}, "wallets": {"22": {
            "address": ADDRESS, "chat_id": "11", "last_signature": None, "registered_at": 1_700_000_000,
            "monitoring_enabled": True, "alerts_enabled": True, "history": [],
        }}}
        signatures = [{"signature": "new", "blockTime": 1_700_000_001}, {"signature": "old", "blockTime": 1_699_999_999}]
        tx = {"blockTime": 1_700_000_001, "meta": {"err": None}}
        with patch.object(bot.solana_wallet, "get_wallet_signatures", return_value=signatures):
            with patch.object(bot.solana_wallet, "get_wallet_transaction", return_value=tx):
                with patch.object(bot, "send_message") as send, patch.object(bot, "save_state"):
                    bot.check_wallets(state, TOKEN)
        send.assert_called_once()
        self.assertEqual(state["wallets"]["22"]["last_signature"], "new")

    def test_wallet_rpc_failure_does_not_escape_wallet_monitor(self):
        state = {"watches": {}, "wallets": {"22": {"address": ADDRESS, "chat_id": "11", "last_signature": "sig"}}}
        with patch.object(bot.solana_wallet, "get_wallet_signatures", side_effect=wallet.WalletRpcError("offline")):
            bot.check_wallets(state, TOKEN)
        self.assertEqual(state["wallets"]["22"]["last_signature"], "sig")

    def test_existing_monitor_loop_checks_wallets_without_running_price_monitor_for_empty_watch_list(self):
        bot.STOP.clear()
        state = {"watches": {}, "wallets": {"22": {"address": ADDRESS, "monitoring_enabled": True}}}
        def stop_after_wallet_check(*_args):
            bot.STOP.set()
        with patch.object(bot, "check_wallets", side_effect=stop_after_wallet_check) as check:
            with patch.object(bot, "check_prices") as prices:
                with patch.object(bot, "save_state"):
                    bot.market_monitor(state, TOKEN, 10, 1800, 60)
        check.assert_called_once_with(state, TOKEN)
        prices.assert_not_called()
        bot.STOP.clear()

    def test_existing_state_migrates_and_preserves_wallet_without_unknown_credentials(self):
        old_state = {"offset": 9, "watches": {}, "wallets": {"22": {
            "address": ADDRESS, "chat_id": "11", "last_signature": "sig", "private_key": "never keep",
            "history": [{"signature": "sig", "assets": [{"mint": MINT, "amount": "1", "secret": "discard"}]}],
        }}}
        path = Path(__file__).resolve().parent / ".wallet-state-test.json"
        try:
            path.write_text(json.dumps(old_state), encoding="utf-8")
            with patch.object(bot, "STATE_FILE", path):
                migrated = bot.load_state()
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(migrated["offset"], 9)
        self.assertEqual(migrated["wallets"]["22"]["last_signature"], "sig")
        self.assertNotIn("private_key", migrated["wallets"]["22"])
        self.assertNotIn("secret", migrated["wallets"]["22"]["history"][0]["assets"][0])


if __name__ == "__main__":
    unittest.main()
