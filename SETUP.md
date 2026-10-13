# Paydirt setup guide (free version)

Everything here is free: GitHub hosts the site and runs the daily data job, nflverse supplies the stats, the National Weather Service supplies forecasts, and The Odds API's free plan supplies betting lines.

## What's in this folder

| File | Where it goes in your repo | What it does |
|---|---|---|
| `index.html` | top level | The website. Loads `data.json`; shows sample data if that file isn't there yet. |
| `scripts/build_data.py` | `scripts/` folder | Pulls stats, schedules, odds, and weather, runs the numbers, writes `data.json`. |
| `.github/workflows/update.yml` | `.github/workflows/` folder | Runs the script every morning and republishes the site. |

## Setup (about 15 minutes)

### 1. Create a GitHub account and repository
1. Sign up at github.com (free).
2. Click **+** (top right) > **New repository**.
3. Name it `paydirt`, choose **Public** (free GitHub Pages hosting requires a public repo), tick **Add a README file**, and click **Create repository**.

### 2. Upload the site and script
1. In the repo, click **Add file > Upload files**.
2. Drag in `index.html` and the whole `scripts` folder.
3. Click **Commit changes**.

### 3. Add the automation file
Hidden folders like `.github` often don't upload by drag and drop, so create this one by hand:
1. Click **Add file > Create new file**.
2. In the name box type exactly: `.github/workflows/update.yml` (typing the slashes creates the folders).
3. Paste in the full contents of `update.yml`, then click **Commit changes**.

### 4. Get your free Odds API key
1. Go to the-odds-api.com and choose the free **Starter** plan.
2. Your key arrives by email.

### 5. Store the key as a secret
Never paste the key into `index.html`, since anyone could see it there.
1. In the repo: **Settings > Secrets and variables > Actions > New repository secret**.
2. Name: `ODDS_API_KEY`. Secret: paste your key. Click **Add secret**.

### 6. Turn on GitHub Pages
1. **Settings > Pages**.
2. Under **Build and deployment > Source**, choose **GitHub Actions**.

### 7. Run it the first time
1. Open the **Actions** tab. If asked, click the button to enable workflows.
2. Click **Update data and publish site** on the left, then **Run workflow > Run workflow**.
3. Wait 3 to 6 minutes for a green check mark.

### 8. Visit your site
`https://YOUR-GITHUB-USERNAME.github.io/paydirt/`

The slate should say "Live data, updated..." under the heading. If it still says "Prototype running on sample data," see Troubleshooting.

## What happens automatically

- **Every morning at 10 AM Eastern:** fresh stats, injuries, weather, moneylines, spreads, and totals. The site republishes itself.
- **Saturdays:** player prop lines (anytime TD, passing, rushing, receiving yards, receptions) are pulled once for that week's remaining games.
- **Whenever you edit `index.html`:** the site republishes.

## Free credit budget (500 per month)

| Job | Credits |
|---|---|
| Daily game lines (3 per run) | about 90 a month |
| Saturday props (about 5 per game) | about 70 to 80 a week |
| **Typical month** | **about 400 to 450** |

The script checks your remaining credits before each prop pull and stops early rather than run out. Each manual run uses 3 more credits, and ticking "force props" uses about 5 per game, so use those sparingly. Thursday night games are already played by Saturday, so they won't get prop lines on the free plan. Upgrading to the $30 plan would allow daily prop pulls.

## Troubleshooting

- **Site still shows sample data:** open the Actions tab, click the latest run, and open the **Build data.json** step. Copy any red error text and send it to Claude.
- **Red X on "deploy":** make sure step 6 is set to **GitHub Actions**, not "Deploy from a branch."
- **Weather says "Forecast pending":** the National Weather Service forecasts about 7 days out, so early in the week some games won't have one yet.
- **No moneylines on game pages:** check the secret name is exactly `ODDS_API_KEY`.
- **Early season (weeks 1 and 2):** the script blends in last season's stats until there's enough new data.

## Once a season

Check the `STADIUMS` list near the top of `scripts/build_data.py` if a team moves into a new stadium or changes its roof.
