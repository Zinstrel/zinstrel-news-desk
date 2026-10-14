"""
Zinstrel News Desk
------------------
Scans Google News for AI music stories by beat, lets you pick the ones worth
covering, drafts them in Zinstrel's house formats, and files them into the
Stories table in the AirMarcus Airtable base.

Separate tool from the BenefitKarma news desk: own repo, own app, own secrets.
"""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ============================================================
# CONFIG
# ============================================================
st.set_page_config(page_title="Zinstrel News Desk", page_icon="📡", layout="wide")


def get_secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


AIRTABLE_TOKEN = get_secret("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = get_secret("AIRTABLE_BASE_ID", "appOpnIsWZttCgxE6")   # AirMarcus
AIRTABLE_TABLE = get_secret("AIRTABLE_TABLE", "tblaG75vgdZabVJmj")       # Stories

ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY")
OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
LLM_PROVIDER = get_secret("LLM_PROVIDER", "anthropic" if ANTHROPIC_API_KEY else "openai")
ANTHROPIC_MODEL = get_secret("ANTHROPIC_MODEL", "claude-sonnet-5-5")
OPENAI_MODEL = get_secret("OPENAI_MODEL", "gpt-4o")

# Stories table field names
COL_TITLE = "Title/Topic"
COL_TYPE = "Type"
COL_STATUS = "Status"
COL_NOTES = "Notes"
COL_PUB_DATE = "Pub Date"
COL_SOURCE_URL = "Source URL"
COL_SOURCE_OUTLET = "Source Outlet"
COL_BEAT = "Beat"
COL_DRAFT = "Article Draft"
NEW_STATUS = "Concept Stage"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# ============================================================
# BEATS + KEYWORDS
# Each keyword becomes its own Google News query; results are merged
# and deduplicated per beat.
# ============================================================
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

# Trade outlets get their own sweep so they don't get buried.
TRADE_SITES = [
    "musicbusinessworldwide.com", "billboard.com", "digitalmusicnews.com",
    "completemusicupdate.com", "musically.com", "hypebot.com", "theverge.com",
    "musicweek.com", "variety.com",
]
TRADES_TAB = "From the Trades"

# Draft formats -> Stories "Type" option + target length + structure guidance
FORMATS = {
    "Signals item": {
        "type": "Signals (Mon)", "words": 120,
        "guide": (
            "A single Signals list item for Monday's Signals issue. About 100-150 words. "
            "A short plain-language header line, then straight-through prose: what happened, "
            "who it involves, and the one-sentence reason it matters for AI music. Minimal commentary."
        ),
    },
    "Major Signal": {
        "type": "Signals (Mon)", "words": 300,
        "guide": (
            "The MAJOR SIGNAL that opens Monday's Signals issue. About 300 words. The headline "
            "doubles as the email subject line, so it must carry the argument. Lead with the news, "
            "give the context needed to understand it, and close with the stakes for the AI music "
            "business, infrastructure, or policy landscape."
        ),
    },
    "Small signal": {
        "type": "Signal (Website Briefs)", "words": 60,
        "guide": "A small signal for zinstrel.com: a simple header and 2-3 sentences. Nothing more.",
    },
    "Culture": {
        "type": "Culture (Wed)", "words": 900,
        "guide": (
            "A Wednesday Culture feature, about 900 words. Structure: one scene, one analysis, "
            "one stake. Open in a concrete scene drawn from the reporting, move into analysis of "
            "what the scene reveals, and land on what is at stake. Scene-driven, not a news recap."
        ),
    },
    "Frame": {
        "type": "Frame (Fri)", "words": 1200,
        "guide": (
            "A Friday Frame, about 1,200 words: a long-form analytical essay with a clear "
            "argument. State the thesis early, build it with evidence and context (history, "
            "comparable cases, the business and policy mechanics), address the strongest "
            "counterpoint, and close on implications."
        ),
    },
    "Breaking News": {
        "type": "Breaking News (Substack)", "words": 400,
        "guide": (
            "A breaking news post for Substack, about 400 words. Lead with the news in the first "
            "sentence, then confirmed facts, context, and what to watch next."
        ),
    },
    "Tips & Tools": {
        "type": "Tips & Tools", "words": 600,
        "guide": (
            "A Tips & Tools piece, about 600 words, explaining the tool or update, what it "
            "actually does, how it compares to what exists, and who it matters for. Prose, not lists."
        ),
    },
    "Pitch only (no draft)": {"type": "Uncertain", "words": 0, "guide": ""},
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
- Links: link to primary sources (the organization's own announcement or page) rather than other news outlets where possible. Use markdown links. Only use URLs that appear in the source material.
- Do not label anything "MAJOR SIGNAL" unless the requested format is Major Signal."""


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


def _fetch_query(query, days_back, keyword_tag):
    url = (
        "https://news.google.com/rss/search?q="
        f"{quote_plus(query)}+when:{days_back}d&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=12)
        feed = feedparser.parse(r.content)
    except Exception:
        return []
    out = []
    for e in feed.entries:
        outlet = ""
        if e.get("source"):
            outlet = e.source.get("title", "") if hasattr(e.source, "get") else ""
        published = _parse_date(e)
        summary_html = e.get("summary", "")
        summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)
        out.append({
            "title": _clean_title(e.get("title", "").strip(), outlet),
            "link": e.get("link", ""),
            "outlet": outlet,
            "published": published,
            "summary": summary,
            "keyword": keyword_tag,
        })
    return out


def _norm(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:80]


def _dedupe(articles, limit):
    seen, out = set(), []
    for a in sorted(
        articles,
        key=lambda x: x["published"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    ):
        k = _norm(a["title"])
        if not a["title"] or k in seen or a["link"] in seen:
            continue
        seen.add(k)
        seen.add(a["link"])
        out.append(a)
        if len(out) >= limit:
            break
    return out


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_all(beats, days_back, per_beat, include_trades):
    jobs = []
    for beat in beats:
        for kw in BEATS[beat]:
            jobs.append((beat, f'"{kw}"' if " " in kw else kw, kw))
    if include_trades:
        for site in TRADE_SITES:
            jobs.append((TRADES_TAB, f"AI music site:{site}", site))

    results = {b: [] for b in list(beats) + ([TRADES_TAB] if include_trades else [])}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_fetch_query, q, days_back, tag): beat for beat, q, tag in jobs}
        for fut, beat in futures.items():
            results[beat].extend(fut.result())

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back + 1)
    claimed_links, claimed_titles = set(), set()   # each story shows once, under its first beat
    for beat in results:
        fresh = [
            a for a in results[beat]
            if (not a["published"] or a["published"] >= cutoff)
            and a["link"] not in claimed_links and _norm(a["title"]) not in claimed_titles
        ]
        results[beat] = _dedupe(fresh, per_beat)
        for a in results[beat]:
            claimed_links.add(a["link"])
            claimed_titles.add(_norm(a["title"]))
    return results


def get_article_text(url):
    """Jina reader first (handles Google News redirects + JS pages), then a plain scrape."""
    try:
        r = requests.get(f"https://r.jina.ai/{url}", timeout=20)
        if r.status_code == 200 and len(r.text) > 600:
            return r.text[:30000]
    except Exception:
        pass
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                tag.decompose()
            paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
            text = "\n\n".join(p for p in paras if len(p) > 40)
            if len(text) > 600:
                return f"Source page: {r.url}\n\n{text[:30000]}"
    except Exception:
        pass
    return None


# ============================================================
# AIRTABLE
# ============================================================
def _at_headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


@st.cache_data(ttl=300, show_spinner=False)
def existing_source_urls():
    """Source URLs already filed in Stories, so the desk can flag repeats."""
    if not AIRTABLE_TOKEN:
        return set()
    urls, offset = set(), None
    endpoint = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    try:
        while True:
            params = {"fields[]": COL_SOURCE_URL, "pageSize": 100}
            if offset:
                params["offset"] = offset
            r = requests.get(endpoint, headers=_at_headers(), params=params, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            for rec in data.get("records", []):
                u = rec.get("fields", {}).get(COL_SOURCE_URL)
                if u:
                    urls.add(u)
            offset = data.get("offset")
            if not offset:
                break
    except Exception:
        pass
    return urls


def create_story(fields):
    endpoint = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE}"
    payload = {"records": [{"fields": {k: v for k, v in fields.items() if v not in (None, "")}}],
               "typecast": True}
    r = requests.post(endpoint, headers=_at_headers(), json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Airtable {r.status_code}: {r.text[:300]}")
    return r.json()["records"][0]["id"]


# ============================================================
# DRAFTING
# ============================================================
def call_llm(system, user, max_tokens):
    if LLM_PROVIDER == "anthropic":
        from anthropic import Anthropic
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content


def word_count(text):
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # count link text, not URLs
    return len(re.findall(r"\b[\w'’-]+\b", body))


def parse_draft(raw, fallback_title):
    headline = fallback_title
    body = raw.strip()
    m = re.match(r"^\s*HEADLINE:\s*(.+?)\s*\n", body, flags=re.IGNORECASE)
    if m:
        headline = m.group(1).strip().strip('"*# ')
        body = body[m.end():].strip()
    return headline, body


def draft_story(article, fmt, beat, note):
    spec = FORMATS[fmt]
    text = get_article_text(article["link"])
    mode = "full" if text else "summary"
    source_block = text if text else (
        f"(Full article could not be retrieved. Work only from this summary and flag "
        f"anything that needs confirming.)\n\nSUMMARY: {article['summary']}"
    )
    system = (
        "You are a senior writer at Zinstrel drafting a first version for the editor, "
        "Marcus Lawrence, to revise.\n\n" + HOUSE_RULES
    )
    user = f"""FORMAT: {fmt}
FORMAT GUIDANCE: {spec['guide']}
TARGET LENGTH: about {spec['words']} words.
BEAT: {beat}
EDITOR'S NOTE: {note or '(none)'}

SOURCE HEADLINE: {article['title']}
SOURCE OUTLET: {article['outlet'] or 'unknown'}
SOURCE LINK: {article['link']}

SOURCE MATERIAL:
{source_block}

Return the draft in exactly this shape:
HEADLINE: <headline>
<blank line>
<body in markdown prose>

After the body, add a line "---" and then a short "Editor flags:" paragraph noting anything unconfirmed, any claim that needs a primary-source link, and any quote you could not verify in the source. Write nothing else."""
    max_tokens = max(800, int(spec["words"] * 3))
    raw = call_llm(system, user, max_tokens)
    headline, body = parse_draft(raw, article["title"])
    main_body = body.split("\n---", 1)[0]
    wc = word_count(main_body)
    return headline, f"{body}\n\nWord count: {wc}", wc, mode


# ============================================================
# STYLE
# ============================================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
h1, h2, h3 { font-family: 'Space Grotesk', sans-serif !important; letter-spacing: -0.01em; }
.zn-card { border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 12px 14px;
           margin-bottom: 4px; background: rgba(255,255,255,0.02); }
.zn-card.sel { border-color: #FF3DA5; box-shadow: 0 0 0 1px #FF3DA5 inset; }
.zn-title a { color: inherit; text-decoration: none; font-weight: 600; font-size: 1.02rem; }
.zn-title a:hover { color: #3DE0FF; }
.zn-meta { font-size: 0.8rem; opacity: 0.65; margin-top: 3px; }
.zn-tag { display: inline-block; font-size: 0.7rem; padding: 1px 7px; border-radius: 99px;
          background: rgba(61,224,255,0.12); color: #3DE0FF; margin-left: 6px; }
.zn-filed { background: rgba(255,200,61,0.15); color: #FFC83D; }
.zn-sum { font-size: 0.88rem; opacity: 0.8; margin-top: 6px; }
</style>
""", unsafe_allow_html=True)


# ============================================================
# UI
# ============================================================
def art_id(a):
    return hashlib.md5(a["link"].encode()).hexdigest()[:12]


st.title("📡 Zinstrel News Desk")
st.caption("Scan AI music news by beat → pick stories → draft in house format → file to Stories in AirMarcus.")

missing = []
if not AIRTABLE_TOKEN:
    missing.append("AIRTABLE_TOKEN")
if LLM_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
    missing.append("ANTHROPIC_API_KEY")
if LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
    missing.append("OPENAI_API_KEY")
if missing:
    st.warning(f"Missing secrets: {', '.join(missing)}. Browsing works; drafting/filing won't until they're set.")

with st.sidebar:
    st.header("Scan")
    days_back = st.slider("Days back", 1, 14, 3)
    per_beat = st.slider("Max stories per beat", 5, 40, 20)
    chosen_beats = st.multiselect("Beats", list(BEATS), default=list(BEATS))
    include_trades = st.checkbox("Include trade-press sweep", value=True)
    if st.button("🔄 Refresh news", use_container_width=True):
        fetch_all.clear()
        existing_source_urls.clear()
    st.divider()
    target_date = st.date_input("Target issue date (optional)", value=None)
    st.caption(f"Drafting with: {LLM_PROVIDER} · "
               f"{ANTHROPIC_MODEL if LLM_PROVIDER == 'anthropic' else OPENAI_MODEL}")

if not chosen_beats and not include_trades:
    st.info("Pick at least one beat in the sidebar.")
    st.stop()

with st.spinner("Scanning AI music news…"):
    news = fetch_all(tuple(chosen_beats), days_back, per_beat, include_trades)
filed = existing_source_urls()

tab_names = [b for b in news if news[b]]
if not tab_names:
    st.info("No stories found for these settings. Try more days back.")
    st.stop()

tabs = st.tabs([f"{b} ({len(news[b])})" for b in tab_names])
lookup = {}
for tab, beat in zip(tabs, tab_names):
    with tab:
        for a in news[beat]:
            aid = art_id(a)
            lookup[aid] = (a, beat)
            selected = st.session_state.get(f"sel_{aid}", False)
            date_str = a["published"].astimezone().strftime("%b %d, %H:%M") if a["published"] else ""
            filed_tag = '<span class="zn-tag zn-filed">already in Stories</span>' if a["link"] in filed else ""
            c1, c2 = st.columns([0.05, 0.95])
            with c1:
                st.checkbox(" ", key=f"sel_{aid}", label_visibility="collapsed")
            with c2:
                st.markdown(
                    f'<div class="zn-card {"sel" if selected else ""}">'
                    f'<div class="zn-title"><a href="{a["link"]}" target="_blank">{a["title"]}</a></div>'
                    f'<div class="zn-meta">{a["outlet"] or "Unknown outlet"} · {date_str}'
                    f'<span class="zn-tag">{a["keyword"]}</span>{filed_tag}</div>'
                    f'</div>', unsafe_allow_html=True,
                )
                if selected:
                    o1, o2 = st.columns(2)
                    default_beat = beat if beat in BEATS else list(BEATS)[0]
                    o1.selectbox("Format", list(FORMATS), key=f"fmt_{aid}")
                    o2.selectbox("Beat", list(BEATS), index=list(BEATS).index(default_beat), key=f"beat_{aid}")
                    st.text_input("Editor's note (angle, what to emphasize)", key=f"note_{aid}")

selected_ids = [aid for aid in lookup if st.session_state.get(f"sel_{aid}")]

with st.sidebar:
    st.divider()
    st.header("File")
    st.write(f"**{len(selected_ids)}** selected")
    go = st.button("✍️ Draft & send to Stories", type="primary",
                   use_container_width=True, disabled=not selected_ids or bool(missing))

if go:
    st.divider()
    st.subheader("Filing")
    progress = st.progress(0.0)
    for i, aid in enumerate(selected_ids, start=1):
        a, _ = lookup[aid]
        fmt = st.session_state.get(f"fmt_{aid}", list(FORMATS)[0])
        beat = st.session_state.get(f"beat_{aid}", list(BEATS)[0])
        note = st.session_state.get(f"note_{aid}", "")
        status = st.empty()
        try:
            if FORMATS[fmt]["words"] == 0:
                headline, draft, notes_extra = a["title"], "", "Pitched from news desk (no draft)."
            else:
                status.info(f"Drafting **{a['title']}** as {fmt}…")
                headline, draft, wc, mode = draft_story(a, fmt, beat, note)
                notes_extra = (f"Drafted by news desk as {fmt} · {wc} words · "
                               f"{'from full article' if mode == 'full' else 'from summary only (verify)'}")
            notes = "\n".join(x for x in [f"Editor's note: {note}" if note else "", notes_extra] if x)
            rec_id = create_story({
                COL_TITLE: headline,
                COL_TYPE: FORMATS[fmt]["type"],
                COL_STATUS: NEW_STATUS,
                COL_BEAT: beat,
                COL_SOURCE_URL: a["link"],
                COL_SOURCE_OUTLET: a["outlet"],
                COL_DRAFT: draft,
                COL_NOTES: notes,
                COL_PUB_DATE: target_date.isoformat() if target_date else None,
            })
            status.success(f"Filed **{headline}** → Stories ({rec_id})")
            if draft:
                with st.expander(f"Preview: {headline}"):
                    st.markdown(draft)
        except Exception as e:
            status.error(f"Couldn't file **{a['title']}**: {e}")
        progress.progress(i / len(selected_ids))
    existing_source_urls.clear()
    st.success("Done. New stories are in Stories at Concept Stage.")
