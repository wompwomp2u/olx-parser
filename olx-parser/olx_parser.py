#!/usr/bin/env python3
"""OLX.ua ThinkPad watcher.

Polls the OLX public API for ThinkPad laptop listings, keeps only the models
you care about, and pushes brand-new listings to a Telegram chat. Designed to
run 24/7 as a loop (see run.bat / the Task Scheduler notes in README.md).
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

import specs

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SEEN_PATH = BASE_DIR / "seen_ids.json"
LOG_PATH = BASE_DIR / "olx_parser.log"

API_URL = "https://www.olx.ua/api/v1/offers/"
PAGE_LIMIT = 50          # OLX caps page size at 50
MAX_OFFSET = 1000        # how deep to paginate per poll
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("olx")


# --------------------------------------------------------------------------- #
# Config / persistence
# --------------------------------------------------------------------------- #
def load_config(require_telegram: bool = True) -> dict:
    if not CONFIG_PATH.exists():
        log.error("config.json not found at %s", CONFIG_PATH)
        sys.exit(1)
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = json.load(fh)

    # Let environment variables override the file — this is how the token/chat id
    # are supplied in CI (GitHub Secrets) so they never live in the repo.
    tg = cfg.setdefault("telegram", {})
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        tg["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        tg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]

    if require_telegram and (
        "PUT_YOUR" in tg.get("bot_token", "") or "PUT_YOUR" in str(tg.get("chat_id", ""))
    ):
        log.error("Telegram bot_token / chat_id not set in config.json. "
                  "See README.md for the 2-minute setup.")
        sys.exit(1)
    return cfg


def load_seen() -> set:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            log.warning("seen_ids.json unreadable, starting fresh")
    return set()


def save_seen(seen: set) -> None:
    tmp = SEEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    os.replace(tmp, SEEN_PATH)  # atomic, survives a crash mid-write


# --------------------------------------------------------------------------- #
# Model matching
# --------------------------------------------------------------------------- #
def detect_gen(title: str) -> int | None:
    """Pull an X1 Carbon generation number out of a title, if present.

    Handles 'gen 9', 'gen9', 'g9', '9th gen', '9 gen'. Returns None when the
    title gives no generation hint.
    """
    t = title.lower()
    patterns = [
        r"gen[\s\-]?(\d{1,2})",
        r"\bg(\d{1,2})\b",
        r"(\d{1,2})\s*(?:th|nd|rd|st)?\s*gen",
        # number right after "carbon", incl. odd ordinals like "carbon 3th"
        r"carbon[\s\-]+(\d{1,2})(?:th|st|nd|rd)?\b",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            return int(m.group(1))
    return None


def match_model(title: str, models: list, exclude_keywords: list = ()) -> str | None:
    """Return the friendly model name if the title matches one of our targets.

    Listings whose title contains a global exclude keyword (parts, chargers,
    broken units, etc.) are skipped entirely.
    """
    t = title.lower()
    if any(kw in t for kw in exclude_keywords):
        return None
    for model in models:
        if any(kw in t for kw in model.get("include", [])):
            if any(kw in t for kw in model.get("exclude", [])):
                continue
            min_gen = model.get("min_gen")
            if min_gen is not None:
                gen = detect_gen(title)
                # Unknown gen -> let it through (better to over-notify than miss),
                # but a known gen below the floor is rejected.
                if gen is not None and gen < min_gen:
                    continue
            return model["name"]
    return None


# --------------------------------------------------------------------------- #
# OLX API
# --------------------------------------------------------------------------- #
def get_price(offer: dict):
    for p in offer.get("params", []):
        if p.get("key") == "price":
            val = p.get("value", {})
            return val.get("value"), val.get("label", "")
    return None, ""


def fetch_offers(query: str) -> list:
    """Page through OLX search results for the query and return all offers."""
    offers, offset = [], 0
    while offset < MAX_OFFSET:
        params = {"offset": offset, "limit": PAGE_LIMIT, "query": query}
        try:
            r = requests.get(API_URL, params=params, headers=HTTP_HEADERS, timeout=30)
            r.raise_for_status()
            batch = r.json().get("data", [])
        except (requests.RequestException, ValueError) as exc:
            log.warning("fetch failed at offset %d: %s", offset, exc)
            break
        if not batch:
            break
        offers.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(0.5)  # be polite to OLX
    return offers


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(cfg: dict, text: str) -> bool:
    tg = cfg["telegram"]
    url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
    payload = {
        "chat_id": tg["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
        return False


def strip_html(text: str) -> str:
    """Turn OLX's HTML description into plain text for spec parsing."""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def format_message(model_name: str, offer: dict) -> str:
    price_val, price_label = get_price(offer)
    title = offer.get("title", "ThinkPad")
    url = offer.get("url", "")
    price_line = price_label or "Ціна не вказана"

    # Parse specs from the title first, falling back to the description.
    spec = specs.parse_specs(title, strip_html(offer.get("description", "")))
    spec_bits = []
    if spec["cpu"]:
        spec_bits.append(f"🧠 {spec['cpu']}")
    if spec["ram"]:
        spec_bits.append(f"📦 {spec['ram']} RAM")
    if spec["storage"]:
        spec_bits.append(f"💾 {spec['storage']}")
    spec_line = ("\n" + "  ".join(spec_bits)) if spec_bits else ""

    return (
        f"💻 <b>{model_name}</b>\n"
        f"{title}"
        f"{spec_line}\n"
        f"💰 {price_line}\n"
        f"{url}"
    )


# --------------------------------------------------------------------------- #
# Core loop
# --------------------------------------------------------------------------- #
def run_once(cfg: dict, seen: set, first_run: bool, dry_run: bool = False) -> int:
    models = cfg["models"]
    exclude_keywords = cfg.get("exclude_keywords", [])
    max_price = cfg["search"].get("max_price_uah")
    send_existing = cfg.get("send_existing_on_first_run", True)
    # In dry-run we always "notify" (print) so you can see matches immediately.
    notify = dry_run or (not first_run) or send_existing

    offers = fetch_offers(cfg["search"]["query"])
    log.info("fetched %d offers", len(offers))

    new_count = 0
    for offer in offers:
        oid = offer.get("id")
        if oid is None or oid in seen:
            continue

        model_name = match_model(offer.get("title", ""), models, exclude_keywords)
        if not model_name:
            seen.add(oid)  # remember non-matches too, so we don't re-check them
            continue

        if max_price is not None:
            price_val, _ = get_price(offer)
            if price_val is not None and price_val > max_price:
                seen.add(oid)
                continue

        if notify:
            message = format_message(model_name, offer)
            if dry_run:
                log.info("[DRY-RUN] would send:\n%s", message)
                new_count += 1
            elif send_telegram(cfg, message):
                log.info("sent: %s | %s", model_name, offer.get("title"))
                new_count += 1
                time.sleep(1)  # stay under Telegram rate limits
            else:
                continue  # don't mark as seen if delivery failed; retry next poll
        seen.add(oid)

    if first_run and not send_existing and not dry_run:
        log.info("first run: seeded %d existing offers silently", len(seen))
    if not dry_run:
        save_seen(seen)  # dry-run never persists, so a real run still works later
    return new_count


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv

    cfg = load_config(require_telegram=not dry_run)
    seen = load_seen()
    first_run = len(seen) == 0
    interval = cfg["search"].get("poll_interval_seconds", 300)

    if dry_run:
        log.info("DRY-RUN: fetching once, printing matches, NOT sending Telegram "
                 "and NOT saving seen_ids.json")
        sent = run_once(cfg, seen, first_run, dry_run=True)
        log.info("DRY-RUN done: %d matching listing(s) found", sent)
        return

    if once:
        # One poll then exit. Used by GitHub Actions (cron) instead of the loop.
        log.info("ONCE mode: single poll (first_run=%s)", first_run)
        sent = run_once(cfg, seen, first_run)
        log.info("ONCE done: %d new listing(s) sent", sent)
        return

    log.info("OLX ThinkPad watcher started (query=%r, interval=%ds, first_run=%s)",
             cfg["search"]["query"], interval, first_run)

    while True:
        try:
            sent = run_once(cfg, seen, first_run)
            if sent:
                log.info("poll done: %d new listing(s) sent", sent)
        except Exception:  # noqa: BLE001 - keep the 24/7 loop alive no matter what
            log.exception("unexpected error during poll")
        first_run = False
        time.sleep(interval)


if __name__ == "__main__":
    main()
