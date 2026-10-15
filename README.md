# Faisal Stock Radar

Arabic RTL Flask app for screening US equities using Massive API, evaluating support/accumulation evidence, and maintaining watchlists.

## Features included
- Initial screener defaults: Exchange Any, price under $5, market cap under $300M, float under 50M, average volume over 100K, relative volume over 1, and monthly performance down.
- Ticker detail page with a PASS / FAIL / UNKNOWN checklist.
- Support/resistance calculations, RSI, MACD, moving averages, volatility contraction, higher lows, selling pressure, former-runner history.
- Separate watchlist and ready list, persistent SQLite data, and evaluation history.
- Admin-protected settings page to register future data sources and see source configuration.
- Massive API connection test, health endpoint, error handling, Render deployment blueprint.

## Important data limitations
The app does not invent market data. Massive plan and endpoint access vary. If market cap or public float cannot be retrieved from the configured endpoint, the checklist shows UNKNOWN. Reverse splits, offerings/dilution, news, and short-available data are explicitly UNKNOWN until a real provider adapter is implemented. Registering a provider in settings does not implement its adapter automatically.
The initial scan is bounded by the selected number of symbols returned by the reference endpoint; it is not a guaranteed full-market scan. A successful API connection does not guarantee access to every field.

## Run locally
Python 3.10+ recommended.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
export MASSIVE_API_KEY="YOUR_KEY"
export FLASK_SECRET_KEY="a-long-random-secret"
export RADAR_ADMIN_PASSWORD="choose-a-strong-password"
python app.py
```

Open http://127.0.0.1:5000

On Windows PowerShell, use `$env:MASSIVE_API_KEY="..."` and equivalent commands for the other variables.

## Deploy on Render
1. Upload these files to your GitHub repository and commit them.
2. In Render choose **New → Blueprint** and select the repository containing `render.yaml`, or create a Web Service with:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
3. Set `MASSIVE_API_KEY` to your real Massive key in Render Environment.
4. Set a strong `RADAR_ADMIN_PASSWORD`.
5. Ensure `FLASK_SECRET_KEY` is a long random value. The Blueprint generates one if created through Render.
6. Use persistent disk at `/var/data` for `DATABASE_PATH=/var/data/radar.db`. Persistent disks may require a paid Render plan depending on current Render offering; without persistent storage, SQLite data can be lost on redeploy/restart.
7. Deploy and verify `/health`, the Massive connection indicator, and one ticker search.

## Security
- Never commit API keys or passwords.
- API credentials are read from server-side environment variables only.
- The settings page requires `RADAR_ADMIN_PASSWORD` and a login session.
- The main screener is not access-controlled in this version. If you later want a fully private site, add authentication to all pages and APIs, not only the settings page.
- Configure HTTPS through Render and keep secrets in Render Environment, not source files.

## Future data sources
Settings lets an administrator register source metadata and a credential environment-variable name. New sources show `NOT_IMPLEMENTED` until a provider adapter is actually coded and tested. Do not treat an entry in the registry as a working data connection.
