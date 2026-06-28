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
        # number right after "carbon" is the most reliable gen for X1 Carbon
        # ("carbon 7", "carbon 3th") — checked first so "7 Gen 14\"" reads 7, not 14
        r"carbon[\s\-]+(\d{1,2})(?:th|st|nd|rd)?\b",
        r"(\d{1,2})\s*(?:th|nd|rd|st)?\s*gen",   # "7 Gen", "9th gen"
        r"gen[\s\-]?(\d{1,2})",                   # "Gen 9"
        r"\bg(\d{1,2})\b",                        # "G10"
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            return int(m.group(1))
    return None


# Any recognizable ThinkPad model code, e.g. t490, x13, e15, l14, p14s, a285,
# w541, z13, plus the named lines. Used to tell "model specified" from a generic
# "Lenovo ThinkPad" listing with no model in the title.
HAS_MODEL_CODE = re.compile(
    r"(?<![a-z0-9])(?:[txelpwra]\d{2,4}[a-z]{0,2}|x1|p1|z1[36]|11e|"
    r"yoga|carbon|nano|extreme|helix)(?![a-z0-9])"
)


def _kw_present(keyword: str, text: str) -> bool:
    """Match a keyword as a whole model token.

    Not preceded by a letter/digit and not followed by a digit, so "x1" matches
    "x1 carbon"/"x1carbon"/"x1 yoga" but NOT "x13"; "t14" still matches "t14s"
    (which the model's exclude list then handles).
    """
    return re.search(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?!\d)", text) is not None


def match_model(title: str, models: list) -> str | None:
    """Return the friendly model name if the title matches one of our targets."""
    t = title.lower()
    for model in models:
        if any(_kw_present(kw, t) for kw in model.get("include", [])):
            if any(_kw_present(kw, t) for kw in model.get("exclude", [])):
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


def fetch_offers(query: str, max_price=None, max_results=None) -> list:
    """Page through OLX search results for one query and return its offers.

    OLX caps pagination at ~1040 results and ignores `limit` (returns a variable
    page size), so offsets advance by the real batch length. The price filter
    (filter_float_price:to) shrinks the set server-side. `max_results` bounds how
    deep we page per run: new listings surface near the top, so a few hundred is
    enough to catch them cheaply while keeping each poll fast.
    """
    limit = min(MAX_OFFSET, max_results or MAX_OFFSET)
    offers, offset = [], 0
    while offset < limit:
        params = {"offset": offset, "limit": PAGE_LIMIT, "query": query}
        if max_price is not None:
            params["filter_float_price:to"] = max_price
        try:
            r = requests.get(API_URL, params=params, headers=HTTP_HEADERS, timeout=30)
            r.raise_for_status()
            batch = r.json().get("data", [])
        except (requests.RequestException, ValueError) as exc:
            log.warning("fetch failed (%s) at offset %d: %s", query, offset, exc)
            break
        if not batch:
            break
        offers.extend(batch)
        offset += len(batch)
        time.sleep(0.3)  # be polite to OLX
    return offers


def fetch_all(queries: list, max_price=None, max_results=None) -> list:
    """Run every query, de-duplicate by id, and return newest-first.

    OLX has no reliable date sort, so we sort client-side on created_time.
    """
    by_id = {}
    for q in queries:
        for o in fetch_offers(q, max_price, max_results):
            oid = o.get("id")
            if oid is not None and oid not in by_id:
                by_id[oid] = o
    offers = list(by_id.values())
    offers.sort(key=lambda o: o.get("created_time") or "", reverse=True)
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


def esc(text: str) -> str:
    """Escape the characters Telegram's HTML parse mode cares about."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(model_name: str, offer: dict, spec: dict, broken: bool = False) -> str:
    _, price_label = get_price(offer)
    title = offer.get("title", "ThinkPad")
    url = offer.get("url", "")

    # A monospace "table"; missing values are left blank.
    rows = [
        ("CPU", spec.get("cpu") or ""),
        ("RAM", spec.get("ram") or ""),
        ("Storage", spec.get("storage") or ""),
        ("Price", price_label or ""),
    ]
    table = "\n".join(f"{label:<8} {value}" for label, value in rows)

    header = f"💻 <b>{esc(model_name)}</b>"
    if broken:
        header += "  🔧 <b>НА ЗАПЧАСТИНИ / НЕСПРАВНИЙ</b>"

    return (
        f"{header}\n"
        f"{esc(title)}\n"
        f"<pre>{esc(table)}</pre>\n"
        f"{url}"
    )


# --------------------------------------------------------------------------- #
# Core loop
# --------------------------------------------------------------------------- #
def run_once(cfg: dict, seen: set, first_run: bool, dry_run: bool = False) -> int:
    models = cfg["models"]
    parts_keywords = cfg.get("parts_keywords", [])
    broken_keywords = cfg.get("broken_keywords", [])
    include_unspecified = cfg.get("include_unspecified_thinkpad", False)
    exclude_cpus = [c.lower() for c in cfg.get("exclude_cpus", [])]
    max_price = cfg["search"].get("max_price_uah")
    send_existing = cfg.get("send_existing_on_first_run", True)
    # In dry-run we always "notify" (print) so you can see matches immediately.
    notify = dry_run or (not first_run) or send_existing

    queries = cfg["search"].get("queries") or [cfg["search"].get("query", "thinkpad")]
    max_results = cfg["search"].get("max_results_per_query")
    offers = fetch_all(queries, max_price, max_results)
    log.info("fetched %d unique offers (newest first)", len(offers))

    new_count = 0
    for offer in offers:
        oid = offer.get("id")
        if oid is None or oid in seen:
            continue

        title = offer.get("title", "")
        title_l = title.lower()

        # Pure component listings (a keyboard, screen, charger...) are not laptops.
        if any(kw in title_l for kw in parts_keywords):
            seen.add(oid)
            continue

        model_name = match_model(title, models)
        if not model_name:
            # Generic "Lenovo ThinkPad" with no model in the title -> still show.
            if include_unspecified and "thinkpad" in title_l \
                    and not HAS_MODEL_CODE.search(title_l):
                model_name = "ThinkPad (модель не вказана)"
            else:
                seen.add(oid)  # remember non-matches so we don't re-check them
                continue

        # Whole-but-broken laptops are kept and flagged (repair/resale candidates).
        broken = any(kw in title_l for kw in broken_keywords)

        spec = specs.parse_specs(title, strip_html(offer.get("description", "")))

        # Skip listings whose CPU is on the blocklist (e.g. older/weaker chips).
        if spec["cpu"] and spec["cpu"].lower() in exclude_cpus:
            seen.add(oid)
            continue

        if max_price is not None:
            price_val, _ = get_price(offer)
            if price_val is not None and price_val > max_price:
                seen.add(oid)
                continue

        if notify:
            message = format_message(model_name, offer, spec, broken)
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

    log.info("OLX ThinkPad watcher started (interval=%ds, first_run=%s)",
             interval, first_run)

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
