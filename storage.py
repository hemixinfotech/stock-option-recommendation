"""
storage.py
----------
Database persistence layer using SQLAlchemy for the Stock Recommendation Platform.
Handles DB initialization, deduplication, saving, and querying.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError

from config import DATABASE_URL
from models import Base, RecommendationModel, RecommendationSchema

logger = logging.getLogger("stock_recommendation.storage")

# Create engine & session factory
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables initialized successfully on %s", DATABASE_URL)


def get_db() -> Session:
    """Dependency helper for database session."""
    db = SessionLocal()
    try:
        return db
    finally:
        db.close()


def save_recommendation(
    schema: RecommendationSchema,
    telegram_message_id: Optional[int] = None
) -> Optional[RecommendationModel]:
    """
    Save recommendation to database with automatic hash deduplication.
    Returns the created record or None if duplicate.
    """
    msg_hash = schema.compute_hash()
    db = SessionLocal()
    try:
        # Check if hash already exists
        existing = db.query(RecommendationModel).filter_by(message_hash=msg_hash).first()
        if existing:
            logger.debug("Duplicate message hash %s skipped.", msg_hash)
            return None

        # Parse timestamp string to datetime object
        try:
            ts_dt = datetime.fromisoformat(schema.timestamp)
        except Exception:
            ts_dt = datetime.now(timezone.utc)

        record = RecommendationModel(
            message_hash=msg_hash,
            telegram_message_id=telegram_message_id,
            source_channel=schema.source_channel,
            symbol=schema.symbol,
            category=schema.category.value,
            action=schema.action.value,
            option_type=schema.option_type.value if schema.option_type else None,
            strike_price=schema.strike_price,
            expiry=schema.expiry,
            entry_range_json=json.dumps(schema.entry_range) if schema.entry_range else None,
            targets_json=json.dumps(schema.targets) if schema.targets else None,
            stop_loss=schema.stop_loss,
            raw_text=schema.raw_text,
            timestamp=ts_dt,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        logger.info("Saved recommendation ID %d [%s - %s]", record.id, record.symbol, record.category)
        return record
    except IntegrityError:
        db.rollback()
        logger.warning("Integrity error on hash %s, skipping.", msg_hash)
        return None
    except Exception as exc:
        db.rollback()
        logger.error("Failed to save recommendation: %s", exc, exc_info=True)
        return None
    finally:
        db.close()


def get_recommendations(
    category: Optional[str] = None,
    symbol: Optional[str] = None,
    source_channel: Optional[str] = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """Retrieve saved recommendations with optional filters."""
    db = SessionLocal()
    try:
        query = db.query(RecommendationModel)
        if category:
            query = query.filter(RecommendationModel.category == category.upper())
        if symbol:
            query = query.filter(RecommendationModel.symbol.like(f"%{symbol.upper()}%"))
        if source_channel:
            query = query.filter(RecommendationModel.source_channel == source_channel)
        
        records = query.order_by(RecommendationModel.timestamp.desc()).limit(limit).all()
        return [r.to_dict() for r in records]
    finally:
        db.close()


def get_recommendation_stats() -> Dict[str, Any]:
    """Calculate aggregated stats across saved recommendations."""
    db = SessionLocal()
    try:
        records = db.query(RecommendationModel).all()
        total = len(records)
        categories = {"OPTION": 0, "BTST": 0, "INVESTMENT": 0, "REPORT": 0}
        symbols = {}
        channels = {}

        for r in records:
            if r.category in categories:
                categories[r.category] += 1
            symbols[r.symbol] = symbols.get(r.symbol, 0) + 1
            channels[r.source_channel] = channels.get(r.source_channel, 0) + 1

        top_symbols = sorted(symbols.items(), key=lambda x: x[1], reverse=True)[:5]
        top_channels = sorted(channels.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "total_count": total,
            "category_counts": categories,
            "top_symbols": [{"symbol": k, "count": v} for k, v in top_symbols],
            "top_channels": [{"channel": k, "count": v} for k, v in top_channels],
        }
    finally:
        db.close()


def delete_recommendation(rec_id: int) -> bool:
    """Delete a single recommendation record by ID."""
    db = SessionLocal()
    try:
        record = db.query(RecommendationModel).filter_by(id=rec_id).first()
        if record:
            db.delete(record)
            db.commit()
            logger.info("Deleted recommendation ID %d", rec_id)
            return True
        return False
    except Exception as exc:
        db.rollback()
        logger.error("Failed to delete recommendation ID %d: %s", rec_id, exc)
        return False
    finally:
        db.close()


def clear_all_recommendations() -> int:
    """Clear all recommendation records from the database."""
    db = SessionLocal()
    try:
        count = db.query(RecommendationModel).delete()
        db.commit()
        logger.info("Cleared all %d recommendations from database", count)
        return count
    except Exception as exc:
        db.rollback()
        logger.error("Failed to clear recommendations: %s", exc)
        return 0
    finally:
        db.close()

