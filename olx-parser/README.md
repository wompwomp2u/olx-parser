# OLX.ua ThinkPad Watcher

Polls the OLX.ua public API every few minutes and sends **new** ThinkPad laptop
listings to your Telegram. Currently watches: **T14, T14s, E14, X1 Carbon Gen 9+**.

Each alert includes the **CPU, RAM and storage**, parsed from the listing title
(and the description as a fallback). The parser handles the many ways people write
specs — `i5-1135G7`, `Ryzen 5 PRO 4650U`, `Core Ultra 7`, `16/512`, `16Gb DDR4`,
`SSD 240 Gb`, `nvme256`, `1000gb`, `1TB`, `32 RAM`, etc.

Parts, chargers, keyboards and broken units are filtered out automatically.

---

## 1. One-time setup

### a) Install the dependency
```
pip install -r requirements.txt
```

### b) Create a Telegram bot (~2 minutes)
1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`, follow the prompts, and copy the **bot token** it gives you
   (looks like `123456789:AAH...`).
3. Open a chat with your new bot and send it any message (e.g. "hi").
   This is required so the bot is allowed to message you.
4. Get your **chat id**: open this URL in a browser (paste your token in):
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":123456789,...}` — that number is your chat id.

### c) Fill in `config.json`
```json
"telegram": {
  "bot_token": "123456789:AAH...",
  "chat_id": "123456789"
}
```

---

## 2. Run it

```
python olx_parser.py
```

### Try it without a Telegram bot first
```
python olx_parser.py --dry-run
```
Fetches once and prints every matching listing to the console instead of sending
it. Doesn't touch `seen_ids.json`, so your real run later is unaffected. No bot
token required.

Or use **run.bat** (double-click) — it auto-restarts the script if it ever crashes.

The **first run** silently records all current matching listings so you don't get
spammed with the ~130-listing backlog. From then on you only get **brand-new**
listings as they appear. Want the current backlog once? Set
`"send_existing_on_first_run": true` in config and delete `seen_ids.json` before
the first run.

---

## 3. Running 24/7 — free, no card, no PC (GitHub Actions)

Instead of keeping a machine on, GitHub runs the script for you on a schedule
(every 5 min) for free. No credit card, no server.

1. **Create a GitHub account** (if you don't have one) and a **new repository**.
   Make it **Public** — Actions minutes are unlimited & free for public repos.
   (Your bot token is NOT in the code — it lives in encrypted Secrets — so public
   is safe.)
2. **Push this project** to that repo (see commands below).
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**. Add two secrets:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat id
4. Open the **Actions** tab, enable workflows if prompted. The watcher
   (`.github/workflows/watch.yml`) now runs every 5 minutes automatically. Use
   **Run workflow** to trigger it once manually and test.

How it works: each run does a single poll (`olx_parser.py --once`) and exits. The
"already seen" list is carried between runs via GitHub's Actions cache, so you're
never notified twice. The very first run silently records current listings (no
spam); after that you only get new ones.

Notes:
- GitHub's minimum schedule is **5 minutes**, and runs can be delayed a few
  minutes under load — fine for a used-laptop watcher.
- Scheduled workflows auto-pause after **60 days of no repo activity**; any commit
  re-enables them.

### Push commands
```
cd d:\code\olx-parser
git init
git add .
git commit -m "OLX ThinkPad watcher"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

---

## 4. Alternative: Running 24/7 on Windows

Pick one:

- **Simplest:** leave `run.bat` running in a terminal window. It restarts on crash.
- **Survives logout / starts on boot — Task Scheduler:**
  1. Open *Task Scheduler* → *Create Task*.
  2. General: check *Run whether user is logged on or not*.
  3. Triggers: *New* → *At startup*.
  4. Actions: *New* → Program: `python`, Arguments: `olx_parser.py`,
     Start in: `d:\code\olx-parser`.
  5. Settings: check *If the task fails, restart every 1 minute*.

---

## 5. Customizing

Everything lives in `config.json`:

| Field | What it does |
|-------|--------------|
| `search.poll_interval_seconds` | How often to check (default 300 = 5 min). |
| `search.query` | OLX search term. `"thinkpad"` covers all of them. |
| `search.max_price_uah` | Set a number to ignore listings above that price (UAH). `null` = no limit. |
| `models[]` | Which models to match. `include`/`exclude` are lowercase keyword lists; `min_gen` enforces a minimum generation (used for X1 Carbon). |
| `exclude_keywords` | Global blocklist — any listing whose title contains one of these is skipped (parts, chargers, etc.). |

To add a model, e.g. P14s, add an entry:
```json
{"name": "ThinkPad P14s", "include": ["p14s"], "exclude": []}
```

### Files created at runtime
- `seen_ids.json` — listings already processed (so you aren't notified twice).
- `olx_parser.log` — activity log.
