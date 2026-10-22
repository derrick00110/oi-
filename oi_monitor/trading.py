"""Manual Binance USD-M orders. Broadcasts never call this."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

from oi_monitor.client import USER_AGENT, BinanceError

MAX_ORDER_ENV = "MAX_ORDER_USDT"
TRADING_ENV = "TRADING_ENABLED"


def max_order_usdt() -> float:
    raw = os.environ.get(MAX_ORDER_ENV, "1000").strip() or "1000"
    try:
        value = float(raw)
    except ValueError as exc:
        raise BinanceError("MAX_ORDER_USDT 不是数字") from exc
    if value <= 0:
        raise BinanceError("MAX_ORDER_USDT 必须大于 0")
    return value


def trading_enabled() -> bool:
    return os.environ.get(TRADING_ENV, "").strip() == "1"


def sign_query(secret: str, query: str) -> str:
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def decimal_text(raw, name: str) -> str:
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"{name} 不是数字")
    try:
        number = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} 不是数字") from exc
    if number <= 0:
        raise ValueError(f"{name} 必须大于 0")
    if number.as_tuple().exponent < -8:
        raise ValueError(f"{name} 最多 8 位小数")
    return format(number, "f")


def clean_order(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("下单内容必须是对象")
    symbol = str(raw.get("symbol") or "").upper().strip()
    if not symbol.isalnum() or not 5 <= len(symbol) <= 30:
        raise ValueError("合约符号无效")
    side = str(raw.get("side") or "").upper()
    order_type = str(raw.get("type") or "").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("方向只能是买入或卖出")
    if order_type not in {"MARKET", "LIMIT"}:
        raise ValueError("只支持市价和限价")
    quantity = decimal_text(raw.get("quantity"), "数量")
    price = None
    if order_type == "LIMIT":
        price = decimal_text(raw.get("price"), "限价")
    return {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
        "price": price,
        "reduceOnly": bool(raw.get("reduceOnly")),
        "confirm": raw.get("confirm"),
    }


def hint_for(key: str) -> str:
    if len(key) < 4:
        return ""
    return f"····{key[-4:]}"


class TradingDesk:
    def __init__(self, directory: Path, monitor):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.monitor = monitor
        self.credentials = self.directory / "binance.credentials"

    def status(self) -> dict:
        key, _secret, source = self._keys()
        return {
            "keysConfigured": bool(key),
            "keyHint": hint_for(key),
            "keySource": source,
            "tradingEnabled": trading_enabled(),
            "maxOrderUsdt": max_order_usdt(),
            "autoTrade": False,
        }

    def save_keys(self, api_key: str, api_secret: str) -> dict:
        env_key = os.environ.get("BINANCE_API_KEY", "").strip()
        env_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        if env_key and env_secret:
            raise BinanceError("密钥来自环境变量，页面不能覆盖。请改环境变量后重启。")
        api_key = (api_key or "").strip()
        api_secret = (api_secret or "").strip()
        if len(api_key) < 16 or len(api_secret) < 16:
            raise ValueError("API Key 或 Secret 太短")
        if "\n" in api_key or "\n" in api_secret:
            raise ValueError("密钥里不能换行")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.credentials.write_text(
            f"BINANCE_API_KEY={api_key}\nBINANCE_API_SECRET={api_secret}\n",
            encoding="utf-8",
        )
        os.chmod(self.credentials, 0o600)
        return self.status()

    def clear_keys(self) -> dict:
        if self.credentials.is_file():
            self.credentials.unlink()
        return self.status()

    def preview(self, raw: dict) -> dict:
        order = clean_order(raw)
        market_price = self._price(order["symbol"])
        used_price = Decimal(order["price"]) if order["price"] else Decimal(str(market_price))
        notional = Decimal(order["quantity"]) * used_price
        cap = Decimal(str(max_order_usdt()))
        ok = notional <= cap
        reason = "" if ok else f"名义价值 {notional:.2f} USDT 超过单笔上限 {cap:.2f} USDT"
        return {
            "ok": ok,
            "reason": reason,
            "symbol": order["symbol"],
            "side": order["side"],
            "type": order["type"],
            "quantity": order["quantity"],
            "price": format(used_price, "f"),
            "notional": float(notional),
            "maxOrderUsdt": float(cap),
            "tradingEnabled": trading_enabled(),
            "reduceOnly": order["reduceOnly"],
            "willSend": False,
        }

    def submit(self, raw: dict) -> dict:
        if not trading_enabled():
            raise BinanceError("真实下单没有打开。启动前设置 TRADING_ENABLED=1。播报不会自动下单。")
        if raw.get("confirm") != "下单":
            raise BinanceError("需要在请求里明确确认下单")
        preview = self.preview(raw)
        if not preview["ok"]:
            raise BinanceError(preview["reason"])
        order = clean_order(raw)
        params = [
            ("symbol", order["symbol"]),
            ("side", order["side"]),
            ("type", order["type"]),
            ("quantity", order["quantity"]),
        ]
        if order["type"] == "LIMIT":
            params.extend((("price", order["price"]), ("timeInForce", "GTC")))
        if order["reduceOnly"]:
            params.append(("reduceOnly", "true"))
        payload = self._signed("POST", "/fapi/v1/order", params)
        return {
            "ok": True,
            "orderId": payload.get("orderId"),
            "status": payload.get("status"),
            "symbol": payload.get("symbol") or order["symbol"],
            "side": payload.get("side") or order["side"],
            "type": payload.get("type") or order["type"],
            "executedQty": payload.get("executedQty"),
            "willSend": True,
        }

    def test_connection(self) -> dict:
        key, _secret, _source = self._keys()
        if not key:
            raise BinanceError("还没有配置 API Key")
        balances = self._signed("GET", "/fapi/v2/balance", [])
        usdt = 0.0
        if isinstance(balances, list):
            for item in balances:
                if item.get("asset") == "USDT":
                    usdt = float(item.get("availableBalance") or 0)
                    break
        status = self.status()
        status["usdtAvailable"] = usdt
        status["connected"] = True
        return status

    def _price(self, symbol: str) -> float:
        row = self.monitor.find_row(symbol) if self.monitor is not None else None
        if row and row.get("price"):
            return float(row["price"])
        ticker = self.monitor.client.get_json("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(ticker["price"])

    def _keys(self) -> tuple[str, str, str]:
        env_key = os.environ.get("BINANCE_API_KEY", "").strip()
        env_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        if env_key and env_secret:
            return env_key, env_secret, "env"
        if not self.credentials.is_file():
            return "", "", "none"
        found = {}
        for line in self.credentials.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            name, value = line.split("=", 1)
            found[name.strip()] = value.strip()
        key = found.get("BINANCE_API_KEY", "")
        secret = found.get("BINANCE_API_SECRET", "")
        if key and secret:
            return key, secret, "file"
        return "", "", "none"

    def _signed(self, method: str, path: str, params: list[tuple[str, str]]):
        key, secret, _source = self._keys()
        if not key or not secret:
            raise BinanceError("还没有配置 API Key")
        base = self.monitor.client.ensure()
        signed = [
            *params,
            ("recvWindow", "5000"),
            ("timestamp", str(int(time.time() * 1000))),
        ]
        query = urllib.parse.urlencode(signed)
        signature = sign_query(secret, query)
        url = f"{base}{path}?{query}&signature={signature}"
        request = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", "X-MBX-APIKEY": key},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            if exc.code == 451 or "restricted location" in body:
                raise BinanceError("这台机器访问不了币安合约交易接口。公开行情仍可播报，下单需要能连上 fapi.binance.com 的网络。") from exc
            message = body[:180]
            try:
                parsed = json.loads(body)
                message = str(parsed.get("msg") or message)
            except json.JSONDecodeError:
                pass
            raise BinanceError(f"币安拒绝了请求：{message}") from exc
        except urllib.error.URLError as exc:
            raise BinanceError(f"连接币安失败：{exc.reason}") from exc
        if isinstance(payload, dict) and payload.get("code"):
            raise BinanceError(str(payload.get("msg") or payload.get("code")))
        return payload
