"""
models.py
---------
Data models for the Stock & Option Recommendation system.
Contains:
1. Pydantic schemas for runtime validation and JSON serialization.
2. SQLAlchemy ORM models for SQLite/PostgreSQL persistence.
"""

from enum import Enum
from datetime import datetime, timezone
from typing import List, Optional, Union
import json
import hashlib

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    Column, Integer, String, Float, Text, DateTime, Index
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CategoryEnum(str, Enum):
    OPTION = "OPTION"
    BTST = "BTST"
    INVESTMENT = "INVESTMENT"
    REPORT = "REPORT"
    IGNORE = "IGNORE"


class ActionEnum(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OptionTypeEnum(str, Enum):
    CE = "CE"
    PE = "PE"


# ---------------------------------------------------------------------------
# Pydantic Schemas (Runtime Validation)
# ---------------------------------------------------------------------------

class RecommendationSchema(BaseModel):
    symbol: str = Field(..., description="Stock or Index symbol, e.g. TATASTEEL, NIFTY, BANKNIFTY")
    category: CategoryEnum = Field(..., description="Classification category")
    action: ActionEnum = Field(default=ActionEnum.BUY, description="Order action: BUY or SELL")
    option_type: Optional[OptionTypeEnum] = Field(default=None, description="CE or PE for options")
    strike_price: Optional[float] = Field(default=None, description="Option strike price")
    expiry: Optional[str] = Field(default=None, description="Option/Contract expiry, e.g., 29AUG2024")
    entry_range: Optional[List[float]] = Field(default=None, description="Target buy/sell range [low, high]")
    targets: Optional[List[float]] = Field(default=None, description="List of target prices [T1, T2, T3]")
    stop_loss: Optional[float] = Field(default=None, description="Stop loss price")
    raw_text: str = Field(..., description="Raw text received from Telegram")
    source_channel: str = Field(..., description="Name or title of the Telegram channel/group")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 formatted timestamp string"
    )

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        clean = v.strip().upper().replace("$", "").replace("#", "")
        return clean or "UNKNOWN"

    @field_validator("entry_range", "targets", mode="before")
    @classmethod
    def ensure_float_list(cls, v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return [float(v)]
        if isinstance(v, list):
            res = []
            for elem in v:
                try:
                    res.append(float(elem))
                except (ValueError, TypeError):
                    continue
            return res if res else None
        return None

    def compute_hash(self) -> str:
        """Generate unique SHA256 hash for deduplication based on text and channel."""
        payload = f"{self.source_channel}:{self.raw_text.strip()}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# SQLAlchemy Database Model
# ---------------------------------------------------------------------------

class RecommendationModel(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_hash = Column(String(64), unique=True, index=True, nullable=False)
    telegram_message_id = Column(Integer, nullable=True)
    source_channel = Column(String(255), nullable=False, index=True)
    
    symbol = Column(String(50), nullable=False, index=True)
    category = Column(String(20), nullable=False, index=True)
    action = Column(String(10), nullable=False, default="BUY")
    option_type = Column(String(10), nullable=True)
    strike_price = Column(Float, nullable=True)
    expiry = Column(String(50), nullable=True)
    
    # Stored as JSON strings in DB for portability across SQLite and PostgreSQL
    entry_range_json = Column(Text, nullable=True)
    targets_json = Column(Text, nullable=True)
    
    stop_loss = Column(Float, nullable=True)
    raw_text = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_symbol_category", "symbol", "category"),
    )

    def to_dict(self) -> dict:
        """Convert SQLAlchemy record into standardized dict / JSON output."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "category": self.category,
            "action": self.action,
            "option_type": self.option_type,
            "strike_price": self.strike_price,
            "expiry": self.expiry,
            "entry_range": json.loads(self.entry_range_json) if self.entry_range_json else None,
            "targets": json.loads(self.targets_json) if self.targets_json else None,
            "stop_loss": self.stop_loss,
            "raw_text": self.raw_text,
            "source_channel": self.source_channel,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }
