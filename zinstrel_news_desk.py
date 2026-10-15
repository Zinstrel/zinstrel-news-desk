"""
Zinstrel News Desk
------------------
Scan  → pulls AI music news (Google News, trade press, newsletters), finds each
        publisher link, reads the article, and keeps ONLY timely AI-music news,
        each with a one-line summary. Results land in the Wire table (AirMarcus).
Ideas → suggests stories in Zinstrel's sphere from what's been published, what's
        on the Wire, Google's trending searches, and (optionally) Google Analytics
        and Search Console.
Wire  → browse by beat, queue what's worth a look, skip the rest.
Queue → pick Major Signal, Minor Signal, or News Brief, add up to 5 links, draft.
Draft → Claude reports it out from primary sources, writes in house style, and
        sends it to the Lovable CMS as an unpublished draft (or to Stories).

Separate tool from the BenefitKarma news desk: own repo, own app, own secrets.
"""

import hmac
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

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

# Optional: Google Analytics 4 + Search Console, read with a service account.
GA4_PROPERTY_ID = str(get_secret("GA4_PROPERTY_ID", ""))      # e.g. "123456789"
GSC_SITE_URL = get_secret("GSC_SITE_URL", "")                 # e.g. "sc-domain:zinstrel.com"

PUBLISHED_FEED = get_secret("PUBLISHED_FEED", "https://zinstrel.com/api/public/social-feed")

KEEP_DAYS = 10            # New/Skipped wire items older than this are cleared on each scan
ARTICLE_TEXT_CAP = 15000  # characters of article text stored per wire item
MAX_EDITOR_LINKS = 5

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# Beats are only for sorting the wire. Each keyword is its own Google News search.
BEATS = {
    "Platforms & Models": [
        "Suno AI", "Udio AI music", "ElevenLabs music", "ElevenMusic", "Google Lyria",
        "AI music model", "Stability AI audio", "Mureka AI", "Riffusion", "Google Flow Music",
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
        "AI mastering launch", "AI stem separation", "AI music app update",
        "AI vocals plugin", "LANDR AI", "BandLab AI",
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
TRADE_PHRASES = ['"AI music"', '"AI-generated"', '"generative AI"']

# Newsletters read directly from their own sites (not through Google News).
NEWSLETTERS_TAB = "Newsletters"
NEWSLETTERS = [
    {"name": "The AI Musicpreneur", "site": "https://the-ai-musicpreneur.beehiiv.com"},
    {"name": "AI Music Newsletter", "site": "https://aimusicnewsletter.substack.com"},
]
NEWSLETTER_MIN_DAYS = 7   # weekly-ish newsletters: always look back at least this far

IDEAS_TAB = "Story Ideas"
WIRE_BEATS = list(BEATS) + [TRADES_TAB, NEWSLETTERS_TAB]

# Pages that are never news: job boards, company/about/product pages, tag indexes.
JUNK_URL = re.compile(
    r"/(jobs?|careers?|hiring|about(-us)?|pricing|plans|tags?|category|categories|"
    r"authors?|search|login|signup|press-kit|contact(-us)?|privacy|terms|faq|"
    r"download|shop|store)(/|$)", re.I)
JUNK_HOSTS = ("jobs.", "careers.", "boards.greenhouse.io", "lever.co", "workable.com",
              "linkedin.com/jobs", "indeed.com", "glassdoor.com")

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
- Links: link to primary sources (the organization's own announcement or page) rather than other news outlets. Use markdown links. Only use URLs that appear in the lead material, the editor's links, or that you actually found through search or fetch.
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
    """status: New (wire stories), Ideas (new story ideas), Queued, or Drafted."""
    extra = {"Queued": ["Article Text", "Editor's Note", "Editor Links"],
             "Drafted": ["Draft Notes"], "Ideas": ["Article Text"]}.get(status, [])
    is_new = "OR({Status}='New',{Status}='')"
    if status == "New":
        formula = f"AND({is_new},{{Beat}}!='{IDEAS_TAB}')"
    elif status == "Ideas":
        formula = f"AND({is_new},{{Beat}}='{IDEAS_TAB}')"
    else:
        formula = f"{{Status}}='{status}'"
    return at_list(WIRE_TABLE, formula=formula, fields=WIRE_LIST_FIELDS + extra, sort_field="Published",
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


def _parse_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return parsedate_to_datetime(str(s))
        except Exception:
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
        jobs += [(TRADES_TAB, f"{ph} site:{s}", s) for s in TRADE_SITES for ph in TRADE_PHRASES]
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
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
            or soup.find("meta", attrs={"itemprop": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _page_published(html, soup=None):
    """The page's own publish date, from meta tags, <time>, or JSON-LD."""
    soup = soup or BeautifulSoup(html or "", "html.parser")
    d = _parse_iso(_meta(soup, "article:published_time", "og:article:published_time",
                         "datePublished", "pubdate", "publishdate", "parsely-pub-date",
                         "sailthru.date", "dc.date", "date"))
    if d:
        return d
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html or "")
    if m and (d := _parse_iso(m.group(1))):
        return d
    t = soup.find("time", attrs={"datetime": True})
    return _parse_iso(t["datetime"]) if t else None


def get_article(url):
    """Reads a page. Returns {"text", "published"}; plain scrape first, then Jina's reader."""
    out = {"text": None, "published": None}
    if not url or "news.google.com" in url:
        return out
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            out["published"] = _page_published(r.text, soup)
            text = _html_to_text(r.text)
            if len(text) > 600:
                out["text"] = text
                return out
    except Exception:
        pass
    try:
        headers = {"Authorization": f"Bearer {JINA_API_KEY}"} if JINA_API_KEY else {}
        r = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=25)
        if r.status_code == 200 and len(r.text) > 600:
            out["text"] = r.text
            if not out["published"]:
                m = re.search(r"^Published Time:\s*(.+)$", r.text, flags=re.M)
                out["published"] = _parse_iso(m.group(1)) if m else None
    except Exception:
        pass
    return out


def get_article_text(url):
    return get_article(url)["text"]


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
    return {"title": title.strip(), "link": url, "url": url, "outlet": outlet,
            "published": _page_published(r.text, soup), "keyword": outlet,
            "text": _html_to_text(r.text) or None, "beat": NEWSLETTERS_TAB}


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


def is_junk_url(url):
    """Job boards, about/product/pricing pages, tag indexes, and bare homepages are never news."""
    if not url or "news.google.com" in url:
        return False
    low = url.lower()
    if any(h in low for h in JUNK_HOSTS):
        return True
    path = urlparse(url).path.rstrip("/")
    return path == "" or bool(JUNK_URL.search(path + "/"))


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


CLASSIFY_SYSTEM = (
    "You screen items for the news wire of Zinstrel, a publication covering AI music: its culture, "
    "business, infrastructure, and policy. For each item decide:\n"
    "on_topic: true only if the item is substantively about AI AND music (AI music tools and models, "
    "AI in the music industry, AI artists and songs, music-AI law, licensing, detection, or business). "
    "General music-industry stories with no AI angle are false. General AI stories with no music angle "
    "are false.\n"
    "is_news: true only if the item reports a specific, recent development: an announcement, launch, "
    "deal, lawsuit or ruling, bill or regulation, funding round, acquisition, release, data or study, "
    "event, or a notable statement. False for product or company pages, 'about' pages, pricing pages, "
    "job listings, evergreen explainers and how-tos, tool roundups and listicles, reviews with no news, "
    "directory or tag pages, and anything with no datable development. If the page's own date shows "
    "it's old, is_news is false.\n"
    "summary: ONE plain sentence (under 25 words) saying what actually happened. Use only what's in "
    "the item; if only a headline is available, summarize the headline without adding facts."
)


def classify_batch(items, days_back):
    """items: dicts with title, outlet, url, text, page_date. Returns list of
    (summary, on_topic, is_news). Unparseable results are treated as rejects."""
    today = datetime.now(timezone.utc).date().isoformat()
    blocks = []
    for i, it in enumerate(items):
        excerpt = (it.get("text") or "")[:1500] or "(article text unavailable; headline only)"
        pd_ = it.get("page_date")
        blocks.append(
            f"[{i}] HEADLINE: {it['title']}\nOUTLET: {it.get('outlet') or 'unknown'}\n"
            f"URL: {it.get('url') or ''}\nPAGE DATE: {pd_.date().isoformat() if pd_ else 'unknown'}\n"
            f"TEXT: {excerpt}")
    user = (f"Today is {today}. Recent means within the last {days_back + 1} days.\n\n"
            + "\n\n".join(blocks)
            + '\n\nReturn ONLY a JSON array, one object per item, like '
              '[{"i": 0, "summary": "...", "on_topic": true, "is_news": true}]')
    got = {}
    for _ in range(2):  # one retry if the reply isn't clean JSON
        try:
            raw, _ = claude(SUMMARY_MODEL, CLASSIFY_SYSTEM, user, 220 * len(items) + 400)
            data = extract_json_array(raw)
            got = {int(d["i"]): (str(d.get("summary", "")).strip(), bool(d.get("on_topic")),
                                 bool(d.get("is_news"))) for d in data if isinstance(d, dict) and "i" in d}
            if got:
                break
        except Exception:
            got = {}
    return [got.get(i, ("", False, False)) for i in range(len(items))]


def extract_json_array(raw):
    """The last top-level JSON array of objects in a reply that may include other text."""
    dec, best = json.JSONDecoder(), None
    for m in re.finditer(r"\[\s*\{", raw):
        try:
            val, _ = dec.raw_decode(raw[m.start():])
        except ValueError:
            continue
        if isinstance(val, list) and val and isinstance(val[0], dict):
            best = val
    if best is None:
        raise ValueError("no JSON array found")
    return best


def word_count(text):
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return len(re.findall(r"\b[\w'’-]+\b", body))


def research_tools(fmt):
    searches = 3 if fmt == "Minor Signal" else 5
    return [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": searches},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 4, "max_content_tokens": 12000},
    ]


def claude_with_research(system, user, tools, max_tokens=8000, model=None):
    """Runs a turn that may search and fetch. Returns (all_text, was_cut_off, searches_used),
    or None if web search/fetch isn't enabled on the API account (caller retries without)."""
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = [{"role": "user", "content": user}]
    texts, searches = [], 0
    for _ in range(6):  # the API can pause long research turns; resume them
        try:
            resp = client.messages.create(model=model or DRAFT_MODEL, max_tokens=max_tokens, system=system,
                                          messages=messages, **({"tools": tools} if tools else {}))
        except Exception as e:
            if tools and any(w in str(e).lower() for w in ("web_search", "web search", "web_fetch", "tool")):
                return None
            raise
        texts.extend(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = getattr(getattr(resp, "usage", None), "server_tool_use", None)
        searches += getattr(usage, "web_search_requests", 0) or 0
        if getattr(resp, "stop_reason", "") == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        return "".join(texts), getattr(resp, "stop_reason", "") == "max_tokens", searches
    return "".join(texts), True, searches


SECTION_LABELS = ["SUMMARY", "EDITOR FLAGS", "FOLLOW-UP REPORTING", "PRIMARY SOURCES"]


def _section(text, label):
    stops = "|".join(l for l in SECTION_LABELS if l != label)
    m = re.search(rf"{label}:\s*(.*?)(?=(?:{stops}):|\Z)", text, flags=re.S | re.I)
    return m.group(1).strip() if m else ""


def _ngrams(text, n=6):
    words = re.findall(r"[a-z0-9']+", re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text.lower()))
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def summary_overlaps(summary, headline, body):
    """True if the summary reuses a 6-word run from the body or repeats the headline."""
    if not summary:
        return False
    if _norm(summary) == _norm(headline):
        return True
    return bool(_ngrams(summary) & _ngrams(body))


def rewrite_summary(summary, headline, body):
    system = ("You write the dek (summary) for a Zinstrel article. Write 1-2 sentences, under 45 words, "
              "that tell a reader what the piece covers and why it matters. Use fresh wording: do not "
              "reuse any phrase of six or more words from the article or repeat the headline. Plain "
              "prose, Oxford comma, no quotes. Return only the summary.")
    user = f"HEADLINE: {headline}\n\nARTICLE:\n{body[:8000]}\n\nCURRENT SUMMARY (too close to the article): {summary}"
    text, _ = claude(SUMMARY_MODEL, system, user, 300)
    return text.strip().strip('"')


def fetch_editor_links(links):
    """Reads each editor-provided link so the draft can use it even if web fetch can't."""
    out = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for url, art in zip(links, pool.map(get_article, links)):
            out.append((url, (art["text"] or "")[:6000]))
    return out


def draft_article(rec_fields, fmt, note, source_text, editor_links=()):
    """Researches primary sources, then writes. Returns dict with headline, body, summary,
    notes, words, cut_off, researched, searches."""
    system = ("You are a senior reporter at Zinstrel writing a first draft for the editor, Marcus "
              "Lawrence, to revise.\n\n" + HOUSE_RULES + "\n\n" + ORIGINAL_REPORTING)
    is_idea = rec_fields.get("Beat") == IDEAS_TAB
    lead_label = ("STORY IDEA (from Zinstrel's own idea desk; there is no lead article, so report it "
                  "out from scratch)" if is_idea else
                  "LEAD ITEM (a tip to report out, not a source to rewrite)")
    links_block = ""
    if editor_links:
        parts = []
        for url, text in fetch_editor_links(list(editor_links)):
            parts.append(f"- {url}\n  TEXT: {text[:6000] or '(could not read this page; fetch it if you can)'}")
        links_block = ("\n\nEDITOR'S LINKS (the editor wants each of these considered. Draw on them where "
                       "relevant and link to them in the body where they support a point. If one isn't "
                       "usable or relevant, say why under EDITOR FLAGS.)\n" + "\n".join(parts))
    user = f"""FORMAT: {fmt}
FORMAT GUIDANCE: {FORMATS[fmt]['guide']}
EDITOR'S NOTE: {note or '(none)'}

{lead_label}
HEADLINE: {rec_fields.get('Headline', '')}
OUTLET: {rec_fields.get('Outlet') or 'unknown'}
LINK: {rec_fields.get('URL', '') or '(none)'}

LEAD TEXT:
{source_text[:25000]}{links_block}

First, research: find and read the primary material behind this. Then write.
When you're done researching, output the finished piece in exactly this shape and nothing after it:

HEADLINE: <headline>

<body in markdown prose>
---
SUMMARY: <1-2 sentences, under 45 words, summarizing the whole piece for readers. Fresh wording: do not reuse any phrase of six or more words from the body, and don't repeat the headline.>
EDITOR FLAGS: <anything unconfirmed, discrepancies between the lead and primary sources, quotes you could not verify. "None." if nothing.>
FOLLOW-UP REPORTING: <1-3 people or organizations Zinstrel should contact for its own comment, and the question to ask each.>
PRIMARY SOURCES: <the primary-source URLs you used, one per line. "None found." if none.>"""
    result = claude_with_research(system, user, research_tools(fmt))
    researched = result is not None
    if not researched:
        user = user.replace("First, research: find and read the primary material behind this. Then write.",
                            "Web research is unavailable, so write from the material above, keep claims "
                            "attributed, and list what needs primary-source checking under EDITOR FLAGS.")
        result = claude_with_research(system, user, None)
    raw, cut_off, searches = result

    # The finished piece starts at the last HEADLINE: line (earlier text is research narration).
    starts = list(re.finditer(r"^[ \t#*]*HEADLINE:", raw, flags=re.M))
    text = raw[starts[-1].start():] if starts else raw
    headline = rec_fields.get("Headline", "")
    m = re.match(r"^[ \t#*]*HEADLINE:\s*(.+?)\s*\n", text, flags=re.IGNORECASE)
    if m:
        headline = m.group(1).strip().strip('"*# ')
        text = text[m.end():].strip()
    body, _, tail = text.partition("\n---")
    body = body.strip()
    summary = _section(tail, "SUMMARY")
    flags = _section(tail, "EDITOR FLAGS") or "None."
    follow = _section(tail, "FOLLOW-UP REPORTING")
    sources = _section(tail, "PRIMARY SOURCES")

    summary_note = ""
    if not summary or summary_overlaps(summary, headline, body):
        try:
            summary = rewrite_summary(summary, headline, body)
        except Exception:
            pass
        if summary_overlaps(summary, headline, body):
            summary_note = " (⚠️ summary still echoes the article; edit before publishing)"

    if not researched:
        flags = "⚠️ Web research was unavailable, so this was written from the lead only. " + flags
    notes = f"Summary: {summary}{summary_note}\n\nEditor flags: {flags}"
    if follow:
        notes += f"\n\nFollow-up reporting: {follow}"
    if sources:
        notes += f"\n\nPrimary sources:\n{sources}"
    return {"headline": headline, "body": body, "summary": summary, "notes": notes,
            "words": word_count(body), "cut_off": cut_off, "researched": researched,
            "searches": searches}


# ============================================================
# DESTINATIONS
# ============================================================
def send_to_lovable(draft, fmt, rec_fields):
    payload = {
        "title": draft["headline"],
        "body_markdown": draft["body"],
        "format": fmt,
        "summary": draft["summary"],
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
    stats = {"found": 0, "junk_pages": 0, "stale": 0, "not_news": 0, "added": 0, "capped": 0, "removed": 0}
    progress(0.02, "Searching Google News and newsletters…")
    found = fetch_headlines(beats, days_back, include_trades) if (beats or include_trades) else []
    if include_newsletters:
        with ThreadPoolExecutor(max_workers=len(NEWSLETTERS)) as pool:
            for posts in pool.map(lambda s: fetch_newsletter(s, days_back), NEWSLETTERS):
                found.extend(posts)
    stats["found"] = len(found)

    progress(0.10, "Checking what's already on the wire…")
    existing = at_list(WIRE_TABLE, fields=["Google Link", "Headline", "URL"])
    seen = set()
    for rec in existing:
        f = rec.get("fields", {})
        seen.update(x for x in (f.get("Google Link"), f.get("URL")) if x)
        seen.add("t:" + _norm(f.get("Headline")))
    fresh = [a for a in found if a["link"] not in seen and "t:" + _norm(a["title"]) not in seen]
    fresh.sort(key=lambda a: a["published"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    stats["capped"] = max(0, len(fresh) - max_new)
    fresh = fresh[:max_new]
    if not fresh:
        stats["removed"] = cleanup_wire()
        return stats

    to_decode = [a for a in fresh if not a.get("url")]  # newsletters already have their own link
    n = len(to_decode)
    with ThreadPoolExecutor(max_workers=4) as pool:  # gentle on Google
        futs = {pool.submit(resolve_publisher_url, a["link"]): a for a in to_decode}
        for k, fut in enumerate(as_completed(futs), 1):
            futs[fut]["url"] = fut.result()
            progress(0.10 + 0.25 * k / n, f"Finding original articles… {k}/{n}")

    before = len(fresh)
    fresh = [a for a in fresh if a["url"] not in seen and not is_junk_url(a["url"])]
    stats["junk_pages"] = before - len(fresh)

    to_read = [a for a in fresh if not a.get("text")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(get_article, a["url"]): a for a in to_read}
        for k, fut in enumerate(as_completed(futs), 1):
            art = fut.result()
            futs[fut]["text"] = art["text"]
            futs[fut]["page_date"] = art["published"]
            progress(0.35 + 0.30 * k / max(len(to_read), 1), f"Reading articles… {k}/{len(to_read)}")

    # Google stamps evergreen pages with today's date; trust the page's own date when it has one.
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=max(days_back, 1) + 2)
    before = len(fresh)
    fresh = [a for a in fresh if a["beat"] == NEWSLETTERS_TAB or not a.get("page_date")
             or a["page_date"] >= stale_cutoff]
    stats["stale"] = before - len(fresh)
    for a in fresh:
        if a.get("page_date") and a["beat"] != NEWSLETTERS_TAB:
            a["published"] = a["page_date"]

    batches = [fresh[i:i + 8] for i in range(0, len(fresh), 8)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(classify_batch, b, days_back): b for b in batches}
        for k, fut in enumerate(as_completed(futs), 1):
            for a, (summary, on_topic, is_news) in zip(futs[fut], fut.result()):
                a["summary"], a["on_topic"], a["is_news"] = summary, on_topic, is_news
            progress(0.65 + 0.25 * k / max(len(batches), 1),
                     f"Keeping only timely AI-music news… {k}/{len(batches)}")
    keep = [a for a in fresh if a.get("on_topic") and a.get("is_news")]
    stats["not_news"] = len(fresh) - len(keep)

    progress(0.92, "Saving to the Wire…")
    at_create(WIRE_TABLE, [wire_row(a) for a in keep])
    stats["added"] = len(keep)
    stats["removed"] = cleanup_wire()
    progress(1.0, "Done")
    return stats


def wire_row(a):
    return {
        "Headline": a["title"],
        "Summary": a.get("summary"),
        "Status": "New",
        "Beat": a["beat"],
        "Outlet": a["outlet"],
        "URL": a["url"],
        "Google Link": a["link"],
        "Published": a["published"].isoformat() if a.get("published") else None,
        "On Topic": True,
        "Readable": bool(a.get("text")),
        "Article Text": (a.get("text") or "")[:ARTICLE_TEXT_CAP],
        "Keyword": a.get("keyword"),
    }


def cleanup_wire():
    formula = (f"AND(OR({{Status}}='New',{{Status}}='Skipped',{{Status}}=''),"
               f"IS_BEFORE(CREATED_TIME(),DATEADD(NOW(),-{KEEP_DAYS},'days')))")
    try:
        old = at_list(WIRE_TABLE, formula=formula, fields=["Headline"])
        at_delete(WIRE_TABLE, [r["id"] for r in old])
        return len(old)
    except Exception:
        return 0


def recheck_wire(progress):
    """Runs the news filter over everything already on the Wire and skips what fails."""
    recs = at_list(WIRE_TABLE, formula=f"AND(OR({{Status}}='New',{{Status}}=''),{{Beat}}!='{IDEAS_TAB}')",
                   fields=["Headline", "Outlet", "URL", "Article Text", "Beat", "Published"])
    if not recs:
        return 0, 0
    items = [{"title": r["fields"].get("Headline", ""), "outlet": r["fields"].get("Outlet"),
              "url": r["fields"].get("URL"), "text": r["fields"].get("Article Text"),
              "page_date": None, "id": r["id"]} for r in recs]
    junk = [it for it in items if is_junk_url(it["url"])]
    rest = [it for it in items if not is_junk_url(it["url"])]
    batches = [rest[i:i + 8] for i in range(0, len(rest), 8)]
    rejects = [it["id"] for it in junk]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(classify_batch, b, 3): b for b in batches}
        for k, fut in enumerate(as_completed(futs), 1):
            for it, (_, on_topic, is_news) in zip(futs[fut], fut.result()):
                if not (on_topic and is_news):
                    rejects.append(it["id"])
            progress(k / max(len(batches), 1), f"Re-checking the wire… {k}/{len(batches)}")
    at_update(WIRE_TABLE, [(rid, {"Status": "Skipped"}) for rid in rejects])
    return len(recs), len(rejects)


# ============================================================
# STORY IDEAS
# ============================================================
TREND_TERMS = re.compile(r"\b(ai|a\.i\.|suno|udio|elevenlabs|music|song|songs|singer|rapper|album|"
                         r"spotify|grammy|billboard|artist|concert|tour|band|playlist|streaming)\b", re.I)


def _gcp_creds(scopes):
    from google.oauth2 import service_account
    info = dict(st.secrets["gcp_service_account"])
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)


def published_history(limit=60):
    """Titles Zinstrel has published (site + Substack), newest first."""
    try:
        r = requests.get(PUBLISHED_FEED, headers={"User-Agent": BROWSER_UA}, timeout=15)
        items = r.json().get("items", []) if r.status_code == 200 else []
    except Exception:
        items = []
    out = [{"title": it.get("title", ""), "url": it.get("url", ""), "date": (it.get("published_at") or "")[:10],
            "source": it.get("source", "")} for it in items if it.get("title")]
    if not out:  # fall back to the editorial calendar in Airtable
        try:
            recs = at_list(STORIES_TABLE, formula="{Status}='Published'", fields=["Title/Topic", "Pub Date"],
                           sort_field="Pub Date", max_records=limit)
            out = [{"title": r["fields"].get("Title/Topic", ""), "url": "", "date": r["fields"].get("Pub Date", ""),
                    "source": "stories"} for r in recs]
        except Exception:
            out = []
    return out[:limit]


def ga_top_pages(days=28, limit=25):
    """Top pages by views from Google Analytics 4. Returns [] if not configured."""
    if not GA4_PROPERTY_ID or "gcp_service_account" not in st.secrets:
        return []
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, OrderBy, RunReportRequest
    client = BetaAnalyticsDataClient(credentials=_gcp_creds(["https://www.googleapis.com/auth/analytics.readonly"]))
    req = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="pageTitle"), Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews"), Metric(name="averageSessionDuration")],
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        limit=limit,
    )
    resp = client.run_report(req)
    return [{"title": r.dimension_values[0].value, "path": r.dimension_values[1].value,
             "views": int(float(r.metric_values[0].value or 0)),
             "avg_session_sec": round(float(r.metric_values[1].value or 0))} for r in resp.rows]


def gsc_queries(days=28, limit=200):
    """Search queries that bring people to Zinstrel, from Search Console. [] if not configured."""
    if not GSC_SITE_URL or "gcp_service_account" not in st.secrets:
        return []
    from googleapiclient.discovery import build
    svc = build("searchconsole", "v1", cache_discovery=False,
                credentials=_gcp_creds(["https://www.googleapis.com/auth/webmasters.readonly"]))
    end = date.today()
    body = {"startDate": (end - timedelta(days=days)).isoformat(), "endDate": end.isoformat(),
            "dimensions": ["query"], "rowLimit": limit}
    rows = svc.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute().get("rows", [])
    return [{"query": r["keys"][0], "clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0),
             "ctr": round(r.get("ctr", 0), 3), "position": round(r.get("position", 0), 1)} for r in rows]


def google_trending(geo="US"):
    """Today's trending Google searches that touch AI or music. Google Trends has no public API;
    this is its daily trending feed, filtered."""
    try:
        r = requests.get(f"https://trends.google.com/trending/rss?geo={geo}",
                         headers={"User-Agent": BROWSER_UA}, timeout=12)
        feed = feedparser.parse(r.content)
    except Exception:
        return []
    out = []
    for e in feed.entries:
        news = " ".join(v for k, v in e.items() if "news_item_title" in k and isinstance(v, str))
        blob = f"{e.get('title', '')} {news}"
        if TREND_TERMS.search(blob):
            out.append({"term": e.get("title", ""), "traffic": e.get("ht_approx_traffic", ""), "context": news[:200]})
    return out


def gather_signals():
    """Everything the idea desk looks at. Each source fails quietly so one outage doesn't block ideas."""
    sig = {"published": [], "ga": [], "gsc": [], "trending": [], "wire": [], "errors": []}
    for key, fn in (("published", published_history), ("ga", ga_top_pages), ("gsc", gsc_queries),
                    ("trending", google_trending)):
        try:
            sig[key] = fn()
        except Exception as e:
            sig["errors"].append(f"{key}: {str(e)[:120]}")
    try:
        sig["wire"] = [{"headline": r["fields"].get("Headline"), "summary": r["fields"].get("Summary"),
                        "outlet": r["fields"].get("Outlet"), "url": r["fields"].get("URL"),
                        "date": (r["fields"].get("Published") or "")[:10]}
                       for r in load_wire("New")][:40]
    except Exception as e:
        sig["errors"].append(f"wire: {str(e)[:120]}")
    return sig


IDEAS_SYSTEM = """You are the assignment editor at Zinstrel, an independent publication covering AI music: its culture, business, infrastructure, and policy. Readers are decision-makers at labels, platforms, and AI music companies, plus journalists. House formats: Major Signal (~300-word lead news item), Minor Signal (2-3 sentences), News Brief (300-600 words), Culture (scene-driven feature, ~900 words), Frame (analytical essay, ~1,200 words).

Propose stories Zinstrel should write next. Every idea must:
- Sit squarely in Zinstrel's sphere (AI + music).
- Be timely: tie it to a specific, dated development from the last two weeks, and say what it is. Use web search to confirm the development and find newer ones.
- Not repeat what Zinstrel already published. A follow-up is fine only if there's a genuinely new development, and say so.
- Use the search data when there is any: target a query from Search Console or a trending search where it fits, and say which. If there's no search data for an idea, say "no search data."
- Be reportable: name the primary sources to start from (announcements, filings, bills, datasets, people to interview)."""


def suggest_ideas(n, focus, sig):
    def block(title, rows, fmt):
        return f"{title}:\n" + ("\n".join(fmt(r) for r in rows) if rows else "(none available)")
    gsc = sig["gsc"]
    opportunities = sorted([q for q in gsc if q["impressions"] >= 20 and q["position"] > 4],
                           key=lambda q: -q["impressions"])[:30]
    user = "\n\n".join([
        f"Today is {date.today().isoformat()}. Propose {n} story ideas.",
        f"EDITOR'S FOCUS: {focus or '(none; use your judgment)'}",
        block("ALREADY PUBLISHED BY ZINSTREL (newest first)", sig["published"][:50],
              lambda r: f"- {r['date']} | {r['title']}"),
        block("TOP PAGES, LAST 28 DAYS (Google Analytics)", sig["ga"],
              lambda r: f"- {r['views']} views | {r['title']} ({r['path']})"),
        block("TOP SEARCH QUERIES BRINGING READERS (Search Console, 28 days)", gsc[:40],
              lambda r: f"- {r['query']} | {r['clicks']} clicks, {r['impressions']} impressions, pos {r['position']}"),
        block("SEARCH OPPORTUNITIES (queries Zinstrel shows up for but ranks low on)", opportunities,
              lambda r: f"- {r['query']} | {r['impressions']} impressions, pos {r['position']}"),
        block("GOOGLE TRENDING SEARCHES TODAY THAT TOUCH AI OR MUSIC", sig["trending"],
              lambda r: f"- {r['term']} ({r['traffic']}) {r['context']}"),
        block("ON THE WIRE NOW (timely AI-music news)", sig["wire"],
              lambda r: f"- {r['date']} | {r['headline']} — {r['summary']} ({r['outlet']}) {r['url']}"),
        """Return ONLY a JSON array (after any research), one object per idea:
[{"headline": "working headline in Zinstrel style",
  "angle": "one sentence: the story and Zinstrel's take",
  "why_now": "the specific dated development that makes it timely",
  "format": "Major Signal | Minor Signal | News Brief | Culture | Frame",
  "seo_target": "the search query to target, or 'no search data'",
  "signals": "which of the inputs above support it (published gap, GA, Search Console, trending, wire)",
  "sources": ["primary-source URLs to start reporting from"]}]""",
    ])
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]
    result = claude_with_research(IDEAS_SYSTEM, user, tools, max_tokens=6000)
    if result is None:
        result = claude_with_research(IDEAS_SYSTEM, user, None, max_tokens=6000)
    ideas = extract_json_array(result[0])
    return [i for i in ideas if isinstance(i, dict) and i.get("headline")][:n]


def save_ideas(ideas):
    rows = []
    for i in ideas:
        sources = [s for s in (i.get("sources") or []) if isinstance(s, str) and s.startswith("http")]
        brief = (f"ANGLE: {i.get('angle', '')}\nWHY NOW: {i.get('why_now', '')}\n"
                 f"SUGGESTED FORMAT: {i.get('format', '')}\nSEO TARGET: {i.get('seo_target', '')}\n"
                 f"SIGNALS: {i.get('signals', '')}\nSTART FROM:\n" + "\n".join(sources))
        rows.append({
            "Headline": i["headline"], "Summary": i.get("angle", ""), "Status": "New", "Beat": IDEAS_TAB,
            "Outlet": "Zinstrel idea desk", "URL": sources[0] if sources else None,
            "Published": datetime.now(timezone.utc).isoformat(), "On Topic": True, "Readable": True,
            "Article Text": brief, "Keyword": (i.get("seo_target") or "")[:200],
        })
    return at_create(WIRE_TABLE, rows)


# ============================================================
# ACTIONS (button callbacks run before the page redraws)
# ============================================================
def _flash(kind, msg):
    st.session_state.setdefault("flash", []).append((kind, msg))


def set_status_for_selected(prefix, status):
    ids = [k[len(prefix):] for k, v in st.session_state.items()
           if k.startswith(prefix) and v is True and k[len(prefix):].startswith("rec")]
    if not ids:
        return
    try:
        at_update(WIRE_TABLE, [(rid, {"Status": status}) for rid in ids])
        for rid in ids:
            st.session_state.pop(prefix + rid, None)
        verb = {"Queued": "Queued", "Skipped": "Skipped", "New": "Moved back"}[status]
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


def collect_links(rid):
    links = []
    for j in range(MAX_EDITOR_LINKS):
        v = (st.session_state.get(f"ql_{rid}_{j}") or "").strip()
        if v:
            links.append(v if v.startswith("http") else "https://" + v)
    return links


def save_queue_fields(rid):
    """Saves the editor's note and links as they're typed, so nothing is lost on reload."""
    try:
        at_update(WIRE_TABLE, [(rid, {"Editor's Note": st.session_state.get(f"qn_{rid}", ""),
                                      "Editor Links": "\n".join(collect_links(rid))})])
        load_wire.clear()
    except Exception as e:
        _flash("error", f"Couldn't save to Airtable: {e}")


# ============================================================
# STYLE (light theme so the logo's full gradient reads)
# ============================================================
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
.zn-title a, .zn-title span { color: #1E1747; text-decoration: none; font-weight: 600; font-size: 1.02rem; }
.zn-title a:hover { color: #0A7FD6; }
.zn-sum { font-size: 0.93rem; color: #3D3666; margin-top: 4px; }
.zn-meta { font-size: 0.78rem; color: #7A7499; margin-top: 6px; }
.zn-tag { display: inline-block; font-size: 0.7rem; padding: 1px 8px; border-radius: 99px;
          background: #E8F4FF; color: #0566B8; margin-left: 6px; }
.zn-warn { background: #FFF3DC; color: #8A5300; }
.zn-idea { background: #FCE8FA; color: #9A0090; }
.zn-why { font-size: 0.85rem; color: #3D3666; margin-top: 6px; }
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


def brief_field(brief, label):
    m = re.search(rf"^{label}:\s*(.*)$", brief or "", flags=re.M)
    return m.group(1).strip() if m else ""


def card(f, selected=False, extra_tags=""):
    tags = extra_tags
    is_idea = f.get("Beat") == IDEAS_TAB
    if not f.get("Readable") and not is_idea:
        tags += '<span class="zn-tag zn-warn">headline only</span>'
    title = (f'<a href="{esc(f.get("URL"))}" target="_blank">{esc(f.get("Headline"))}</a>'
             if f.get("URL") and not is_idea else f'<span>{esc(f.get("Headline"))}</span>')
    why = ""
    if is_idea:
        brief = f.get("Article Text", "")
        bits = [("Why now", brief_field(brief, "WHY NOW")), ("Format", brief_field(brief, "SUGGESTED FORMAT")),
                ("Search target", brief_field(brief, "SEO TARGET"))]
        why = "".join(f"<div class='zn-why'><b>{k}:</b> {esc(v)}</div>" for k, v in bits if v)
        meta = '<span class="zn-tag zn-idea">idea</span>'
    else:
        meta = (f'{esc(f.get("Outlet") or "Unknown outlet")} · {fmt_date(f.get("Published"))}'
                f'<span class="zn-tag">{esc(f.get("Beat") or "")}</span>')
    st.markdown(
        f'<div class="zn-card {"sel" if selected else ""}"><div class="zn-title">{title}</div>'
        f'<div class="zn-sum">{esc(f.get("Summary") or "")}</div>{why}'
        f'<div class="zn-meta">{meta}{tags}</div></div>',
        unsafe_allow_html=True,
    )


def selectable_list(records, prefix):
    for r in records:
        f = r["fields"]
        key = f"{prefix}{r['id']}"
        a, b = st.columns([0.04, 0.96])
        with a:
            st.checkbox(" ", key=key, label_visibility="collapsed")
        with b:
            card(f, st.session_state.get(key, False))


def bulk_buttons(prefix, skip_label="✕ Skip selected"):
    n_sel = sum(1 for k, v in st.session_state.items() if k.startswith(prefix) and v)
    c1, c2, _ = st.columns([1, 1, 3])
    # Button keys must not start with the checkbox prefix, or a click would count as a selection.
    c1.button(f"➕ Queue selected ({n_sel})", key=f"btn_queue_{prefix}", on_click=set_status_for_selected,
              args=(prefix, "Queued"), disabled=not n_sel, use_container_width=True, type="primary")
    c2.button(f"{skip_label} ({n_sel})", key=f"btn_skip_{prefix}", on_click=set_status_for_selected,
              args=(prefix, "Skipped"), disabled=not n_sel, use_container_width=True)


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
    recheck_clicked = st.button("🧹 Re-check the wire", use_container_width=True,
                                help="Runs the news-only filter over what's already on the Wire and skips the rest.")
    st.divider()
    st.caption(f"Drafts go to: **{'Lovable CMS' if lovable_on else 'Stories (Airtable)'}**")
    st.caption(f"Drafting: {DRAFT_MODEL} · Screening: {SUMMARY_MODEL}")
    if st.button("↻ Reload from Airtable", use_container_width=True):
        refresh_wire()

if scan_clicked:
    bar = st.progress(0.0, text="Starting scan…")
    try:
        s = run_scan(tuple(chosen_beats), days_back, include_trades, max_new,
                     lambda p, t: bar.progress(min(p, 1.0), text=t), include_newsletters)
        tossed = s["junk_pages"] + s["stale"] + s["not_news"]
        msg = f"Scan done: {s['added']} new stories on the wire ({s['found']} headlines checked)."
        if tossed:
            msg += (f" Filtered out {tossed}: {s['not_news']} not timely AI-music news, "
                    f"{s['stale']} old pages, {s['junk_pages']} job/product/about pages.")
        if s["capped"]:
            msg += f" {s['capped']} more were left for the next scan."
        if s["removed"]:
            msg += f" Cleared {s['removed']} unused items older than {KEEP_DAYS} days."
        st.success(msg)
    except Exception as e:
        st.error(f"Scan failed: {e}")
    refresh_wire()

if recheck_clicked:
    bar = st.progress(0.0, text="Re-checking the wire…")
    try:
        checked, skipped = recheck_wire(lambda p, t: bar.progress(min(p, 1.0), text=t))
        st.success(f"Re-checked {checked} stories and skipped {skipped} that weren't timely AI-music news.")
    except Exception as e:
        st.error(f"Re-check failed: {e}")
    refresh_wire()

try:
    wire = [r for r in load_wire("New") if r["fields"].get("On Topic", True)]
    ideas = load_wire("Ideas")
    queue = load_wire("Queued")
except Exception as e:
    st.error(f"Couldn't load the Wire from Airtable: {e}")
    st.stop()

tab_wire, tab_ideas, tab_queue, tab_done = st.tabs(
    [f"Wire ({len(wire)})", f"Ideas ({len(ideas)})", f"Queue ({len(queue)})", "Drafted"])

# ---------------- WIRE ----------------
with tab_wire:
    if not wire:
        st.info("The wire is empty. Click **Scan now** in the sidebar.")
    else:
        bulk_buttons("w_")
        beats_present = [b for b in WIRE_BEATS if any(r["fields"].get("Beat") == b for r in wire)]
        btabs = st.tabs([f"{b} ({sum(1 for r in wire if r['fields'].get('Beat') == b)})" for b in beats_present])
        for bt, beat in zip(btabs, beats_present):
            with bt:
                selectable_list([r for r in wire if r["fields"].get("Beat") == beat], "w_")

# ---------------- IDEAS ----------------
with tab_ideas:
    st.caption("Story ideas in Zinstrel's sphere, from what you've published, what's on the Wire, Google's "
               "trending searches, and Google Analytics and Search Console when they're connected.")
    c1, c2, c3 = st.columns([3, 1, 1.2])
    focus = c1.text_input("Focus (optional)", placeholder="e.g. licensing, detection, Suno, artists, policy",
                          label_visibility="collapsed")
    n_ideas = c2.number_input("How many", 3, 10, 6, label_visibility="collapsed")
    go_ideas = c3.button("💡 Suggest stories", type="primary", use_container_width=True)
    if go_ideas:
        with st.status("Working on ideas…", expanded=True) as box:
            st.write("Gathering signals: your published work, the Wire, trending searches, analytics…")
            sig = gather_signals()
            parts = [f"{len(sig['published'])} published pieces", f"{len(sig['wire'])} wire stories",
                     f"{len(sig['trending'])} relevant trending searches"]
            parts.append(f"{len(sig['ga'])} top pages from Analytics" if sig["ga"] else "Analytics not connected")
            parts.append(f"{len(sig['gsc'])} search queries" if sig["gsc"] else "Search Console not connected")
            st.write(" · ".join(parts))
            for err in sig["errors"]:
                st.write(f"⚠️ Couldn't read {err}")
            st.write("Checking what's new and drafting pitches (this takes a minute)…")
            try:
                new_ideas = suggest_ideas(int(n_ideas), focus, sig)
                save_ideas(new_ideas)
                box.update(label=f"Added {len(new_ideas)} ideas", state="complete", expanded=False)
            except Exception as e:
                box.update(label=f"Couldn't generate ideas: {e}", state="error")
        refresh_wire()
        st.rerun()
    if ideas:
        bulk_buttons("i_", "✕ Dismiss selected")
        selectable_list(ideas, "i_")
    else:
        st.info("No ideas yet. Click **Suggest stories**.")

# ---------------- QUEUE ----------------
with tab_queue:
    if not queue:
        st.info("Nothing queued. Select stories on the Wire or Ideas tab and click **Queue selected**.")
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
                          placeholder="Editor's note (optional angle)", label_visibility="collapsed",
                          on_change=save_queue_fields, args=(rid,))
            c3.button("Remove", key=f"qr_{rid}", on_click=set_status_one, args=(rid, "Skipped"),
                      use_container_width=True)
            saved_links = [l for l in (f.get("Editor Links") or "").splitlines() if l.strip()]
            n_links = len([1 for j in range(MAX_EDITOR_LINKS) if st.session_state.get(f"ql_{rid}_{j}")]) \
                or len(saved_links)
            with st.expander(f"🔗 Links to include ({n_links}/{MAX_EDITOR_LINKS}, optional)"):
                st.caption("Links you want considered in the writing or linked in the piece. "
                           "Leave blank and Claude finds its own sources.")
                for j in range(MAX_EDITOR_LINKS):
                    st.text_input(f"Link {j + 1}", key=f"ql_{rid}_{j}",
                                  value=saved_links[j] if j < len(saved_links) else "",
                                  placeholder="https://", label_visibility="collapsed",
                                  on_change=save_queue_fields, args=(rid,))
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
                links = collect_links(rid)
                status = st.empty()
                try:
                    source = pasted.strip() or f.get("Article Text") or ""
                    if not source:
                        status.info(f"Reading **{f.get('Headline')}**…")
                        source = get_article_text(f.get("URL", "")) or ""
                    if not source and not links:
                        status.warning(f"**{f.get('Headline')}**: couldn't read the article and nothing was "
                                       "pasted, so it stays in the queue. Paste the text or add links and try again.")
                        continue
                    status.info(f"Reporting out **{f.get('Headline')}** (finding primary sources"
                                f"{', reading your ' + str(len(links)) + ' links' if links else ''}, then "
                                f"writing a {fmt})… this can take a minute.")
                    draft = draft_article(f, fmt, note, source, links)

                    where, link, fell_back = "Lovable", "", None
                    if lovable_on:
                        try:
                            link = send_to_lovable(draft, fmt, f)
                        except Exception as e:
                            fell_back = str(e)
                    if not lovable_on or fell_back:
                        where, link = "Stories", send_to_stories(draft, fmt, f, note)

                    updates = {"Status": "Drafted", "Format": fmt, "Editor's Note": note,
                               "Editor Links": "\n".join(links), "Draft Link": link or None,
                               "Draft Notes": draft["notes"]}
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
                        st.markdown(f"### {draft['headline']}\n\n*{draft['summary']}*\n\n{draft['body']}")
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
