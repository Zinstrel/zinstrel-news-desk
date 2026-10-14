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

import feedparser
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Zinstrel News Desk", page_icon="📡", layout="wide")


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
    st.title("📡 Zinstrel News Desk")
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
ALL_BEATS = list(BEATS) + [TRADES_TAB]

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
- Links: link to primary sources (the organization's own announcement or page) rather than other news outlets where possible. Use markdown links. Only use URLs that appear in the source material or the SOURCE LINK.
- Don't add format labels such as "MAJOR SIGNAL" to the headline or body."""


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
    fields = WIRE_LIST_FIELDS + (["Article Text", "Editor's Note"] if status == "Queued" else [])
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
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                tag.decompose()
            root = soup.find("article") or soup
            paras = [p.get_text(" ", strip=True) for p in root.find_all("p")]
            text = "\n\n".join(p for p in paras if len(p) > 40)
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


def draft_article(rec_fields, fmt, note, source_text):
    """Returns dict: headline, body, flags, words, cut_off."""
    system = ("You are a senior writer at Zinstrel drafting a first version for the editor, "
              "Marcus Lawrence, to revise.\n\n" + HOUSE_RULES)
    user = f"""FORMAT: {fmt}
FORMAT GUIDANCE: {FORMATS[fmt]['guide']}
EDITOR'S NOTE: {note or '(none)'}

SOURCE HEADLINE: {rec_fields.get('Headline', '')}
SOURCE OUTLET: {rec_fields.get('Outlet') or 'unknown'}
SOURCE LINK: {rec_fields.get('URL', '')}

SOURCE MATERIAL:
{source_text[:30000]}

Return exactly this shape:
HEADLINE: <headline>
<blank line>
<body in markdown prose>
---
EDITOR FLAGS: <one short paragraph: anything unconfirmed, claims that need a primary-source link, quotes you could not verify. Write "None." if nothing.>"""
    raw, cut_off = claude(DRAFT_MODEL, system, user, 5000)
    text = raw.strip()
    headline = rec_fields.get("Headline", "")
    m = re.match(r"^\s*HEADLINE:\s*(.+?)\s*\n", text, flags=re.IGNORECASE)
    if m:
        headline = m.group(1).strip().strip('"*# ')
        text = text[m.end():].strip()
    body, _, flags = text.partition("\n---")
    flags = re.sub(r"^\s*EDITOR FLAGS:\s*", "", flags.strip(), flags=re.IGNORECASE)
    body = body.strip()
    return {"headline": headline, "body": body, "flags": flags or "None.",
            "words": word_count(body), "cut_off": cut_off}


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
    article = f"{draft['body']}\n\n---\nEditor flags: {draft['flags']}\n\nWord count: {draft['words']}"
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
def run_scan(beats, days_back, include_trades, max_new, progress):
    progress(0.02, "Searching Google News…")
    found = fetch_headlines(beats, days_back, include_trades)

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

    n = len(fresh)
    with ThreadPoolExecutor(max_workers=4) as pool:  # gentle on Google
        futs = {pool.submit(resolve_publisher_url, a["link"]): a for a in fresh}
        for k, fut in enumerate(as_completed(futs), 1):
            futs[fut]["url"] = fut.result()
            progress(0.10 + 0.30 * k / n, f"Finding original articles… {k}/{n}")

    fresh = [a for a in fresh if a["url"] not in seen]  # publisher URL already on the wire
    n = len(fresh)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(get_article_text, a["url"]): a for a in fresh}
        for k, fut in enumerate(as_completed(futs), 1):
            futs[fut]["text"] = fut.result()
            progress(0.40 + 0.30 * k / max(n, 1), f"Reading articles… {k}/{n}")

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
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
h1, h2, h3 { font-family: 'Space Grotesk', sans-serif !important; letter-spacing: -0.01em; }
.zn-card { border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 11px 14px;
           margin-bottom: 4px; background: rgba(255,255,255,0.02); }
.zn-card.sel { border-color: #FF3DA5; box-shadow: 0 0 0 1px #FF3DA5 inset; }
.zn-title a { color: inherit; text-decoration: none; font-weight: 600; font-size: 1.02rem; }
.zn-title a:hover { color: #3DE0FF; }
.zn-sum { font-size: 0.92rem; opacity: 0.85; margin-top: 4px; }
.zn-meta { font-size: 0.78rem; opacity: 0.6; margin-top: 5px; }
.zn-tag { display: inline-block; font-size: 0.7rem; padding: 1px 7px; border-radius: 99px;
          background: rgba(61,224,255,0.12); color: #3DE0FF; margin-left: 6px; }
.zn-warn { background: rgba(255,200,61,0.15); color: #FFC83D; }
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
st.title("📡 Zinstrel News Desk")

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
                       lambda p, t: bar.progress(min(p, 1.0), text=t))
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
                    status.info(f"Drafting **{f.get('Headline')}** as {fmt}…")
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
                               "Draft Link": link or None}
                    if pasted.strip():
                        updates["Article Text"] = pasted.strip()[:ARTICLE_TEXT_CAP]
                    at_update(WIRE_TABLE, [(rid, updates)])

                    line = f"**{draft['headline']}** → {where} · {fmt} · {draft['words']} words"
                    if link:
                        line += f" · [open draft]({link})"
                    if fell_back:
                        status.warning(f"{line}\n\nLovable didn't accept it ({fell_back}), so it went to Stories instead.")
                    elif draft["cut_off"]:
                        status.warning(f"{line}\n\n⚠️ The draft was cut off before the end. Finish it by hand or redraft.")
                    else:
                        status.success(line)
                    with st.expander(f"Preview: {draft['headline']}"):
                        st.markdown(f"### {draft['headline']}\n\n{draft['body']}")
                        st.caption(f"Editor flags: {draft['flags']}")
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
                    f"<span style='opacity:.6;font-size:.85rem'>{esc(f.get('Outlet'))} · {fmt_date(f.get('Published'))}</span>",
                    unsafe_allow_html=True)
