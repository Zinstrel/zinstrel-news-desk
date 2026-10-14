# Zinstrel News Desk

**Scan → Wire → Queue → Draft**, for Zinstrel's AI music coverage.

1. **Scan now** (sidebar) searches Google News across AI music beats and the trade press, and reads **The AI Musicpreneur** and the **AI Music Newsletter** directly from their sites (see `NEWSLETTERS` to add more). It finds each publisher's own link, reads the article, and has Claude write a one-line summary. Each story also gets an on-topic check, so non-music AI stories are hidden by default. Results save to the **Wire** table in AirMarcus.
2. **Wire** tab: browse by beat. Check stories, then **Queue selected** or **Skip selected**.
3. **Queue** tab: pick **Major Signal**, **Minor Signal**, or **News Brief** for each story you want written, and add an optional editor's note. If an article couldn't be read (a paywall), paste its text into the box. Then click **Draft**.
4. Claude **reports each story out** before writing. It treats the wire item as a tip, uses web search and fetch to find the primary material (the company's announcement, press release, filing, bill text, or original statement), and writes from that, with links to the primary sources rather than "according to [outlet]." Quotes come only from primary material. Each draft comes with reporting notes: editor flags, suggested follow-up interviews, and the primary sources used. Then Claude drafts in house style. The draft goes to the **Lovable CMS** as an unpublished draft. If Lovable isn't configured or rejects it, the draft goes to **Stories** in Airtable instead. Editor flags and word count show in the desk; they stay out of the CMS body.
5. **Drafted** tab: links to everything drafted.

Unused wire items (New or Skipped) older than 10 days are cleared automatically on each scan, so the base doesn't fill up.

## Formats
- **Major Signal:** about 300 words; the headline doubles as the email subject.
- **Minor Signal:** a simple header and 2–3 sentences.
- **News Brief:** 300 words minimum, up to about 600 if the story supports it.

## Secrets (Streamlit → Manage app → Settings → Secrets)
See `.streamlit/secrets.toml.example`. Required: `APP_PASSWORD`, `AIRTABLE_TOKEN`, `ANTHROPIC_API_KEY`. For Lovable drafts, also set `LOVABLE_DRAFTS_URL` and `LOVABLE_DRAFTS_KEY`.

## Tuning
Beats and keywords live in `BEATS`, trade sites in `TRADE_SITES`, and format guidance in `FORMATS` and `HOUSE_RULES`, all near the top of `zinstrel_news_desk.py`.
