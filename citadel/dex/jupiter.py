# -*- coding: utf-8 -*-
"""
Исполнение свопов на Solana через Jupiter + минимальный JSON-RPC клиент.

Почему Jupiter, а не Axiom: у Axiom нет публичного API для сторонних ботов —
это веб-терминал, который сам маршрутизирует свопы через агрегаторы. Jupiter —
документированный бесплатный агрегатор, через который ходит почти вся Solana,
включая такие терминалы. Токены те же, маршрут тот же, только без посредника.

Подпись транзакции требует пакета `solders` (pip install solders). Без него
доступен только бумажный режим — и это нормальный сценарий работы.
"""
from __future__ import annotations

import base64
import logging
import time

from .http import ApiError, HttpClient

log = logging.getLogger("citadel.dex.jupiter")

LAMPORTS = 1_000_000_000


class SolanaRpc:
    def __init__(self, url: str, client: HttpClient | None = None):
        self.url = url
        self.http = client or HttpClient(min_interval=0.15, retries=4)
        self._id = 0

    def call(self, method: str, params: list) -> dict:
        self._id += 1
        resp = self.http.post_json(self.url, {"jsonrpc": "2.0", "id": self._id,
                                              "method": method, "params": params})
        if "error" in resp:
            raise ApiError(f"RPC {method}: {resp['error']}")
        return resp.get("result", {})

    def decimals(self, mint: str) -> int:
        res = self.call("getTokenSupply", [mint])
        return int((res.get("value") or {}).get("decimals", 0))

    def sol_balance(self, owner: str) -> float:
        res = self.call("getBalance", [owner])
        return float((res.get("value") or 0)) / LAMPORTS

    def token_balance(self, owner: str, mint: str) -> float:
        """Фактический баланс токена на кошельке — источник истины после свопа."""
        res = self.call("getTokenAccountsByOwner",
                        [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0.0
        for acc in res.get("value") or []:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amount = (info.get("tokenAmount") or {}).get("uiAmountString")
            try:
                total += float(amount)
            except (TypeError, ValueError):
                continue
        return total

    def send_raw(self, signed_b64: str) -> str:
        return self.call("sendTransaction",
                         [signed_b64, {"encoding": "base64", "skipPreflight": False,
                                       "maxRetries": 3}])

    def confirm(self, signature: str, timeout: float = 90.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            status = (res.get("value") or [None])[0]
            if status:
                if status.get("err"):
                    raise ApiError(f"транзакция {signature} отклонена сетью: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            time.sleep(2)
        raise ApiError(f"транзакция {signature} не подтвердилась за {timeout:.0f}с")


class Jupiter:
    def __init__(self, base_url: str, client: HttpClient | None = None):
        self.base = base_url.rstrip("/")
        self.http = client or HttpClient(min_interval=0.2)

    def quote(self, input_mint: str, output_mint: str, amount_atomic: int,
              slippage_bps: int) -> dict:
        q = self.http.get_json(f"{self.base}/quote", {
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": int(amount_atomic), "slippageBps": int(slippage_bps),
            "restrictIntermediateTokens": "true",
        })
        if not q.get("outAmount"):
            raise ApiError(f"Jupiter не построил маршрут {input_mint} → {output_mint}")
        return q

    def swap_transaction(self, quote: dict, user_pubkey: str,
                         priority_lamports: int = 100_000) -> str:
        resp = self.http.post_json(f"{self.base}/swap", {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": int(priority_lamports),
        })
        tx = resp.get("swapTransaction")
        if not tx:
            raise ApiError(f"Jupiter не вернул транзакцию: {str(resp)[:200]}")
        return tx


class Wallet:
    """Кошелёк на solders. Импорт ленивый: без реальной торговли пакет не нужен."""

    def __init__(self, private_key_base58: str):
        try:
            from solders.keypair import Keypair            # noqa: PLC0415
        except ImportError as e:
            raise SystemExit("для реальных свопов нужен solders:  pip install solders") from e
        if not private_key_base58:
            raise SystemExit("нет SOLANA_PRIVATE_KEY в .env — реальная торговля невозможна")
        self.keypair = Keypair.from_base58_string(private_key_base58.strip())
        self.pubkey = str(self.keypair.pubkey())

    def sign(self, swap_tx_b64: str) -> str:
        from solders.transaction import VersionedTransaction   # noqa: PLC0415

        raw = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [self.keypair])
        return base64.b64encode(bytes(signed)).decode()
