# Company News Bot

Daily monitoring for a list of companies using Google News RSS, with Telegram delivery. Free, cron-friendly, and easy to extend.

## Features
- Google News RSS per company
- 48-hour lookback (configurable)
- Strict relevance filtering (company name + keyword in title, noise blocking)
- Short summaries per item
- Telegram delivery
- Telegram commands: `/add`, `/list`, `/update`, `/help`
- Telegram command: `/broad on|off|status`
- Optional LLM materiality summary (OpenAI)

## Modes

### Strict mode (default)
Company items must pass all of:
- Company name appears in title
- Material keyword appears in title
- Not noisy or speculative (blocks opinion/rumor/preview/expected/etc.)
- Plus at least one of:
  - number present
  - material verb (awarded/approved/signed/etc.)
  - official source domain (sec.gov, fda.gov, IR sites)

### Broad mode (toggle in Telegram)
Use `/broad on` to relax filters for company items:
- Keywords can match in summary (not just title)
- The “number/verb/official domain” gate is skipped
Noise/forward-looking filters still apply.

Use `/broad off` to return to strict mode.

### General Market Movers
Macro RSS queries are always included and routed to the “General Market Movers” section in the LLM summary.

## Requirements
- Python 3
- Ubuntu 18+ (tested)

## Setup

1. Install dependencies:
   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create env file (keep it outside the repo):
   ```bash
   mkdir -p ~/.config
   cat <<'EOF' > ~/.config/company_news.env
   TELEGRAM_BOT_TOKEN=YOUR_TOKEN
   TELEGRAM_CHAT_ID=YOUR_CHAT_ID
   COMPANIES_JSON=/path/to/CompaniesToFollow/company_watchlist_unique.json
  TELEGRAM_STATE_JSON=/path/to/CompaniesToFollow/data/telegram_state.json
  LOOKBACK_HOURS=48
  MAX_PER_COMPANY=3
  NEWS_LANG=en
  # NEWS_GEO=US   # optional, leave unset for global
  ENABLE_COMMANDS=1
  COMMANDS_ONLY=0
  BROAD_MODE=0
  USE_LLM=0
  OPENAI_API_KEY=YOUR_OPENAI_KEY
  OPENAI_MODEL=gpt-4.1-mini
  EOF
  ```

3. Test:
   ```bash
   . ~/.config/company_news.env
   .venv/bin/python scripts/news_bot.py
   ```

## Cron

Daily report at 9:00 AM Central time:
```bash
0 9 * * * TZ=America/Chicago . ~/.config/company_news.env; /path/to/CompaniesToFollow/.venv/bin/python /path/to/CompaniesToFollow/scripts/news_bot.py >> /path/to/CompaniesToFollow/logs/cron.log 2>&1
```

Command poll every 5 minutes (enables `/update`):
```bash
*/5 * * * * TZ=America/Chicago COMMANDS_ONLY=1 . ~/.config/company_news.env; /path/to/CompaniesToFollow/.venv/bin/python /path/to/CompaniesToFollow/scripts/news_bot.py >> /path/to/CompaniesToFollow/logs/cron.log 2>&1
```

## Telegram privacy
- If you only use a private chat with the bot, privacy settings don’t matter.
- If the bot is in a group, privacy can stay enabled since we only send messages.

## Notes
- The bot token must never be committed to GitHub.
- `data/telegram_state.json` is local state and ignored by git.
