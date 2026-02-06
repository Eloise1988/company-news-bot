#!/usr/bin/env python3
import os
import re
import sys
import html
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List

import feedparser
import requests
from dateutil import parser as dateparser


def _now_utc():
    return datetime.now(timezone.utc)


def _strip_html(text: str) -> str:
    if not text:
        return ""
    # Remove tags and collapse whitespace
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _truncate(text: str, max_len: int = 280) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\\s]", " ", text)
    text = re.sub(r"\\s+", " ", text).strip()
    return text


def _parse_dt(entry):
    for key in ("published", "updated"):
        val = entry.get(key)
        if val:
            try:
                return dateparser.parse(val)
            except Exception:
                continue
    return None


def _build_rss_url(query: str, lang: str = "en", geo: Optional[str] = None):
    # Google News RSS
    # Example: https://news.google.com/rss/search?q=Apple&hl=en
    base = "https://news.google.com/rss/search"
    params = [f"q={requests.utils.quote(query)}", f"hl={lang}"]
    if geo:
        params.append(f"gl={geo}")
        params.append(f"ceid={geo}:{lang}")
    return f"{base}?{'&'.join(params)}"


def _is_relevant(text: str, keywords_map: Dict[str, List[str]]):
    text_l = text.lower()
    matched = []
    for label, keywords in keywords_map.items():
        if any(k in text_l for k in keywords):
            matched.append(label)
    return matched


def _load_companies(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [c["name"] for c in data.get("companies", [])]


def _send_telegram_messages(token: str, chat_id: str, messages: List[str], parse_mode: Optional[str] = "HTML"):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for msg in messages:
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(url, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Telegram send failed: {resp.status_code} {resp.text}")
        time.sleep(1)


def _chunk_message(text: str, max_len: int = 3800):
    # Telegram hard limit is 4096; keep headroom
    chunks = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > max_len and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _extract_response_text(resp_json: dict) -> str:
    # Responses API returns a list of output items; pull the first output_text found.
    if "output" in resp_json:
        for item in resp_json.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return content.get("text", "")
    return resp_json.get("output_text", "") or ""


def _call_openai_summary(api_key: str, model: str, items: List[dict]) -> str:
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    sanitized_items = []
    for item in items:
        sanitized = dict(item)
        dt = sanitized.get("date")
        if dt is not None:
            sanitized["date"] = dt.isoformat()
        sanitized_items.append(sanitized)
    items_text = json.dumps(sanitized_items, ensure_ascii=False, indent=2)
    prompt = (
        "You are a buy-side analyst. Using only the provided news items, "
        "identify materially relevant events for investment decision-making. "
        "Only include items that describe concrete, completed events or official "
        "announcements with material impact. Prioritize: reported earnings/guidance "
        "(with actual results), major contracts/awards (signed/awarded), regulatory "
        "approvals/permits, M&A announcements, confirmed funding rounds, major product "
        "launches, material government actions (DoD/DOE/DIU).\n\n"
        "Exclude any item that is speculative, scheduled, or forward-looking without "
        "new facts, such as: 'expected to', 'set to announce', 'plans to', 'slides', "
        "'earnings date announced', 'previews', 'analyst expects', 'rumor', 'opinion'.\n\n"
        "Output exactly two sections:\n"
        "1) Company Watchlist\n"
        "2) General Market Movers\n\n"
        "For each company item, use this format:\n"
        "🔴|🟡|🟢 Company — headline — why it matters (1 short sentence)\n"
        "Use 🔴 for high materiality, 🟡 for medium, 🟢 for low.\n"
        "Include at most 1 item per company. If nothing material, omit the company.\n"
        "If there are no general market movers in the provided items, write 'None.'\n"
        "Use items where bucket=company for Company Watchlist, and bucket=market for General Market Movers.\n"
        "Do not add any facts not present in the input. No markdown, no HTML.\n\n"
        "News items JSON:\n"
        f"{items_text}\n"
    )

    payload = {
        "model": model,
        "input": prompt,
        "temperature": 0.2,
        "max_output_tokens": 900,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"OpenAI API failed: {resp.status_code} {resp.text}")
    text = _extract_response_text(resp.json()).strip()
    return text


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _handle_commands(token: str, chat_id: str, companies_path: str, state_path: str) -> bool:
    state = _load_json(state_path, {"last_update_id": 0, "broad_mode": False})
    offset = state.get("last_update_id", 0) + 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
    if not resp.ok:
        raise RuntimeError(f"Telegram getUpdates failed: {resp.status_code} {resp.text}")
    payload = resp.json()
    updates = payload.get("result", [])

    if not updates:
        return False

    companies_data = _load_json(companies_path, {"companies": [], "count": 0})
    companies = companies_data.get("companies", [])
    names = {c.get("name", "").lower(): c.get("name", "") for c in companies}
    next_id = max([c.get("id", 0) for c in companies] + [0]) + 1
    run_now = False

    for update in updates:
        state["last_update_id"] = update.get("update_id", state["last_update_id"])
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue

        from_chat_id = str(msg.get("chat", {}).get("id", ""))
        if from_chat_id != str(chat_id):
            # Ignore other chats for safety
            continue

        text = (msg.get("text") or "").strip()
        if not text:
            continue

        if text.lower().startswith("/add "):
            company = text[5:].strip()
            if not company:
                _send_telegram_messages(token, chat_id, ["Please provide a company name, e.g. /add Amazon"])
                continue
            if "<" in company or ">" in company:
                _send_telegram_messages(token, chat_id, ["Please provide a plain company name without < or >."])
                continue

            key = company.lower()
            if key in names:
                safe_name = html.escape(names[key])
                _send_telegram_messages(token, chat_id, [f"Already tracking: {safe_name}"])
                continue

            companies.append({"id": next_id, "name": company})
            names[key] = company
            next_id += 1
            companies_data["companies"] = companies
            companies_data["count"] = len(companies)
            _save_json(companies_path, companies_data)
            safe_company = html.escape(company)
            _send_telegram_messages(token, chat_id, [f"Added: {safe_company}"])

        elif text.lower().startswith("/list"):
            company_names = [c.get("name", "") for c in companies]
            if not company_names:
                _send_telegram_messages(token, chat_id, ["No companies are currently tracked."])
            else:
                msg = "Tracked companies:\n" + "\n".join(f"- {html.escape(n)}" for n in company_names)
                _send_telegram_messages(token, chat_id, _chunk_message(msg))

        elif text.lower().startswith("/help"):
            _send_telegram_messages(
                token,
                chat_id,
                [
                    "Commands:\n"
                    "/add COMPANY — add a company\n"
                    "/list — list companies\n"
                    "/update — run now\n"
                    "/broad on|off|status — toggle broad mode\n"
                    "/help — this help"
                ],
            )

        elif text.lower().startswith("/update"):
            run_now = True
            _send_telegram_messages(token, chat_id, ["Running update now…"])
        elif text.lower().startswith("/broad"):
            parts = text.split()
            if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == "status"):
                status = "ON" if state.get("broad_mode") else "OFF"
                _send_telegram_messages(token, chat_id, [f"Broad mode is {status}."])
            elif len(parts) == 2 and parts[1].lower() in ("on", "off"):
                state["broad_mode"] = parts[1].lower() == "on"
                status = "ON" if state.get("broad_mode") else "OFF"
                _send_telegram_messages(token, chat_id, [f"Broad mode set to {status}."])
            else:
                _send_telegram_messages(token, chat_id, ["Usage: /broad on|off|status"])

    _save_json(state_path, state)
    return run_now


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    companies_path = os.getenv("COMPANIES_JSON", "company_watchlist_unique.json")
    state_path = os.getenv("TELEGRAM_STATE_JSON", "data/telegram_state.json")
    lang = os.getenv("NEWS_LANG", "en")
    geo = os.getenv("NEWS_GEO")  # optional: e.g., US, GB, CA
    hours = int(os.getenv("LOOKBACK_HOURS", "48"))
    commands_enabled = os.getenv("ENABLE_COMMANDS", "1") == "1"
    commands_only = os.getenv("COMMANDS_ONLY", "0") == "1"
    max_per_company = int(os.getenv("MAX_PER_COMPANY", "3"))
    use_llm = os.getenv("USE_LLM", "0") == "1"
    openai_api_key = os.getenv("OPENAI_API_KEY")
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    env_broad_mode = os.getenv("BROAD_MODE", "0") == "1"

    run_now = False
    if commands_enabled:
        run_now = _handle_commands(token, chat_id, companies_path, state_path)
    if commands_only and not run_now:
        return
    state = _load_json(state_path, {"last_update_id": 0, "broad_mode": False})
    broad_mode = env_broad_mode or state.get("broad_mode", False)

    keywords_map = {
        "Funding": ["funding", "raised", "series", "seed", "round", "investment", "investor"],
        "Contracts": ["contract", "award", "awarded", "deal", "partnership", "agreement", "orders"],
        "Regulatory": ["regulatory", "approval", "approved", "license", "licensed", "permit", "fda"],
        "Product": ["launch", "released", "unveiled", "product", "breakthrough", "prototype"],
        "Earnings": ["earnings", "results", "quarter", "q1", "q2", "q3", "q4", "guidance"],
    }
    noise_words = [
        "opinion",
        "rumor",
        "rumour",
        "speculation",
        "blog",
        "podcast",
        "interview",
        "sponsored",
        "advertisement",
        "promo",
        "review",
        "analysis",
        "explainer",
    ]
    forward_looking_phrases = [
        "expected to",
        "set to",
        "plans to",
        "scheduled to",
        "announces date",
        "announces dates",
        "earnings date",
        "preview",
        "previews",
        "analyst expects",
        "forecast",
        "guidance expected",
        "could",
        "may",
        "might",
    ]
    material_verbs = [
        "awarded",
        "approved",
        "announced",
        "closed",
        "signed",
        "acquired",
        "funded",
        "sec filing",
        "sec filings",
        "form 8-k",
        "form 10-k",
        "form 10-q",
    ]
    official_source_domains = [
        "sec.gov",
        "fda.gov",
        "energy.gov",
        "doe.gov",
        "defense.gov",
        "usa.gov",
        "gov.uk",
        "europa.eu",
        "investor.",
        "ir.",
        "investors.",
        "newsroom.",
        "press",
    ]
    market_keywords = [
        "stock market",
        "equities",
        "selloff",
        "plunge",
        "rout",
        "volatility",
        "vix",
        "rates",
        "rate hike",
        "rate cut",
        "central bank",
        "fed",
        "ecb",
        "boj",
        "pce",
        "cpi",
        "inflation",
        "recession",
        "risk-off",
        "credit spreads",
        "yield",
        "treasury",
        "curve",
        "oil",
        "gold",
        "commodities",
    ]

    companies = _load_companies(companies_path)
    cutoff = _now_utc() - timedelta(hours=hours)

    relevant_items = []
    seen_links = set()

    for company in companies:
        query = f"\"{company}\""
        url = _build_rss_url(query, lang=lang, geo=geo)
        feed = feedparser.parse(url)
        for entry in feed.entries:
            dt = _parse_dt(entry)
            if dt:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            summary = _truncate(_strip_html(summary_raw), 280)

            title_norm = _normalize(title)
            company_norm = _normalize(company)
            text_blob = f"{company} {title} {summary}"
            categories = _is_relevant(title, keywords_map)
            if broad_mode and not categories:
                categories = _is_relevant(summary, keywords_map)

            # Strict relevance rules:
            # 1) Company name must appear in title
            # 2) Keyword must appear in title (or summary in broad mode)
            # 3) Block noisy content
            if company_norm not in title_norm:
                continue
            if not categories:
                continue
            summary_norm = _normalize(summary)
            if any(n in title_norm for n in noise_words) or any(n in summary_norm for n in noise_words):
                continue
            if any(p in title_norm for p in forward_looking_phrases) or any(p in summary_norm for p in forward_looking_phrases):
                continue
            if not broad_mode:
                # Extra strict OR gate: require at least one of
                # (1) number present, (2) material verb present, (3) official source domain.
                has_number = bool(re.search(r"\\b\\d[\\d,\\.]*\\b", title_norm)) or bool(re.search(r"\\b\\d[\\d,\\.]*\\b", summary_norm))
                has_material_verb = any(v in title_norm for v in material_verbs) or any(v in summary_norm for v in material_verbs)
                link_l = (link or "").lower()
                has_official_source = any(d in link_l for d in official_source_domains)
                if not (has_number or has_material_verb or has_official_source):
                    continue

            if link in seen_links:
                continue
            seen_links.add(link)

            relevant_items.append({
                "company": company,
                "title": title,
                "link": link,
                "summary": summary,
                "categories": categories,
                "date": dt,
                "bucket": "company",
            })

    # Market movers feed (macro context)
    market_queries = [
        "stock market selloff",
        "equities plunge",
        "market rout",
        "risk-off",
        "vix spike",
        "fed decision",
        "central bank rate hike",
        "inflation data cpi",
        "recession fears",
        "credit spreads widening",
    ]
    for q in market_queries:
        url = _build_rss_url(q, lang=lang, geo=geo)
        feed = feedparser.parse(url)
        for entry in feed.entries:
            dt = _parse_dt(entry)
            if dt:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            summary = _truncate(_strip_html(summary_raw), 280)

            title_norm = _normalize(title)
            summary_norm = _normalize(summary)
            if any(n in title_norm for n in noise_words) or any(n in summary_norm for n in noise_words):
                continue
            if any(p in title_norm for p in forward_looking_phrases) or any(p in summary_norm for p in forward_looking_phrases):
                continue

            has_number = bool(re.search(r"\\b\\d[\\d,\\.]*\\b", title_norm)) or bool(re.search(r"\\b\\d[\\d,\\.]*\\b", summary_norm))
            has_material_verb = any(v in title_norm for v in material_verbs) or any(v in summary_norm for v in material_verbs)
            link_l = (link or "").lower()
            has_official_source = any(d in link_l for d in official_source_domains)
            has_market_keyword = any(k in title_norm for k in market_keywords) or any(k in summary_norm for k in market_keywords)

            if not (has_number or has_material_verb or has_official_source or has_market_keyword):
                continue

            if link in seen_links:
                continue
            seen_links.add(link)

            relevant_items.append({
                "company": "Market",
                "title": title,
                "link": link,
                "summary": summary,
                "categories": ["Macro"],
                "date": dt,
                "bucket": "market",
            })

    if not relevant_items:
        print("No relevant items found.")
        if run_now:
            _send_telegram_messages(token, chat_id, ["No relevant items found in the last 48h."])
        return

    # Sort newest first
    relevant_items.sort(key=lambda x: x["date"] or _now_utc(), reverse=True)

    # Limit number of items per company
    limited_items = []
    per_company_counts = {}
    for item in relevant_items:
        name = item["company"]
        per_company_counts[name] = per_company_counts.get(name, 0) + 1
        if per_company_counts[name] > max_per_company:
            continue
        limited_items.append(item)
    relevant_items = limited_items

    lines = []
    lines.append(f"<b>Daily Company News — last {hours}h</b>")
    lines.append("")

    for item in relevant_items:
        cat = ", ".join(item["categories"])
        title = html.escape(item["title"])
        link = html.escape(item["link"])
        summary = html.escape(item["summary"])
        company = html.escape(item["company"])
        lines.append(f"<b>{company}</b>")
        lines.append(f"- <a href=\"{link}\">{title}</a> ({cat})")
        if summary:
            lines.append(f"  {summary}")
        lines.append("")

    if use_llm:
        if not openai_api_key:
            raise RuntimeError("USE_LLM=1 but OPENAI_API_KEY is missing.")
        llm_output = _call_openai_summary(openai_api_key, openai_model, relevant_items)
        if not llm_output:
            llm_output = "No materially relevant items found in the last 48h."
        chunks = _chunk_message(llm_output, max_len=3600)
        _send_telegram_messages(token, chat_id, chunks, parse_mode=None)
        print(f"Sent LLM summary in {len(chunks)} message(s).")
    else:
        message = "\n".join(lines).strip()
        chunks = _chunk_message(message)
        _send_telegram_messages(token, chat_id, chunks)
        print(f"Sent {len(relevant_items)} items in {len(chunks)} message(s).")


if __name__ == "__main__":
    main()
