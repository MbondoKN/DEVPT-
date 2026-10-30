# Job scout

Runs every day at 08:00 Nairobi time, searches for civil engineering and international development roles, and rebuilds a table (`docs/index.html`, `docs/jobs.csv`).

## Set up (about 10 minutes)

1. Create a GitHub repo and upload this folder.
2. Repo > Settings > Secrets and variables > Actions, add:
   - `ANTHROPIC_API_KEY` (from console.anthropic.com). Powers the web-search agent.
   - `RELIEFWEB_APPNAME` (optional, free: register an app name at apidoc.reliefweb.int).
3. Settings > Pages > Deploy from branch > `main` / `/docs`. Your table lives at the URL GitHub shows.
4. Actions tab > Daily job scout > Run workflow, to test it without waiting for tomorrow.

Pages on a private repo needs a paid GitHub plan. On a free plan, use a public repo, or just download `docs/jobs.csv` / open `docs/index.html` from the repo.

## Tuning

Everything is in `config.json`: keyword lists, the search topics the agent runs, RSS feeds, how many days an unseen job stays listed (`stale_days`), and the model.

## Run locally

    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=...   # optional sources are skipped if unset
    python scout.py                # real run
    python scout.py --demo         # preview the page with sample rows
