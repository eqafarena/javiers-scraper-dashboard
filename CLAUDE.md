# Javier's Review Scraper — Project Context

## What this is
Scrapes Google Maps reviews for Javier's Downtown (Detroit) every 6 hours via GitHub Actions. Matches review text against a list of staff names, then writes results to flat files that get committed back to the repo. `README.md` serves as a live dashboard on the repo homepage.

## Key files
| File | Purpose |
|---|---|
| `scraper.py` | Single entry point — scraping, name matching, output, dashboard |
| `key_names.txt` | Staff name variants to match. Format: `name_variant\|Display Name` one per line |
| `output.txt` | Full review text + mentions for the current lookback window (overwritten each run) |
| `README.md` | Markdown dashboard — mention counts + avg ratings per staff member (overwritten each run) |
| `log.txt` | Timestamped run log (overwritten each run) |
| `.github/workflows/scraper.yml` | GitHub Actions cron job — runs every 6 hours, commits output back |

## Configuration constants (top of scraper.py)
- `LOOKBACK_DAYS` — how many days back to include reviews (currently `7`)
- `FUZZY_THRESHOLD` — fuzzy match sensitivity for name matching, 0–100 (currently `80`)
- `SHORT_NAME_THRESHOLD` — names this length or shorter use exact word-boundary match instead of fuzzy (currently `4`)

## How the scraper works
1. Opens headless Chromium via Playwright, warms up on Google homepage, navigates to the Maps reviews URL
2. Tries ~8 CSS/role selectors to click the "Sort by newest" button; falls back to client-side date sort if all fail
3. Gets a bounding box on the reviews container and uses `page.mouse.wheel()` to scroll — `scrollTop +=` does NOT trigger Google Maps lazy loading
4. Parses cards on every scroll step and accumulates into a dict keyed by review ID (handles virtual DOM — Google Maps recycles cards out of the DOM as you scroll)
5. Stops early once 2 consecutive batches contain only reviews older than `LOOKBACK_DAYS`
6. Filters to recent reviews, writes `output.txt`, generates `README.md` dashboard, writes `log.txt`

## Important quirks / past decisions
- **`page.mouse.wheel()` not `scrollTop`** — Google Maps only lazy-loads more reviews in response to real wheel events, not DOM property changes. This was the root cause of the scraper always returning only 9 reviews.
- **Parse during scroll, not after** — Google Maps uses a virtual DOM; only currently visible cards exist in the DOM. Parsing after scrolling to the bottom only sees the last ~9 cards.
- **No database** — Supabase was removed intentionally. All data lives in flat files committed to this repo. This is a deliberate testing/simplicity choice.
- **`headless=True`** — must stay True for CI; headless=False was the original setting and breaks on any server environment.
- **Runner pinned to `ubuntu-22.04`** — `ubuntu-latest` is now 24.04 which renamed `libasound2` → `libasound2t64`. Playwright 1.44.0's `--with-deps` script doesn't handle this. Do not change to `ubuntu-latest` without upgrading Playwright first.
- **"See more" buttons** — expanded via a single `page.evaluate(querySelectorAll...forEach click)` JS call, not individual Playwright `.click()` calls. Per-element clicks block on network settle and caused multi-minute hangs with many reviews loaded.

## Running locally
```bash
# Activate venv (Windows)
venv\Scripts\activate

# Run
python scraper.py
```
Outputs `output.txt`, `README.md`, and `log.txt` in the project root.

## GitHub Actions
- Scheduled: every 6 hours (`0 */6 * * *` UTC)
- Can be triggered manually: Actions tab → Scrape Reviews → Run workflow
- After each run, commits `output.txt`, `README.md`, `log.txt` back to `main`
- Uses `secrets.GITHUB_TOKEN` (auto-provided, no setup needed)
- Repo needs `contents: write` permission — already set in the workflow file
