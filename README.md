# Zinstrel News Desk

**Scan → Wire → Queue → Draft**, for Zinstrel's AI music coverage.

1. **Scan now** (sidebar) searches Google News across AI music beats and the trade press, and reads **The AI Musicpreneur** and the **AI Music Newsletter** directly from their sites (see `NEWSLETTERS` to add more). It finds each publisher's own link, reads the article, and has Claude write a one-line summary. Each story also gets an on-topic check, so non-music AI stories are hidden by default. Results save to the **Wire** table in AirMarcus.
2. **Wire** tab: browse by beat. Check stories, then **Queue selected** or **Skip selected**.
3. **Queue** tab: pick **Major Signal**, **Minor Signal**, or **News Brief** for each story you want written, and add an optional editor's note. If an article couldn't be read (a paywall), paste its text into the box. Then click **Draft**.
4. Claude **reports each story out** before writing. It treats the wire item as a tip, uses web search and fetch to find the primary material (the company's announcement, press release, filing, bill text, or original statement), and writes from that, with links to the primary sources rather than "according to [outlet]." Quotes come only from primary material. Each draft comes with reporting notes: editor flags, suggested follow-up interviews, and the primary sources used. Then Claude drafts in house style. The draft goes to the **Lovable CMS** as an unpublished draft. If Lovable isn't configured or rejects it, the draft goes to **Stories** in Airtable instead. Editor flags and word count show in the desk; they stay out of the CMS body.
5. **Drafted** tab: links to everything drafted.

Unused wire items (New or Skipped) older than 10 days are cleared automatically on each scan, so the base doesn't fill up.

## News only
Every scanned item must pass a news check before it reaches the Wire. It has to be about AI and music, and it has to report a specific, recent development. Job listings, company and about pages, product and pricing pages, evergreen explainers, tool roundups, and pages whose own publish date is old are dropped. **Re-check the wire** in the sidebar runs the same check over what's already there.

## Story ideas
The **Ideas** tab suggests stories in Zinstrel's sphere. It draws on everything published on zinstrel.com and Substack (from the site's social feed), the timely stories on the Wire, Google's daily trending searches that touch AI or music, and, when connected, Google Analytics (top pages) and Search Console (the searches that bring readers, plus queries where Zinstrel ranks low). Claude checks for new developments with web search, then pitches each idea with a why-now, a suggested format, a search target, and primary sources to start from. Queue an idea just like a wire story.

Google Trends has no public API, so the desk uses Google's daily trending-searches feed. For keyword-level search data, connect Search Console.

### Connecting Google Analytics and Search Console (optional)
1. In Google Cloud, create a service account (or reuse one) and create a JSON key for it.
2. Enable the **Google Analytics Data API** and the **Google Search Console API** for that project.
3. In GA4, go to Admin → Property access management and add the service account's email as a **Viewer**. Copy the property ID from Admin → Property details.
4. In Search Console, go to Settings → Users and permissions and add the service account's email (Restricted is enough).
5. In Streamlit secrets, add `GA4_PROPERTY_ID`, `GSC_SITE_URL` (for example `sc-domain:zinstrel.com`), and a `[gcp_service_account]` block with the JSON key's fields, at the end of the secrets.

## Links per story
In the Queue, each story has **Links to include**, for up to 5 links. They save as you type. The draft reads each one, draws on it where relevant, and links to it where it supports a point. If a link isn't usable, the editor flags say why. Leave them blank and Claude finds its own sources.

## Summary
Each draft gets a fresh 1–2 sentence summary. If it repeats the headline or reuses any six-word run from the article, it's rewritten automatically. The summary goes to Lovable's `summary` field and leads the reporting notes.

## Formats
- **Major Signal:** about 300 words; the headline doubles as the email subject.
- **Minor Signal:** a simple header and 2–3 sentences.
- **News Brief:** 300 words minimum, up to about 600 if the story supports it.

## Secrets (Streamlit → Manage app → Settings → Secrets)
See `.streamlit/secrets.toml.example`. Required: `APP_PASSWORD`, `AIRTABLE_TOKEN`, `ANTHROPIC_API_KEY`. For Lovable drafts, also set `LOVABLE_DRAFTS_URL` and `LOVABLE_DRAFTS_KEY`.

## Tuning
Beats and keywords live in `BEATS`, trade sites in `TRADE_SITES`, and format guidance in `FORMATS` and `HOUSE_RULES`, all near the top of `zinstrel_news_desk.py`.
