"""Paper order model. These objects never leave this process: there is no code path that sends them anywhere."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from config import EXECUTION_MODE


class DuplicatePaperOrder(ValueError):
    pass


def idea_key(symbol: str, side: str, setup: str, signal_date: str) -> str:
    return f"{symbol}|{side}|{setup}|{signal_date}"


class PaperOrder(BaseModel):
    order_id: str = ""
    idea_key: str
    symbol: str
    side: Literal["LONG", "SHORT"]
    instrument: Literal["EQ_DELIVERY", "STOCK_FUTURES"]
    order_type: Literal["STOP_ENTRY"] = "STOP_ENTRY"
    qty: int = Field(gt=0)
    lot: int = Field(default=1, gt=0)
    trigger_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    target1: float = Field(gt=0)
    target2: float = Field(gt=0)
    created_on: date  # signal session; the order can first fill on the NEXT session
    valid_sessions: int = 3
    max_hold: int = 15
    mode: Literal["PAPER"] = "PAPER"
    notes: str = ""

    @field_validator("mode")
    @classmethod
    def _paper_only(cls, v: str) -> str:
        if v != "PAPER" or EXECUTION_MODE != "PAPER":
            raise ValueError("paper orders only")
        return v

    def model_post_init(self, _ctx) -> None:
        if not self.order_id:
            self.order_id = "P-" + hashlib.sha1(self.idea_key.encode()).hexdigest()[:10].upper()
        long = self.side == "LONG"
        if long and not (self.stop_loss < self.trigger_price < self.target1 <= self.target2):
            raise ValueError("long order needs stop < trigger < T1 <= T2")
        if not long and not (self.stop_loss > self.trigger_price > self.target1 >= self.target2):
            raise ValueError("short order needs stop > trigger > T1 >= T2")
        if self.qty % self.lot:
            raise ValueError("quantity must be a whole number of lots")
