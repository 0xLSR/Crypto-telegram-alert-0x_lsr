import json
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import wallet
import nansen_client as nansen

ADDRESS = "So11111111111111111111111111111111111111112"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLM"


class WalletRpcTests(unittest.TestCase):
    def setUp(self):
        wallet._PRICE_CACHE.clear()

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

    def make_wallet_tokens(self, count=20):
        accounts = []
        mints = []
        for index in range(count):
            mint = ("123456789ABCDEFGHJKLMNPQRSTUVWXYZ"[index] * 32)
            mints.append(mint)
            accounts.append({"account": {"data": {"parsed": {"info": {
                "mint": mint,
                "tokenAmount": {"amount": "1000000", "decimals": 6, "uiAmountString": "1"},
            }}}}})
        return accounts, mints

    def get_wallet_with_tokens(self, accounts, market, batch=None, sol_price="100"):
        def rpc(method, params):
            if method == "getBalance":
                return {"value": 1_000_000_000}
            if method == "getTokenAccountsByOwner":
                return {"value": accounts if params[1]["programId"] == wallet.TOKEN_PROGRAMS[0] else []}
            self.fail(f"Unexpected RPC method: {method}")

        with patch.object(wallet, "solana_rpc", side_effect=rpc):
            return wallet.get_wallet_info(ADDRESS, market, batch)

    def test_wallet_total_is_partial_when_two_of_twenty_tokens_have_no_quote(self):
        accounts, mints = self.make_wallet_tokens()
        quoted = {mint: {"priceUsd": "2", "baseToken": {"symbol": f"T{i}"}}
                  for i, mint in enumerate(mints[:18])}

        def market(mint):
            if mint == ADDRESS:
                return {"priceUsd": "100"}
            return None

        info = self.get_wallet_with_tokens(accounts, market, lambda batch: {m: quoted[m] for m in batch if m in quoted})
        self.assertEqual(info["token_count"], 20)
        self.assertEqual(info["unpriced_tokens"], 2)
        self.assertEqual(info["estimated_usd"], "136")
        self.assertTrue(info["is_partial"])
        self.assertEqual(len(info["tokens"]), 20)

    def test_twenty_tokens_are_all_valued_without_quantity_cutoff(self):
        accounts, mints = self.make_wallet_tokens()
        quotes = {mint: {"priceUsd": "1"} for mint in mints}
        info = self.get_wallet_with_tokens(accounts,
                                          lambda mint: {"priceUsd": "1"} if mint == ADDRESS else None,
                                          lambda batch: {mint: quotes[mint] for mint in batch})
        self.assertEqual(info["estimated_usd"], "21")
        self.assertEqual(info["unpriced_tokens"], 0)
        self.assertEqual(len(info["tokens"]), 20)

    def test_all_tokens_unpriced_still_shows_quoted_sol_value(self):
        accounts, _ = self.make_wallet_tokens()
        info = self.get_wallet_with_tokens(accounts, lambda mint: {"priceUsd": "100"} if mint == ADDRESS else None)
        self.assertEqual(info["estimated_usd"], "100")
        self.assertEqual(info["unpriced_tokens"], 20)
        self.assertTrue(info["is_partial"])

    def test_valuation_uses_usd_value_not_absolute_token_quantity(self):
        large, small = "2" * 32, "3" * 32
        accounts = [
            {"account": {"data": {"parsed": {"info": {"mint": large, "tokenAmount": {"amount": "1000000000000", "decimals": 0, "uiAmountString": "1000000000000"}}}}}},
            {"account": {"data": {"parsed": {"info": {"mint": small, "tokenAmount": {"amount": "1", "decimals": 0, "uiAmountString": "1"}}}}}},
        ]
        quotes = {large: {"priceUsd": "0.000001"}, small: {"priceUsd": "100"}}
        info = self.get_wallet_with_tokens(accounts, lambda _: None, lambda batch: {m: quotes[m] for m in batch})
        self.assertEqual(info["estimated_usd"], "1000100.000000")
        self.assertEqual(len(info["tokens"]), 2)

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

    def test_nansen_wallet_screen_shows_exact_ten_holdings_top_and_allocation(self):
        holdings = [{"mint": f"mint-{i}", "symbol": "SIGF" if i == 0 else f"T{i}",
                     "name": f"Token {i}", "amount": str(i + 1), "price_usd": "1",
                     "value_usd": "397.48" if i == 0 else "4.79"} for i in range(10)]
        info = nansen.normalize_balances(ADDRESS, [{
            "token_address": item["mint"], "token_symbol": item["symbol"], "token_name": item["name"],
            "token_amount": item["amount"], "price_usd": item["price_usd"], "value_usd": item["value_usd"],
        } for item in holdings])
        info["defi"] = None
        entry = {"address": ADDRESS, "configured_default": True, "monitoring_enabled": False}
        text = bot.wallet_info_text(ADDRESS, info, entry, details=True)
        self.assertIn("Top token: SIGF", text)
        self.assertIn("1. SIGF", text)
        self.assertIn("10. T9", text)
        self.assertIn("10 ativos encontrados", text)
        self.assertNotIn("rpc-", text)

    def test_wallet_screen_shows_partial_estimate_and_paginated_asset_details(self):
        tokens = [{"mint": f"mint-{i}", "amount": "100", "symbol": f"T{i}",
                   "price_usd": "1" if i < 18 else None, "value_usd": "100" if i < 18 else None}
                  for i in range(20)]
        info = {"sol": "0.002860606", "sol_price_usd": "118.62997", "sol_value_usd": "0.3395",
                "estimated_usd": "1800.3395", "token_count": 20, "unpriced_tokens": 2,
                "is_partial": True, "tokens": tokens}
        entry = {"address": ADDRESS, "chat_id": "11", "alerts_enabled": True, "history": []}
        text = bot.wallet_info_text(ADDRESS, info, entry)
        self.assertIn("$1,800.34", text)
        self.assertIn("2 token(s) sem cotação", text)
        first_page = bot.wallet_info_text(ADDRESS, info, entry, details=True)
        second_page = bot.wallet_info_text(ADDRESS, info, entry, details=True, detail_page=1)
        last_page = bot.wallet_info_text(ADDRESS, info, entry, details=True, detail_page=3)
        self.assertIn("1. T0", first_page)
        self.assertIn("6. T5", first_page)
        self.assertIn("10. T9", first_page)
        self.assertNotIn("11. T10", first_page)
        self.assertIn("11. T10", second_page)
        self.assertIn("20. T19", last_page)
        self.assertIn("Valor: indisponível", last_page)
        self.assertTrue(any("wallet_tokens:1" == b.get("callback_data") for row in bot.wallet_markup(entry, detail_page=0, page_count=2)["inline_keyboard"] for b in row))
        state = {"watches": {}, "wallets": {"22": entry}}
        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message") as edit, \
             patch.object(bot.solana_wallet, "get_wallet_info", return_value=info), patch.object(bot, "save_state"):
            bot.handle_callback_update(self.callback("wallet_tokens:1"), state, TOKEN, 10, set())
        self.assertIn("11. T10", edit.call_args.args[3])
        self.assertIn("2/2", str(edit.call_args.args[4]))

        with patch.object(bot, "answer_callback"), patch.object(bot, "edit_message") as edit:
            with patch.object(bot.solana_wallet, "get_wallet_info", side_effect=wallet.WalletRpcError("offline")) as lookup:
                bot.handle_callback_update(self.callback("wallet_refresh"), state, TOKEN, 10, set())
        self.assertIs(lookup.call_args.kwargs["force_refresh"], True)
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
