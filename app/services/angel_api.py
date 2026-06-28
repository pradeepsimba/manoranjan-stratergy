from __future__ import annotations

"""
Angel One SmartAPI — order execution only.
Market data (historical + live) comes from the custom server at 35.234.219.141.
"""

import asyncio
from typing import Optional

import app.config as cfg


class AngelAPIService:
    def __init__(self) -> None:
        self._smart   = None
        self._ready   = False
        self._auth_token:  Optional[str] = None
        self._feed_token:  Optional[str] = None

    # ── Session ───────────────────────────────────────────────────────────────

    async def init_session(self) -> bool:
        """Authenticate with Angel One. Returns True on success."""
        if not cfg.ANGEL_API_KEY or not cfg.ANGEL_CLIENT_ID:
            print("Angel One credentials not set — order execution disabled")
            return False
        try:
            from SmartApi import SmartConnect
            import pyotp

            self._smart = SmartConnect(api_key=cfg.ANGEL_API_KEY)
            totp = pyotp.TOTP(cfg.ANGEL_TOTP_SECRET).now()
            data = await asyncio.to_thread(
                self._smart.generateSession,
                cfg.ANGEL_CLIENT_ID,
                cfg.ANGEL_PASSWORD,
                totp,
            )
            if data.get("status"):
                self._auth_token = data["data"]["jwtToken"]
                self._feed_token = data["data"]["feedToken"]
                self._ready = True
                print("Angel One session established")
                return True
            print(f"Angel One login failed: {data.get('message')}")
        except Exception as e:
            print(f"Angel One init error: {e}")
        return False

    async def close_session(self) -> None:
        if self._smart and self._ready:
            try:
                await asyncio.to_thread(self._smart.terminateSession, cfg.ANGEL_CLIENT_ID)
            except Exception:
                pass
        self._ready = False

    # ── Bracket Order (ROBO / BO) ─────────────────────────────────────────────

    async def place_bracket_order(
        self,
        symbol:        str,
        token:         str,
        quantity:      int,
        sl_offset:     float,
        target_offset: float,
    ) -> Optional[str]:
        """
        Fire a Market BUY Bracket Order.

        Angel One BO params:
          variety    = "ROBO"
          producttype = "BO"
          stoploss   = absolute offset below entry (points gap)
          squareoff  = absolute offset above entry (points gap)

        Returns order_id string on success, None on failure.
        """
        if not self._ready or not self._smart:
            print(f"Angel One not ready — cannot place order for {symbol}")
            return None

        order_params = {
            "variety":         "ROBO",
            "tradingsymbol":   f"{symbol}-EQ",
            "symboltoken":     token,
            "transactiontype": "BUY",
            "exchange":        cfg.NSE_EXCHANGE,
            "ordertype":       "MARKET",
            "producttype":     "BO",
            "duration":        "DAY",
            "price":           "0",
            "squareoff":       str(round(target_offset, 2)),
            "stoploss":        str(round(sl_offset, 2)),
            "quantity":        str(quantity),
        }

        try:
            resp = await asyncio.to_thread(self._smart.placeOrder, order_params)
            if resp.get("status"):
                order_id = resp["data"]["orderid"]
                print(
                    f"BO placed: {symbol} qty={quantity} "
                    f"SL={sl_offset} TGT={target_offset} → order_id={order_id}"
                )
                return order_id
            print(f"BO rejected for {symbol}: {resp.get('message')}")
        except Exception as e:
            print(f"place_bracket_order error ({symbol}): {e}")
        return None

    @property
    def is_ready(self) -> bool:
        return self._ready
