"""
parser.py
---------
Hybrid Signal Parser Engine for Indian Stock & Option Recommendations.
Uses high-performance Regex pattern matching for standard advisory formats,
and falls back to Gemini API (LLM) for complex, unstructured, or research text.
"""

import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from config import GEMINI_API_KEY, GEMINI_MODEL_NAME
from models import (
    RecommendationSchema, CategoryEnum, ActionEnum, OptionTypeEnum
)

logger = logging.getLogger("stock_recommendation.parser")

# Popular Indian Indices & Stock Symbols regex cache
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}


class SignalParser:
    """Hybrid advisory message parser."""

    def __init__(self, use_gemini_fallback: bool = True):
        self.use_gemini_fallback = use_gemini_fallback
        self.gemini_client = None

        if self.use_gemini_fallback and GEMINI_API_KEY:
            try:
                # Try google-genai SDK first
                from google import genai
                self.gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                self.sdk_type = "google-genai"
                logger.info("Initialized Gemini API using google-genai SDK.")
            except ImportError:
                try:
                    # Fallback to google-generativeai SDK
                    import google.generativeai as genai
                    genai.configure(api_key=GEMINI_API_KEY)
                    self.gemini_client = genai.GenerativeModel(GEMINI_MODEL_NAME)
                    self.sdk_type = "google-generativeai"
                    logger.info("Initialized Gemini API using google-generativeai SDK.")
                except Exception as exc:
                    logger.warning("Failed to initialize Gemini SDK: %s", exc)

    def parse_message(
        self,
        text: str,
        source_channel: str,
        timestamp: Optional[str] = None
    ) -> RecommendationSchema:
        """
        Main entrypoint: parses raw text into a standardized RecommendationSchema.
        Attempts regex parsing first; if regex confidence is low or text is complex,
        invokes Gemini API if available.
        """
        if not text or not text.strip():
            return self._build_fallback(text or "", source_channel, timestamp)

        clean_text = text.strip()
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        # Step 1: Run deterministic Regex extraction
        regex_res = self._parse_with_regex(clean_text)

        # Step 2: Determine if Gemini fallback is needed
        # Fallback is triggered if:
        # 1. Symbol is missing or UNKNOWN
        # 2. Category is REPORT (often unstructured long text)
        # 3. Targets and Stop Loss are both missing for OPTION/BTST calls
        needs_fallback = (
            regex_res["symbol"] == "UNKNOWN" or
            regex_res["category"] == CategoryEnum.REPORT or
            (regex_res["category"] in (CategoryEnum.OPTION, CategoryEnum.BTST) and not regex_res["targets"] and not regex_res["stop_loss"])
        )

        if needs_fallback and self.gemini_client:
            logger.info("Regex extraction incomplete for channel '%s'. Calling Gemini API...", source_channel)
            gemini_res = self._parse_with_gemini(clean_text)
            if gemini_res:
                gemini_res["source_channel"] = source_channel
                gemini_res["raw_text"] = clean_text
                gemini_res["timestamp"] = ts
                try:
                    return RecommendationSchema(**gemini_res)
                except Exception as val_err:
                    logger.warning("Gemini output validation error: %s. Using regex output.", val_err)

        # Build schema from regex result
        return RecommendationSchema(
            symbol=regex_res["symbol"],
            category=regex_res["category"],
            action=regex_res["action"],
            option_type=regex_res["option_type"],
            strike_price=regex_res["strike_price"],
            expiry=regex_res["expiry"],
            entry_range=regex_res["entry_range"],
            targets=regex_res["targets"],
            stop_loss=regex_res["stop_loss"],
            raw_text=clean_text,
            source_channel=source_channel,
            timestamp=ts
        )

    def _parse_with_regex(self, text: str) -> Dict[str, Any]:
        """Regex-based rule engine for standard advisory calls."""
        upper_text = text.upper()

        # Comprehensive set of non-symbol noise words common in Telegram market channels
        ignored_words = {
            "BUY", "SELL", "BTST", "STBT", "CALL", "PUT", "STOP", "LOSS", "STOPLOSS",
            "TARGET", "TARGETS", "TGT", "ABOVE", "BELOW", "ENTRY", "RANGE", "TODAY", "TOMORROW",
            "LONG", "SHORT", "TERM", "HOLD", "PICK", "STOCK", "NOW", "CMP", "PRICE", "EXIT",
            "BOOK", "PROFIT", "SUPER", "POWER", "CALLS", "VIP", "FREE", "HERO", "ZERO", "JACKPOT",
            "EQUITY", "ALERT", "ALERTS", "GOOD", "MORNING", "DAILY", "WEEKLY", "MONTHLY",
            "LEVEL", "LEVELS", "SPOT", "FUTURE", "FUTURES", "CHART", "DISCLAIMER", "TRADE",
            "TRADING", "ADVISORY", "STRONG", "BREAKOUT", "BOOM", "ROCKET", "FAST", "VERY",
            "HIGH", "LOW", "RISK", "INTRADAY", "POSITIONAL", "NEW", "JOIN", "TELEGRAM",
            "CHANNEL", "GROUP", "THIS", "THAT", "WITH", "FROM", "FOR", "AND", "THE", "YOU",
            "YOUR", "OUR", "WILL", "HIT", "DONE", "BEST", "SURE", "SURESHOT", "GAIN", "GAINS",
            "SAFE", "PREMIUM", "PAID", "REPORT", "UPDATE", "NEWS", "VIEW", "ANALYSIS",
            "INTRADAY", "SWING", "CALL", "PUTS", "OPTION", "OPTIONS", "INDEX", "INDICES",
            "FILE", "DOWNLOAD", "IMAGES", "TRADER"
        }

        # 1. Category Classification
        category = CategoryEnum.OPTION
        
        # Any big paragraph (many lines or long text) should go to Reports & Research
        if len(text) > 300 or text.count('\n') >= 5:
            category = CategoryEnum.REPORT
        elif re.search(r"\b(BTST|STBT|BUY TODAY|SELL TOMORROW)\b", upper_text):
            category = CategoryEnum.BTST
        elif re.search(r"\b(INVESTMENT|LONG TERM|FUNDAMENTAL|MULTIPACKER|TARGET \d+ MONTHS|BUY AND HOLD)\b", upper_text):
            category = CategoryEnum.INVESTMENT
        elif re.search(r"\b(REPORT|MARKET UPDATE|RESEARCH|MORNING BRIEF|NIFTY VIEW|NEWS|UPDATE|GOOD MORNING|STOCK IN NEWS|FILE|DOWNLOAD|IMAGES|TRADER)\b", upper_text):
            category = CategoryEnum.REPORT
        elif re.search(r"\b(CE|PE|CALL|PUT)\b", upper_text) or any(idx in upper_text for idx in INDEX_SYMBOLS):
            category = CategoryEnum.OPTION
        else:
            category = CategoryEnum.OPTION

        # 2. Action Detection
        action = ActionEnum.BUY
        if re.search(r"\b(SELL|SHORT|PUT|EXIT|BOOK)\b", upper_text) and not re.search(r"\bBUY\b", upper_text):
            action = ActionEnum.SELL

        # 3. Option Type
        option_type = None
        if re.search(r"\b(CE|CALL)\b", upper_text):
            option_type = OptionTypeEnum.CE
        elif re.search(r"\b(PE|PUT)\b", upper_text):
            option_type = OptionTypeEnum.PE

        # 4. Symbol & Strike Detection
        symbol = "UNKNOWN"
        strike_price = None

        # Step 4a: Check index symbols first
        for idx in INDEX_SYMBOLS:
            if re.search(r"\b" + idx + r"\b", upper_text):
                symbol = idx
                break

        # Step 4b: Match explicit option structure e.g. "BUY SBIN 800 CE" or "SBIN 800 CE" or "RELIANCE 3000 CALL"
        op_match = re.search(r"(?:BUY|SELL)?\s*\b([A-Z0-9]{3,15})\s+(\d{2,6}(?:\.\d+)?)\s*(CE|PE|CALL|PUT)\b", upper_text)
        if op_match:
            cand_sym = op_match.group(1).strip()
            if cand_sym not in ignored_words and cand_sym not in INDEX_SYMBOLS:
                symbol = cand_sym
                try:
                    strike_price = float(op_match.group(2))
                except ValueError:
                    pass
                if not option_type:
                    option_type = OptionTypeEnum.CE if op_match.group(3) in ("CE", "CALL") else OptionTypeEnum.PE

        # Step 4c: Match direct stock action e.g. "BUY TATAMOTORS", "SELL INFY CMP", "#SBIN"
        if symbol == "UNKNOWN":
            act_match = re.search(r"\b(?:BUY|SELL)\s+#?([A-Z0-9]{3,15})\b", upper_text)
            if act_match:
                cand_sym = act_match.group(1).strip()
                if cand_sym not in ignored_words and cand_sym not in INDEX_SYMBOLS:
                    symbol = cand_sym

        # Step 4d: Match hashtags e.g. "#SBIN", "$RELIANCE"
        if symbol == "UNKNOWN":
            hash_match = re.search(r"[#\$]([A-Z0-9]{3,15})\b", text)
            if hash_match:
                cand_sym = hash_match.group(1).upper()
                if cand_sym not in ignored_words and cand_sym not in INDEX_SYMBOLS:
                    symbol = cand_sym

        # Step 4e: Generic token search filtering out ignored words
        if symbol == "UNKNOWN":
            tokens = re.findall(r"\b[A-Z]{3,15}\b", upper_text)
            for tok in tokens:
                if tok not in ignored_words and tok not in INDEX_SYMBOLS and len(tok) >= 3:
                    symbol = tok
                    break

        # 5. Strike Price fallback
        if not strike_price:
            strike_match = re.search(r"\b(\d{4,5})\s*(?:CE|PE|CALL|PUT)?\b", upper_text)
            if strike_match and symbol in INDEX_SYMBOLS:
                try:
                    strike_price = float(strike_match.group(1))
                except ValueError:
                    pass
            elif option_type:
                strike_match_generic = re.search(r"\b(\d{2,5}(?:\.\d+)?)\s*(?:CE|PE)\b", upper_text)
                if strike_match_generic:
                    try:
                        strike_price = float(strike_match_generic.group(1))
                    except ValueError:
                        pass

        # 6. Expiry
        expiry = None
        expiry_match = re.search(
            r"\b(\d{1,2}\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*\d{0,4})\b",
            upper_text
        )
        if expiry_match:
            expiry = expiry_match.group(1).replace(" ", "")

        # 7. Entry Range
        entry_range = None
        entry_match = re.search(
            r"(?:ENTRY|BUY|PRICE|CMP|ABOVE|RANGE|@|AT)\s*:?\s*(\d+(?:\.\d+)?)\s*(?:-|TO|/)?\s*(\d+(?:\.\d+)?)?",
            upper_text
        )
        if entry_match:
            try:
                val1 = float(entry_match.group(1))
                if entry_match.group(2):
                    val2 = float(entry_match.group(2))
                    entry_range = [min(val1, val2), max(val1, val2)]
                else:
                    entry_range = [val1]
            except ValueError:
                pass

        # 8. Targets
        targets = []
        target_matches = re.findall(
            r"(?:TGT|TARGET|T1|T2|T3|TP)\s*:?\s*(\d+(?:\.\d+)?)\s*(?:/|,|AND|\s+)?\s*(\d+(?:\.\d+)?)?\s*(?:/|,|AND|\s+)?\s*(\d+(?:\.\d+)?)?",
            upper_text
        )
        if target_matches:
            for group in target_matches:
                for val in group:
                    if val:
                        try:
                            flt = float(val)
                            if flt not in targets and flt > 0:
                                targets.append(flt)
                        except ValueError:
                            pass
        targets = sorted(targets) if targets else None

        # 9. Stop Loss
        stop_loss = None
        sl_match = re.search(r"(?:SL|STOP\s*LOSS|STOPLOSS)\s*:?\s*(\d+(?:\.\d+)?)", upper_text)
        if sl_match:
            try:
                stop_loss = float(sl_match.group(1))
            except ValueError:
                pass

        return {
            "symbol": symbol,
            "category": category,
            "action": action,
            "option_type": option_type,
            "strike_price": strike_price,
            "expiry": expiry,
            "entry_range": entry_range,
            "targets": targets,
            "stop_loss": stop_loss,
        }

    def _parse_with_gemini(self, text: str) -> Optional[Dict[str, Any]]:
        """Invokes Gemini LLM with JSON schema prompting to parse messy messages."""
        prompt = f"""
Act as a financial NLP extraction engine for Indian stock market advisory calls.
Extract the structured fields from the raw Telegram text into a JSON object matching this schema:

{{
  "symbol": "NSE Stock or Index Ticker e.g. TATASTEEL, NIFTY, BANKNIFTY",
  "category": "OPTION" | "BTST" | "INVESTMENT" | "REPORT",
  "action": "BUY" | "SELL",
  "option_type": "CE" | "PE" | null,
  "strike_price": 160.0 | null,
  "expiry": "29AUG2024" | null,
  "entry_range": [158.0, 160.0] | null,
  "targets": [165.0, 170.0, 175.0] | null,
  "stop_loss": 154.0 | null
}}

Guidelines:
- category must be OPTION (intraday/options calls), BTST (buy today sell tomorrow), INVESTMENT (multi-month picks), or REPORT (market analysis/pdf research).
- Output strictly valid JSON. Do not include markdown codeblock tags ```json or extra commentary.

Raw Message:
"{text}"
"""
        try:
            raw_response = ""
            if self.sdk_type == "google-genai":
                response = self.gemini_client.models.generate_content(
                    model=GEMINI_MODEL_NAME,
                    contents=prompt,
                )
                raw_response = response.text
            elif self.sdk_type == "google-generativeai":
                response = self.gemini_client.generate_content(prompt)
                raw_response = response.text

            # Clean JSON formatting
            clean_json = raw_response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(clean_json)
            return data
        except Exception as exc:
            logger.error("Gemini API call failed: %s", exc)
            return None

    def _build_fallback(self, text: str, source_channel: str, timestamp: Optional[str]) -> RecommendationSchema:
        return RecommendationSchema(
            symbol="UNKNOWN",
            category=CategoryEnum.REPORT,
            action=ActionEnum.BUY,
            raw_text=text,
            source_channel=source_channel,
            timestamp=timestamp or datetime.now(timezone.utc).isoformat()
        )
