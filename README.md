# Prediction Market Bot

Finds edge opportunities on Polymarket using two strategies:

1. **Soccer** — Pinnacle sharp lines vs Polymarket (low-volume markets only)
2. **Weather** — Open-Meteo ECMWF forecasts vs Polymarket weather markets

All trades are paper trades logged to `trades.json`. Nothing is executed on Polymarket.

---

## Repo Structure

```
├── run_bot.py            ← main runner (this is what GitHub Actions calls)
├── polymarket.py         ← fetches and parses Polymarket markets
├── odds_api.py           ← fetches Pinnacle lines via The Odds API
├── weather_scanner.py    ← compares Open-Meteo forecasts to weather markets
├── trades.json           ← paper trade log (auto-updated by GitHub Actions)
├── market_state.json     ← tracks seen market IDs
├── .gitignore
└── .github/
    └── workflows/
        └── bot.yml       ← GitHub Actions workflow (runs every 5 min)
```

---

## Setup — Step by Step

### 1. Create a new GitHub repo
Create a new empty repo on GitHub.

### 2. Upload all files
Upload these files to the **root** of your repo:
- `run_bot.py`
- `polymarket.py`
- `odds_api.py`
- `weather_scanner.py`
- `trades.json`
- `market_state.json`
- `.gitignore`

### 3. Create the workflow folder and upload the workflow
This is the most important step — GitHub Actions only works if the workflow file is in exactly this path:

```
.github/workflows/bot.yml
```

To create this on GitHub:
1. Click **Add file → Create new file**
2. In the filename field type: `.github/workflows/bot.yml`
3. As you type the `/` characters, GitHub creates the folders automatically
4. Paste the contents of `bot.yml` into the editor
5. Click **Commit changes**

### 4. Get your free Odds API key
1. Go to https://the-odds-api.com
2. Sign up free — takes 30 seconds
3. Copy your API key

### 5. Add the API key as a GitHub Secret
1. Go to your repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Name: `ODDS_API_KEY`
4. Value: paste your key
5. Click **Add secret**

### 6. Create the GitHub environment
The workflow uses an environment called `sports` to access secrets:
1. Go to **Settings** → **Environments**
2. Click **New environment**
3. Name it `sports`
4. Click **Configure environment**
5. Under "Environment secrets" add `ODDS_API_KEY` again here too

### 7. Make the repo public (recommended)
Public repos get unlimited GitHub Actions minutes.
Settings → scroll to bottom → Change visibility → Public

---

## Running the Bot

### First run (dry run — recommended)
1. Go to **Actions** tab in your repo
2. Click **Prediction Market Bot** in the left sidebar
3. Click **Run workflow** (top right)
4. Leave "Dry run" as `true`
5. Click the green **Run workflow** button

This shows you what opportunities the bot finds without logging any trades.

### Go live
Same as above but change "Dry run" to `false`.

After a live run, `trades.json` is automatically committed back to your repo with the paper trades logged.

### Automatic runs
Once set up, the bot runs automatically every 5 minutes between 10:00-23:00 UTC with no action needed from you.

---

## Reading the output

In the Actions log you'll see:

```
── Soccer: Polymarket vs Pinnacle ───────────────────────────
  162 markets parsed → 12 under $8,000
  [soccer_epl] quota: 8 used, 492 remaining
  ...
   Edge     Bet   Poly%    Pin%      Vol  Match
  -------------------------------------------------------
   7.2%  €4.50   25.5%   32.7%   $3,100  Freiburg vs Union Berlin  [Bundesliga]

── Weather: Open-Meteo vs Polymarket ────────────────────────
  23 weather markets found
   Edge     Bet   Poly%   Mdl%   Days   Fcst°C  Market
  -------------------------------------------------------
   9.1%  €5.20   38.0%  47.1%      4    18.3°  Will London exceed 17°C on...
```

---

## Viewing P&L

Download `trades.json` from your repo and upload it to `dashboard.html` (from the old repo) to see your full P&L dashboard.

---

## API Quotas

| API | Limit | Usage |
|-----|-------|-------|
| Polymarket Gamma | Unlimited | ~300 req/run |
| The Odds API | 500 req/month free | Only called when low-vol markets found |
| Open-Meteo | Unlimited | ~5-20 req/run when weather markets exist |

The bot is designed to preserve The Odds API quota — it only calls the Odds API when low-volume soccer markets are detected on Polymarket first.
