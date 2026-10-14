# Zinstrel News Desk

Scans Google News for AI music stories by beat, lets you pick the ones worth covering, drafts them in Zinstrel's house formats (Signals item, Major Signal, Small signal, Culture, Frame, Breaking News, Tips & Tools), and files them into the **Stories** table in the **AirMarcus** Airtable base at *Concept Stage*.

This is a separate tool from the BenefitKarma news desk: its own repo, its own Streamlit app, and its own keys.

## What lands in Stories

| Stories field | Filled with |
|---|---|
| Title/Topic | Drafted headline (or the source headline for pitch-only) |
| Type | The format you picked (Signals (Mon), Culture (Wed), Frame (Fri), etc.) |
| Status | Concept Stage |
| Beat | The beat you picked |
| Source URL / Source Outlet | The original story |
| Article Draft | The draft, ending with editor flags and the word count |
| Notes | Your editor's note, plus how the draft was made |
| Pub Date | Target issue date, if you set one in the sidebar |

Stories already filed (matched by Source URL) are tagged "already in Stories" so you don't draft the same thing twice.

## 1. Get your keys

1. **Airtable token:** In Airtable, go to Builder Hub → Personal access tokens → Create token. Name it "Zinstrel News Desk". Add the scopes `data.records:read` and `data.records:write`, and give it access to the **AirMarcus** base only. Copy the token (it starts with `pat`).
2. **Model key:** Use your Anthropic API key (the same kind your Zinstrel Workers use). An OpenAI key also works if you'd rather.

## 2. Run it on your Mac

From the folder containing these files:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Open `.streamlit/secrets.toml`, paste in your keys, then run:

```bash
streamlit run zinstrel_news_desk.py
```

## 3. Put it on Streamlit Cloud (same as the BK desk)

1. Create a new **private** repo on GitHub named `zinstrel-news-desk`. Leave it empty, with no README.
2. In the project folder:
   ```bash
   git init
   git add .
   git commit -m "Zinstrel news desk"
   git branch -M main
   git remote add origin https://github.com/marcustar7/zinstrel-news-desk.git
   git push -u origin main
   ```
   `.gitignore` keeps `secrets.toml` out of GitHub.
3. On share.streamlit.io, click **Create app**, pick the `zinstrel-news-desk` repo, branch `main`, and main file `zinstrel_news_desk.py`.
4. Under **Advanced settings → Secrets**, paste the contents of your `secrets.toml`. Deploy.

## Tuning

- **Beats and keywords** live in the `BEATS` dict near the top of the file. Each keyword is its own search, so add the platforms and names you track.
- **Trade-press sweep** searches "AI music" on the outlets in `TRADE_SITES`.
- **Format specs and house rules** are in `FORMATS` and `HOUSE_RULES`.
- **Model:** set `ANTHROPIC_MODEL` (or `LLM_PROVIDER = "openai"` with `OPENAI_MODEL`) in secrets.
