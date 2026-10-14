"""
Zinstrel News Desk
------------------
Scan  → pulls AI music news, finds each publisher link, reads the article,
        and writes a one-line summary. Results land in the Wire table (AirMarcus).
Wire  → browse by beat, queue what's worth a look, skip the rest.
Queue → pick Major Signal, Minor Signal, or News Brief for each story and draft.
Draft → Claude writes it in house style and sends it to the Lovable CMS as an
        unpublished draft. If Lovable isn't set up or fails, it goes to Stories.

Separate tool from the BenefitKarma news desk: own repo, own app, own secrets.
"""

import hmac
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
import streamlit as st
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
LOGO_PATH = HERE / "zinstrel-logo.png"
STAR_PATH = HERE / "zinstrel-star.png"


def _page_icon():
    try:
        from PIL import Image
        return Image.open(STAR_PATH)
    except Exception:
        return "✦"


st.set_page_config(page_title="Zinstrel News Desk", page_icon=_page_icon(), layout="wide")


def show_logo(width=300):
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=width)
    else:
        st.title("Zinstrel")


def get_secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


# ============================================================
# PASSWORD GATE (the app is publicly reachable on Community Cloud)
# ============================================================
APP_PASSWORD = get_secret("APP_PASSWORD")


def _password_ok():
    if st.session_state.get("authed"):
        return True
    col, _ = st.columns([1, 1.6])
    with col:
        show_logo(260)
        st.subheader("News Desk")
        if not APP_PASSWORD:
            st.error("APP_PASSWORD isn't set in this app's secrets, so the desk is locked.")
            return False
        pw = st.text_input("Password", type="password")
        if pw:
            if hmac.compare_digest(pw, APP_PASSWORD):
                st.session_state["authed"] = True
                st.rerun()
            else:
                st.error("Wrong password.")
    return False


if not _password_ok():
    st.stop()

# ============================================================
# CONFIG
# ============================================================
AIRTABLE_TOKEN = get_secret("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = get_secret("AIRTABLE_BASE_ID", "appOpnIsWZttCgxE6")   # AirMarcus
WIRE_TABLE = get_secret("WIRE_TABLE", "tblgZDgNDmuqUODdM")              # Wire
STORIES_TABLE = get_secret("STORIES_TABLE", "tblaG75vgdZabVJmj")        # Stories

ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY")
DRAFT_MODEL = get_secret("DRAFT_MODEL", "claude-sonnet-5-5")
SUMMARY_MODEL = get_secret("SUMMARY_MODEL", "claude-haiku-5-5")

LOVABLE_DRAFTS_URL = get_secret("LOVABLE_DRAFTS_URL")   # e.g. https://zinstrel.com/api/news-desk/drafts
LOVABLE_DRAFTS_KEY = get_secret("LOVABLE_DRAFTS_KEY")   # the NEWS_DESK_API_KEY value from Lovable
JINA_API_KEY = get_secret("JINA_API_KEY")               # optional; raises Jina's rate limit

KEEP_DAYS = 10            # New/Skipped wire items older than this are cleared on each scan
ARTICLE_TEXT_CAP = 15000  # characters of article text stored per wire item

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# Beats are only for sorting the wire. Each keyword is its own Google News search.
BEATS = {
    "Platforms & Models": [
        "Suno AI", "Udio AI music", "ElevenLabs music", "ElevenMusic", "Google Lyria",
        "AI music generator", "AI music model", "Stability AI audio", "Mureka AI",
        "Riffusion", "Google Flow Music",
    ],
    "Labels & Licensing": [
        "Universal Music AI", "Sony Music AI", "Warner Music AI",
        "AI music licensing deal", "music publishers AI", "AI music royalties",
        "Merlin AI licensing", "Kobalt AI",
    ],
    "Policy & Copyright": [
        "AI music copyright", "NO FAKES Act", "AI music lawsuit", "EU AI Act music",
        "voice cloning law", "Copyright Office AI music", "ELVIS Act", "AI training data music",
    ],
    "Streaming & Detection": [
        "Spotify AI music", "Deezer AI detection", "AI generated tracks streaming",
        "streaming fraud AI music", "AI music detection", "YouTube AI music",
        "AI music provenance", "Apple Music AI",
    ],
    "Funding & Business": [
        "AI music startup funding", "AI music acquisition", "music AI startup raises",
        "AI audio startup", "AI music valuation",
    ],
    "Culture & Artists": [
        "AI music artist", "AI generated song charts", "AI singer", "virtual artist AI",
        "AI music community", "AI band", "AI music video",
    ],
    "Tools & Workflows": [
        "AI mastering", "AI stem separation", "AI music production tool",
        "AI vocals plugin", "AI DAW", "LANDR AI", "BandLab AI",
    ],
    "Events": [
        "AI music festival", "AI Song Contest", "AI music conference", "AI music awards",
    ],
}
TRADES_TAB = "From the Trades"
TRADE_SITES = [
    "musicbusinessworldwide.com", "billboard.com", "digitalmusicnews.com",
    "completemusicupdate.com", "musically.com", "hypebot.com", "theverge.com",
    "musicweek.com", "variety.com",
]

# Newsletters read directly from their own sites (not through Google News).
NEWSLETTERS_TAB = "Newsletters"
NEWSLETTERS = [
    {"name": "The AI Musicpreneur", "site": "https://the-ai-musicpreneur.beehiiv.com"},
    {"name": "AI Music Newsletter", "site": "https://aimusicnewsletter.substack.com"},
]
NEWSLETTER_MIN_DAYS = 7   # weekly-ish newsletters: always look back at least this far

ALL_BEATS = list(BEATS) + [TRADES_TAB, NEWSLETTERS_TAB]

FORMATS = {
    "Major Signal": {
        "stories_type": "Signals (Mon)",
        "guide": (
            "A Major Signal: about 300 words. The headline doubles as the email subject line, "
            "so it must carry the argument in a short, declarative way. Lead with the news, give "
            "the context needed to understand it, and close with what's at stake for the AI music "
            "business, infrastructure, or policy landscape."
        ),
    },
    "Minor Signal": {
        "stories_type": "Signal (Website Briefs)",
        "guide": "A Minor Signal: a simple, plain header and 2-3 sentences of prose. Nothing more.",
    },
    "News Brief": {
        "stories_type": "Signal (Website Briefs)",
        "guide": (
            "A News Brief: straight news, at least 300 words. Go longer, up to about 600 words, "
            "only if the source material genuinely supports it. Lead with the news in the first "
            "sentence, then the confirmed facts, context, and what to watch next."
        ),
    },
}

HOUSE_RULES = """ZINSTREL HOUSE STYLE (follow all of these):
- Zinstrel is an independent editorial publication covering AI music: culture, business, infrastructure, and policy. Readers include decision-makers at labels, platforms, and AI music companies, plus journalists. Write for them.
- Write in prose. No bullet points or numbered lists in the article body.
- No bolded label fragments as section leads (never "The timing." or "Response." as a lead-in). Write straight through.
- Use the Oxford (serial) comma.
- Do not address creators directly as "you."
- Plain, reader-friendly, confident. Short declarative headlines work best; avoid long titles, multi-noun "And" headlines, front-loaded jargon, and brand-heavy openings.
- Quotes: use full direct quotes rather than paraphrase or partial quotes, but ONLY quotes that appear verbatim in the source material. Never invent, alter, or attribute a quote that is not in the source.
- Facts: use only facts supported by the source material. If something important is unknown, say it is unconfirmed rather than guessing. Never invent numbers, dates, names, or details.
- Links: link to primary sources (the organization's own announcement or page) rather than other news outlets. Use markdown links. Only use URLs that appear in the lead material or that you actually found through search or fetch.
- Don't add format labels such as "MAJOR SIGNAL" to the headline or body."""

ORIGINAL_REPORTING = """ORIGINAL REPORTING (this matters most):
- The lead item is a tip, not the story. When it is another outlet's or newsletter's reporting, do NOT rewrite it, and do NOT build the piece on "according to [outlet]" or "[outlet] reported."
- Before writing, use web search and web fetch to go to the primary material: the company's or person's own announcement, press release, blog post, filing, court document, bill text, official social post, dataset, product page, or original statement. Read it.
- Write the story from that primary material, link to it, and check every key fact (names, numbers, dates, terms) against it. If the primary material contradicts the lead, follow the primary material and flag the discrepancy.
- Quotes: use only quotes that appear in primary material you found, and link to where they appear. If a quote exists only in another outlet's interview, leave it out; if it's essential, attribute it explicitly ("told Billboard") and list it under follow-ups as a quote to replace with Zinstrel's own.
- Credit an outlet only when its own reporting is the sole source of a material fact (an exclusive). Say so plainly and flag it.
- If you cannot find primary material for a claim, either drop the claim or mark it as unconfirmed. Never present secondhand claims as established fact.
- Add Zinstrel's value: context from the wider AI music landscape that the lead didn't include, only if you can verify it."""


# ============================================================
# AIRTABLE
# ============================================================
def _at_url(table):
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}"


def _at_headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def _at_check(r):
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Airtable {r.status_code}: {r.text[:300]}")
    return r.json()


def at_list(table, formula=None, fields=None, sort_field=None, desc=True, max_records=None):
    out, offset = [], None
    while True:
        params = {"pageSize": 100}
        if formula:
            params["filterByFormula"] = formula
        if fields:
            params["fields[]"] = fields
        if sort_field:
            params["sort[0][field]"] = sort_field
            params["sort[0][direction]"] = "desc" if desc else "asc"
        if max_records:
            params["maxRecords"] = max_records
        if offset:
            params["offset"] = offset
        data = _at_check(requests.get(_at_url(table), headers=_at_headers(), params=params, timeout=20))
        out.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return out


def _clean(fields):
    return {k: v for k, v in fields.items() if v not in (None, "")}


def at_create(table, rows):
    ids = []
    for i in range(0, len(rows), 10):
        payload = {"records": [{"fields": _clean(f)} for f in rows[i:i + 10]], "typecast": True}
        data = _at_check(requests.post(_at_url(table), headers=_at_headers(), json=payload, timeout=20))
        ids.extend(r["id"] for r in data["records"])
        time.sleep(0.22)  # stay under Airtable's 5 requests/second
    return ids


def at_update(table, updates):
    """updates: list of (record_id, fields)."""
    for i in range(0, len(updates), 10):
        payload = {"records": [{"id": rid, "fields": f} for rid, f in updates[i:i + 10]],
                   "typecast": True}
        _at_check(requests.patch(_at_url(table), headers=_at_headers(), json=payload, timeout=20))
        time.sleep(0.22)


def at_delete(table, ids):
    for i in range(0, len(ids), 10):
        params = [("records[]", rid) for rid in ids[i:i + 10]]
        _at_check(requests.delete(_at_url(table), headers=_at_headers(), params=params, timeout=20))
        time.sleep(0.22)


def airtable_record_link(table, rec_id):
    return f"https://airtable.com/{AIRTABLE_BASE_ID}/{table}/{rec_id}"


WIRE_LIST_FIELDS = ["Headline", "Summary", "Status", "Beat", "Outlet", "URL",
                    "Published", "On Topic", "Readable", "Keyword", "Format", "Draft Link"]


@st.cache_data(ttl=120, show_spinner=False)
def load_wire(status):
    extra = {"Queued": ["Article Text", "Editor's Note"], "Drafted": ["Draft Notes"]}.get(status, [])
    fields = WIRE_LIST_FIELDS + extra
    if status == "New":
        formula = "OR({Status}='New',{Status}='')"
    else:
        formula = f"{{Status}}='{status}'"
    return at_list(WIRE_TABLE, formula=formula, fields=fields, sort_field="Published",
                   max_records=60 if status == "Drafted" else None)


def refresh_wire():
    load_wire.clear()


# ============================================================
# FETCHING
# ============================================================
def _parse_date(entry):
    for key in ("published", "updated"):
        if entry.get(key):
            try:
                return parsedate_to_datetime(entry[key])
            except Exception:
                pass
    return None


def _clean_title(title, outlet):
    if outlet and title.endswith(f" - {outlet}"):
        return title[: -len(f" - {outlet}")].strip()
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", title).strip()


def _norm(title):
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower())[:80]


def _fetch_query(query, days_back, keyword_tag):
    url = ("https://news.google.com/rss/search?q="
           f"{quote_plus(query)}+when:{days_back}d&hl=en-US&gl=US&ceid=US:en")
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=12)
        feed = feedparser.parse(r.content)
    except Exception:
        return []
    out = []
    for e in feed.entries:
        outlet = e.source.get("title", "") if e.get("source") and hasattr(e.source, "get") else ""
        out.append({
            "title": _clean_title(e.get("title", "").strip(), outlet),
            "link": e.get("link", ""),
            "outlet": outlet,
            "published": _parse_date(e),
            "keyword": keyword_tag,
        })
    return out


def fetch_headlines(beats, days_back, include_trades):
    """Returns a flat list of articles, each tagged with the first beat it matched."""
    jobs = [(b, f'"{kw}"' if " " in kw else kw, kw) for b in beats for kw in BEATS[b]]
    if include_trades:
        jobs += [(TRADES_TAB, f"AI music site:{s}", s) for s in TRADE_SITES]
    by_beat = {b: [] for b in list(beats) + ([TRADES_TAB] if include_trades else [])}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_fetch_query, q, days_back, tag): beat for beat, q, tag in jobs}
        for fut in as_completed(futures):
            by_beat[futures[fut]].extend(fut.result())

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back + 1)
    seen_links, seen_titles, out = set(), set(), []
    for beat, items in by_beat.items():  # earlier beats claim shared stories
        for a in items:
            k = _norm(a["title"])
            if not a["title"] or a["link"] in seen_links or k in seen_titles:
                continue
            if a["published"] and a["published"] < cutoff:
                continue
            seen_links.add(a["link"])
            seen_titles.add(k)
            out.append({**a, "beat": beat})
    return out


def _html_to_text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    root = soup.find("article") or soup
    paras = [p.get_text(" ", strip=True) for p in root.find_all(["p", "li", "h2", "h3"])]
    return "\n\n".join(p for p in paras if len(p) > 30)


def _meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _parse_iso(s):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fetch_post_page(url, outlet):
    """Reads one newsletter post page: title, date, full text."""
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15)
        if r.status_code != 200:
            return None
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    title = _meta(soup, "og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    published = _parse_iso(_meta(soup, "article:published_time", "og:article:published_time"))
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        published = _parse_iso(t["datetime"]) if t else None
    return {"title": title.strip(), "link": url, "url": url, "outlet": outlet, "published": published,
            "keyword": outlet, "text": _html_to_text(r.text) or None, "beat": NEWSLETTERS_TAB}


def fetch_newsletter(src, days_back):
    """RSS first (Substack has one at /feed); otherwise read the archive page (beehiiv)."""
    days = max(days_back, NEWSLETTER_MIN_DAYS)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    name, site = src["name"], src["site"].rstrip("/")
    posts = []
    try:
        r = requests.get(f"{site}/feed", headers={"User-Agent": BROWSER_UA}, timeout=12)
        feed = feedparser.parse(r.content) if r.status_code == 200 else None
    except Exception:
        feed = None
    if feed and feed.entries:
        for e in feed.entries[:15]:
            html = (e.get("content") or [{}])[0].get("value") or e.get("summary", "")
            posts.append({"title": e.get("title", "").strip(), "link": e.get("link", ""),
                          "url": e.get("link", ""), "outlet": name, "published": _parse_date(e),
                          "keyword": name, "text": _html_to_text(html) or None, "beat": NEWSLETTERS_TAB})
    else:
        try:
            r = requests.get(f"{site}/archive?page=1", headers={"User-Agent": BROWSER_UA}, timeout=12)
            soup = BeautifulSoup(r.text, "html.parser") if r.status_code == 200 else None
        except Exception:
            soup = None
        links = []
        for a in (soup.find_all("a", href=True) if soup else []):
            href = urljoin(site + "/", a["href"]).split("?")[0].split("#")[0]
            if "/p/" in href and href.startswith(site) and href not in links:
                links.append(href)
        with ThreadPoolExecutor(max_workers=4) as pool:
            posts = [p for p in pool.map(lambda u: _fetch_post_page(u, name), links[:8]) if p]
    return [p for p in posts if p["title"] and (not p["published"] or p["published"] >= cutoff)]


@st.cache_data(ttl=86400, show_spinner=False)
def _decode_google_news(url):
    # Raises on failure so only successful lookups get cached.
    from googlenewsdecoder import gnewsdecoder
    result = gnewsdecoder(url, timeout=15)
    if result.get("success") and result.get("decoded_url"):
        return result["decoded_url"]
    raise ValueError(result.get("message", "decode failed"))


def resolve_publisher_url(url):
    """Google News links don't redirect to the publisher anymore, so decode them."""
    if "news.google.com" not in url:
        return url
    try:
        return _decode_google_news(url)
    except Exception:
        return url


def get_article_text(url):
    """Plain scrape first (fast, no rate limit), then Jina's reader for JS-heavy pages."""
    if not url or "news.google.com" in url:
        return None
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            text = _html_to_text(r.text)
            if len(text) > 600:
                return text
    except Exception:
        pass
    try:
        headers = {"Authorization": f"Bearer {JINA_API_KEY}"} if JINA_API_KEY else {}
        r = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=25)
        if r.status_code == 200 and len(r.text) > 600:
            return r.text
    except Exception:
        pass
    return None


# ============================================================
# CLAUDE
# ============================================================
def claude(model, system, user, max_tokens):
    """Returns (text, was_cut_off)."""
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text, getattr(resp, "stop_reason", "") == "max_tokens"


SUMMARY_SYSTEM = (
    "You write one-line summaries for the news wire of Zinstrel, a publication covering AI music. "
    "For each item, write ONE plain sentence (under 25 words) saying what actually happened. "
    "Use only what's in the item; if only a headline is available, summarize the headline without "
    "adding facts. Also judge on_topic: true only if the story is substantively about AI and music, "
    "audio, musicians, or the music industry."
)


def summarize_batch(items):
    """items: list of dicts with title, outlet, text. Returns list of (summary, on_topic)."""
    blocks = []
    for i, it in enumerate(items):
        excerpt = (it.get("text") or "")[:1500] or "(article text unavailable; headline only)"
        blocks.append(f"[{i}] HEADLINE: {it['title']}\nOUTLET: {it.get('outlet') or 'unknown'}\nTEXT: {excerpt}")
    user = ("\n\n".join(blocks) + "\n\nReturn ONLY a JSON array, one object per item, like "
            '[{"i": 0, "summary": "...", "on_topic": true}]')
    try:
        raw, _ = claude(SUMMARY_MODEL, SUMMARY_SYSTEM, user, 200 * len(items) + 400)
        data = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
        got = {int(d["i"]): (str(d.get("summary", "")).strip(), bool(d.get("on_topic", True)))
               for d in data if "i" in d}
    except Exception:
        got = {}
    return [got.get(i, ("", True)) for i in range(len(items))]


def word_count(text):
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return len(re.findall(r"\b[\w'’-]+\b", body))


def research_tools(fmt):
    searches = 3 if fmt == "Minor Signal" else 5
    return [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": searches},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 4, "max_content_tokens": 12000},
    ]


def claude_with_research(system, user, tools, max_tokens=8000):
    """Runs a turn that may search and fetch. Returns (all_text, was_cut_off, searches_used).
    Falls back to no tools if web search/fetch isn't enabled on the API account."""
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = [{"role": "user", "content": user}]
    texts, searches = [], 0
    for _ in range(6):  # the API can pause long research turns; resume them
        try:
            resp = client.messages.create(model=DRAFT_MODEL, max_tokens=max_tokens, system=system,
                                          messages=messages, **({"tools": tools} if tools else {}))
        except Exception as e:
            if tools and any(w in str(e).lower() for w in ("web_search", "web search", "web_fetch", "tool")):
                return None  # caller retries without research
            raise
        texts.extend(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = getattr(getattr(resp, "usage", None), "server_tool_use", None)
        searches += getattr(usage, "web_search_requests", 0) or 0
        if getattr(resp, "stop_reason", "") == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        return "".join(texts), getattr(resp, "stop_reason", "") == "max_tokens", searches
    return "".join(texts), True, searches


def _section(text, label, stop_labels):
    m = re.search(rf"{label}:\s*(.*?)(?=(?:{'|'.join(stop_labels)}):|\Z)", text, flags=re.S | re.I)
    return m.group(1).strip() if m else ""


def draft_article(rec_fields, fmt, note, source_text):
    """Researches primary sources, then writes. Returns dict with headline, body, notes, words,
    cut_off, researched."""
    system = ("You are a senior reporter at Zinstrel writing a first draft for the editor, Marcus "
              "Lawrence, to revise.\n\n" + HOUSE_RULES + "\n\n" + ORIGINAL_REPORTING)
    user = f"""FORMAT: {fmt}
FORMAT GUIDANCE: {FORMATS[fmt]['guide']}
EDITOR'S NOTE: {note or '(none)'}

LEAD ITEM (a tip to report out, not a source to rewrite)
HEADLINE: {rec_fields.get('Headline', '')}
OUTLET: {rec_fields.get('Outlet') or 'unknown'}
LINK: {rec_fields.get('URL', '')}

LEAD TEXT:
{source_text[:25000]}

First, research: find and read the primary material behind this lead. Then write.
When you're done researching, output the finished piece in exactly this shape and nothing after it:

HEADLINE: <headline>

<body in markdown prose>
---
EDITOR FLAGS: <anything unconfirmed, discrepancies between the lead and primary sources, quotes you could not verify. "None." if nothing.>
FOLLOW-UP REPORTING: <1-3 people or organizations Zinstrel should contact for its own comment, and the question to ask each.>
PRIMARY SOURCES: <the primary-source URLs you used, one per line. "None found." if none.>"""
    result = claude_with_research(system, user, research_tools(fmt))
    researched = result is not None
    if not researched:
        user = user.replace("First, research: find and read the primary material behind this lead. Then write.",
                            "Web research is unavailable, so write from the lead text, keep claims attributed, "
                            "and list what needs primary-source checking under EDITOR FLAGS.")
        result = claude_with_research(system, user, None)
    raw, cut_off, searches = result

    # The finished piece starts at the last HEADLINE: (earlier text is research narration).
    starts = list(re.finditer(r"^[ \t#*]*HEADLINE:", raw, flags=re.M))
    text = raw[starts[-1].start():] if starts else raw
    headline = rec_fields.get("Headline", "")
    m = re.match(r"^\s*HEADLINE:\s*(.+?)\s*\n", text, flags=re.IGNORECASE)
    if m:
        headline = m.group(1).strip().strip('"*# ')
        text = text[m.end():].strip()
    body, _, tail = text.partition("\n---")
    stops = ["EDITOR FLAGS", "FOLLOW-UP REPORTING", "PRIMARY SOURCES"]
    flags = _section(tail, "EDITOR FLAGS", stops) or "None."
    follow = _section(tail, "FOLLOW-UP REPORTING", stops)
    sources = _section(tail, "PRIMARY SOURCES", stops)
    if not researched:
        flags = "⚠️ Web research was unavailable, so this was written from the lead only. " + flags
    notes = f"Editor flags: {flags}"
    if follow:
        notes += f"\n\nFollow-up reporting: {follow}"
    if sources:
        notes += f"\n\nPrimary sources:\n{sources}"
    body = body.strip()
    return {"headline": headline, "body": body, "notes": notes, "words": word_count(body),
            "cut_off": cut_off, "researched": researched, "searches": searches}


# ============================================================
# DESTINATIONS
# ============================================================
def send_to_lovable(draft, fmt, rec_fields):
    payload = {
        "title": draft["headline"],
        "body_markdown": draft["body"],
        "format": fmt,
        "summary": rec_fields.get("Summary", ""),
        "source_url": rec_fields.get("URL", ""),
        "source_outlet": rec_fields.get("Outlet", ""),
        "beat": rec_fields.get("Beat", ""),
    }
    r = requests.post(LOVABLE_DRAFTS_URL, json=payload, timeout=30,
                      headers={"Authorization": f"Bearer {LOVABLE_DRAFTS_KEY}"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Lovable {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except Exception:
        data = {}
    return data.get("edit_url") or data.get("url") or ""


def send_to_stories(draft, fmt, rec_fields, note):
    article = f"{draft['body']}\n\n---\n{draft['notes']}\n\nWord count: {draft['words']}"
    notes = "\n".join(x for x in [
        f"Editor's note: {note}" if note else "",
        f"Drafted by news desk as {fmt} · {draft['words']} words",
        "⚠️ Draft was cut off before the end; regenerate or finish by hand." if draft["cut_off"] else "",
    ] if x)
    rec_id = at_create(STORIES_TABLE, [{
        "Title/Topic": draft["headline"],
        "Type": FORMATS[fmt]["stories_type"],
        "Status": "Concept Stage",
        "Beat": rec_fields.get("Beat") if rec_fields.get("Beat") in BEATS else None,
        "Source URL": rec_fields.get("URL"),
        "Source Outlet": rec_fields.get("Outlet"),
        "Article Draft": article,
        "Notes": notes,
    }])[0]
    return airtable_record_link(STORIES_TABLE, rec_id)


# ============================================================
# SCAN
# ============================================================
def run_scan(beats, days_back, include_trades, max_new, progress, include_newsletters=True):
    progress(0.02, "Searching Google News and newsletters…")
    found = fetch_headlines(beats, days_back, include_trades) if (beats or include_trades) else []
    if include_newsletters:
        with ThreadPoolExecutor(max_workers=len(NEWSLETTERS)) as pool:
            for posts in pool.map(lambda s: fetch_newsletter(s, days_back), NEWSLETTERS):
                found.extend(posts)

    progress(0.10, "Checking what's already on the wire…")
    existing = at_list(WIRE_TABLE, fields=["Google Link", "Headline", "URL"])
    seen = set()
    for rec in existing:
        f = rec.get("fields", {})
        seen.update(x for x in (f.get("Google Link"), f.get("URL")) if x)
        seen.add("t:" + _norm(f.get("Headline")))
    fresh = [a for a in found if a["link"] not in seen and "t:" + _norm(a["title"]) not in seen]
    fresh.sort(key=lambda a: a["published"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    skipped_for_cap = max(0, len(fresh) - max_new)
    fresh = fresh[:max_new]
    if not fresh:
        removed = cleanup_wire()
        return {"found": len(found), "added": 0, "capped": 0, "removed": removed}

    to_decode = [a for a in fresh if not a.get("url")]  # newsletters already have their own link
    n = len(to_decode)
    with ThreadPoolExecutor(max_workers=4) as pool:  # gentle on Google
        futs = {pool.submit(resolve_publisher_url, a["link"]): a for a in to_decode}
        for k, fut in enumerate(as_completed(futs), 1):
            futs[fut]["url"] = fut.result()
            progress(0.10 + 0.30 * k / n, f"Finding original articles… {k}/{n}")

    fresh = [a for a in fresh if a["url"] not in seen]  # publisher URL already on the wire
    n = len(fresh)
    to_read = [a for a in fresh if not a.get("text")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(get_article_text, a["url"]): a for a in to_read}
        for k, fut in enumerate(as_completed(futs), 1):
            futs[fut]["text"] = fut.result()
            progress(0.40 + 0.30 * k / max(len(to_read), 1), f"Reading articles… {k}/{len(to_read)}")

    batches = [fresh[i:i + 8] for i in range(0, n, 8)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(summarize_batch, b): b for b in batches}
        for k, fut in enumerate(as_completed(futs), 1):
            for a, (summary, on_topic) in zip(futs[fut], fut.result()):
                a["summary"], a["on_topic"] = summary, on_topic
            progress(0.70 + 0.20 * k / max(len(batches), 1), f"Writing one-line summaries… {k}/{len(batches)}")

    progress(0.92, "Saving to the Wire…")
    at_create(WIRE_TABLE, [{
        "Headline": a["title"],
        "Summary": a.get("summary"),
        "Status": "New",
        "Beat": a["beat"],
        "Outlet": a["outlet"],
        "URL": a["url"],
        "Google Link": a["link"],
        "Published": a["published"].isoformat() if a["published"] else None,
        "On Topic": bool(a.get("on_topic", True)),
        "Readable": bool(a.get("text")),
        "Article Text": (a.get("text") or "")[:ARTICLE_TEXT_CAP],
        "Keyword": a["keyword"],
    } for a in fresh])
    removed = cleanup_wire()
    progress(1.0, "Done")
    return {"found": len(found), "added": n, "capped": skipped_for_cap, "removed": removed}


def cleanup_wire():
    formula = (f"AND(OR({{Status}}='New',{{Status}}='Skipped',{{Status}}=''),"
               f"IS_BEFORE(CREATED_TIME(),DATEADD(NOW(),-{KEEP_DAYS},'days')))")
    try:
        old = at_list(WIRE_TABLE, formula=formula, fields=["Headline"])
        at_delete(WIRE_TABLE, [r["id"] for r in old])
        return len(old)
    except Exception:
        return 0


# ============================================================
# ACTIONS (button callbacks run before the page redraws)
# ============================================================
def _flash(kind, msg):
    st.session_state.setdefault("flash", []).append((kind, msg))


def set_status_for_selected(prefix, status):
    ids = [k[len(prefix):] for k, v in st.session_state.items() if k.startswith(prefix) and v]
    if not ids:
        return
    try:
        at_update(WIRE_TABLE, [(rid, {"Status": status}) for rid in ids])
        for rid in ids:
            st.session_state.pop(prefix + rid, None)
        verb = {"Queued": "Queued", "Skipped": "Skipped", "New": "Moved back to the wire"}[status]
        _flash("success", f"{verb}: {len(ids)} {'story' if len(ids) == 1 else 'stories'}.")
    except Exception as e:
        _flash("error", f"Couldn't update Airtable: {e}")
    refresh_wire()


def set_status_one(rec_id, status):
    try:
        at_update(WIRE_TABLE, [(rec_id, {"Status": status})])
    except Exception as e:
        _flash("error", f"Couldn't update Airtable: {e}")
    refresh_wire()


# ============================================================
# STYLE
# ============================================================
# Light theme so the logo's full gradient reads (the deep purple/navy middle disappears on dark).
# Colors are pulled from the logo: magenta → plum → navy → sky blue.
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
h1, h2, h3 { font-family: 'Space Grotesk', sans-serif !important; letter-spacing: -0.01em; color: #1E1747; }
.zn-bar { height: 4px; border-radius: 4px; margin: 2px 0 14px;
          background: linear-gradient(90deg, #FF00E6, #8E0F8F 40%, #1E2A5E 65%, #0A9BFF); }
.zn-card { border: 1px solid #E6E1F2; border-radius: 12px; padding: 12px 15px; margin-bottom: 4px;
           background: #FFFFFF; box-shadow: 0 1px 2px rgba(30,23,71,0.04); }
.zn-card.sel { border-color: #E000CC; box-shadow: 0 0 0 1px #E000CC inset; }
.zn-title a { color: #1E1747; text-decoration: none; font-weight: 600; font-size: 1.02rem; }
.zn-title a:hover { color: #0A7FD6; }
.zn-sum { font-size: 0.93rem; color: #3D3666; margin-top: 4px; }
.zn-meta { font-size: 0.78rem; color: #7A7499; margin-top: 6px; }
.zn-tag { display: inline-block; font-size: 0.7rem; padding: 1px 8px; border-radius: 99px;
          background: #E8F4FF; color: #0566B8; margin-left: 6px; }
.zn-warn { background: #FFF3DC; color: #8A5300; }
</style>
""", unsafe_allow_html=True)


def fmt_date(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%b %d, %-I:%M %p")
    except Exception:
        return iso[:10]


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card(f, selected=False, extra_tags=""):
    tags = extra_tags
    if not f.get("Readable"):
        tags += '<span class="zn-tag zn-warn">headline only</span>'
    summary = f.get("Summary") or "<i>No summary</i>"
    st.markdown(
        f'<div class="zn-card {"sel" if selected else ""}">'
        f'<div class="zn-title"><a href="{esc(f.get("URL"))}" target="_blank">{esc(f.get("Headline"))}</a></div>'
        f'<div class="zn-sum">{esc(summary) if f.get("Summary") else summary}</div>'
        f'<div class="zn-meta">{esc(f.get("Outlet") or "Unknown outlet")} · {fmt_date(f.get("Published"))}'
        f'<span class="zn-tag">{esc(f.get("Beat") or "")}</span>{tags}</div></div>',
        unsafe_allow_html=True,
    )


# ============================================================
# PAGE
# ============================================================
try:
    st.logo(str(LOGO_PATH), icon_image=str(STAR_PATH), size="large")
except Exception:
    pass
h1, h2 = st.columns([1, 2.4])
with h1:
    show_logo(240)
with h2:
    st.markdown("<div style='padding-top:14px;font-family:Space Grotesk,sans-serif;font-size:1.5rem;"
                "font-weight:700;color:#1E1747'>News Desk</div>", unsafe_allow_html=True)
st.markdown('<div class="zn-bar"></div>', unsafe_allow_html=True)

missing = [k for k, v in {"AIRTABLE_TOKEN": AIRTABLE_TOKEN, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY}.items() if not v]
if missing:
    st.error(f"Missing secrets: {', '.join(missing)}. Add them under Manage app → Settings → Secrets.")
    st.stop()

for kind, msg in st.session_state.pop("flash", []):
    getattr(st, kind)(msg)

lovable_on = bool(LOVABLE_DRAFTS_URL and LOVABLE_DRAFTS_KEY)

with st.sidebar:
    st.header("Scan")
    days_back = st.slider("Days back", 1, 7, 2)
    max_new = st.slider("Max new stories per scan", 20, 150, 60, step=10)
    chosen_beats = st.multiselect("Beats to search", list(BEATS), default=list(BEATS))
    include_trades = st.checkbox("Trade-press sweep", value=True)
    include_newsletters = st.checkbox("Newsletters", value=True,
                                      help=", ".join(n["name"] for n in NEWSLETTERS))
    scan_clicked = st.button("🔎 Scan now", type="primary", use_container_width=True)
    st.divider()
    st.caption(f"Drafts go to: **{'Lovable CMS' if lovable_on else 'Stories (Airtable)'}**")
    st.caption(f"Drafting: {DRAFT_MODEL} · Summaries: {SUMMARY_MODEL}")
    if st.button("↻ Reload from Airtable", use_container_width=True):
        refresh_wire()

if scan_clicked:
    bar = st.progress(0.0, text="Starting scan…")
    try:
        res = run_scan(tuple(chosen_beats), days_back, include_trades, max_new,
                       lambda p, t: bar.progress(min(p, 1.0), text=t), include_newsletters)
        msg = f"Scan done: {res['added']} new on the wire ({res['found']} headlines checked)."
        if res["capped"]:
            msg += f" {res['capped']} more were left for the next scan (raise the max to pull more)."
        if res["removed"]:
            msg += f" Cleared {res['removed']} unused items older than {KEEP_DAYS} days."
        st.success(msg)
    except Exception as e:
        st.error(f"Scan failed: {e}")
    refresh_wire()

try:
    wire = load_wire("New")
    queue = load_wire("Queued")
except Exception as e:
    st.error(f"Couldn't load the Wire from Airtable: {e}")
    st.stop()

tab_wire, tab_queue, tab_done = st.tabs([f"Wire ({len(wire)})", f"Queue ({len(queue)})", "Drafted"])

# ---------------- WIRE ----------------
with tab_wire:
    if not wire:
        st.info("The wire is empty. Click **Scan now** in the sidebar.")
    else:
        show_off = st.toggle("Show off-topic stories", value=False)
        visible = [r for r in wire if show_off or r["fields"].get("On Topic")]
        hidden = len(wire) - len(visible)
        n_sel = sum(1 for k, v in st.session_state.items() if k.startswith("w_") and v)
        c1, c2, c3 = st.columns([1, 1, 3])
        c1.button(f"➕ Queue selected ({n_sel})", on_click=set_status_for_selected,
                  args=("w_", "Queued"), disabled=not n_sel, use_container_width=True, type="primary")
        c2.button(f"✕ Skip selected ({n_sel})", on_click=set_status_for_selected,
                  args=("w_", "Skipped"), disabled=not n_sel, use_container_width=True)
        if hidden and not show_off:
            c3.caption(f"{hidden} off-topic {'story' if hidden == 1 else 'stories'} hidden.")

        beats_present = [b for b in ALL_BEATS if any(r["fields"].get("Beat") == b for r in visible)]
        if beats_present:
            btabs = st.tabs([f"{b} ({sum(1 for r in visible if r['fields'].get('Beat') == b)})"
                             for b in beats_present])
            for bt, beat in zip(btabs, beats_present):
                with bt:
                    for r in visible:
                        f = r["fields"]
                        if f.get("Beat") != beat:
                            continue
                        key = f"w_{r['id']}"
                        a, b = st.columns([0.04, 0.96])
                        with a:
                            st.checkbox(" ", key=key, label_visibility="collapsed")
                        with b:
                            off = "" if f.get("On Topic") else '<span class="zn-tag zn-warn">off-topic?</span>'
                            card(f, st.session_state.get(key, False), off)

# ---------------- QUEUE ----------------
with tab_queue:
    if not queue:
        st.info("Nothing queued. Select stories on the Wire and click **Queue selected**.")
    else:
        st.caption("Pick a format for each story you want written, then draft. Stories without a format stay in the queue.")
        for r in queue:
            f, rid = r["fields"], r["id"]
            card(f)
            c1, c2, c3 = st.columns([2.2, 3, 0.8])
            saved_fmt = f.get("Format")
            c1.radio("Format", list(FORMATS), key=f"qf_{rid}", horizontal=True,
                     index=list(FORMATS).index(saved_fmt) if saved_fmt in FORMATS else None,
                     label_visibility="collapsed")
            c2.text_input("Editor's note", key=f"qn_{rid}", value=f.get("Editor's Note", ""),
                          placeholder="Editor's note (optional angle)", label_visibility="collapsed")
            c3.button("Remove", key=f"qr_{rid}", on_click=set_status_one, args=(rid, "Skipped"),
                      use_container_width=True)
            if not f.get("Readable") and not f.get("Article Text"):
                st.text_area("Couldn't read this article. Paste its text to draft it:",
                             key=f"qp_{rid}", height=90)
            st.write("")

        ready = [r for r in queue if st.session_state.get(f"qf_{r['id']}")]
        go = st.button(f"✍️ Draft {len(ready)} {'story' if len(ready) == 1 else 'stories'}",
                       type="primary", disabled=not ready)
        if go:
            st.divider()
            bar = st.progress(0.0)
            for i, r in enumerate(ready, 1):
                f, rid = r["fields"], r["id"]
                fmt = st.session_state[f"qf_{rid}"]
                note = st.session_state.get(f"qn_{rid}", "")
                pasted = st.session_state.get(f"qp_{rid}", "")
                status = st.empty()
                try:
                    source = pasted.strip() or f.get("Article Text") or ""
                    if not source:
                        status.info(f"Reading **{f.get('Headline')}**…")
                        source = get_article_text(f.get("URL", "")) or ""
                    if not source:
                        status.warning(f"**{f.get('Headline')}**: couldn't read the article and nothing was "
                                       "pasted, so it stays in the queue. Paste the text and try again.")
                        continue
                    status.info(f"Reporting out **{f.get('Headline')}** (finding primary sources, then "
                                f"writing a {fmt})… this can take a minute.")
                    draft = draft_article(f, fmt, note, source)

                    where, link, fell_back = "Lovable", "", None
                    if lovable_on:
                        try:
                            link = send_to_lovable(draft, fmt, f)
                        except Exception as e:
                            fell_back = str(e)
                    if not lovable_on or fell_back:
                        where, link = "Stories", send_to_stories(draft, fmt, f, note)

                    updates = {"Status": "Drafted", "Format": fmt, "Editor's Note": note,
                               "Draft Link": link or None, "Draft Notes": draft["notes"]}
                    if pasted.strip():
                        updates["Article Text"] = pasted.strip()[:ARTICLE_TEXT_CAP]
                    at_update(WIRE_TABLE, [(rid, updates)])

                    line = f"**{draft['headline']}** → {where} · {fmt} · {draft['words']} words"
                    if draft["researched"]:
                        line += f" · {draft['searches']} searches"
                    if link:
                        line += f" · [open draft]({link})"
                    if fell_back:
                        status.warning(f"{line}\n\nLovable didn't accept it ({fell_back}), so it went to Stories instead.")
                    elif draft["cut_off"]:
                        status.warning(f"{line}\n\n⚠️ The draft was cut off before the end. Finish it by hand or redraft.")
                    elif not draft["researched"]:
                        status.warning(f"{line}\n\n⚠️ Web research wasn't available, so this was written from "
                                       "the lead only. Check primary sources before publishing.")
                    else:
                        status.success(line)
                    with st.expander(f"Preview and reporting notes: {draft['headline']}"):
                        st.markdown(f"### {draft['headline']}\n\n{draft['body']}")
                        st.divider()
                        st.markdown(draft["notes"].replace("\n", "  \n"))
                except Exception as e:
                    status.error(f"Couldn't draft **{f.get('Headline')}**: {e}")
                bar.progress(i / len(ready))
            refresh_wire()
            st.button("Done — refresh the queue")

# ---------------- DRAFTED ----------------
with tab_done:
    try:
        done = load_wire("Drafted")
    except Exception as e:
        done = []
        st.error(f"Couldn't load drafted stories: {e}")
    if not done:
        st.info("Drafted stories will show up here.")
    for r in done:
        f = r["fields"]
        link = f" · [open draft]({f['Draft Link']})" if f.get("Draft Link") else ""
        st.markdown(f"**{f.get('Headline')}** · {f.get('Format', '')}{link}  \n"
                    f"<span style='opacity:.6;font-size:.85rem'>Lead: {esc(f.get('Outlet'))} · {fmt_date(f.get('Published'))}</span>",
                    unsafe_allow_html=True)
        if f.get("Draft Notes"):
            with st.expander("Reporting notes"):
                st.markdown(f["Draft Notes"].replace("\n", "  \n"))
