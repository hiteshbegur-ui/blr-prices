# Bengaluru Fruit & Veg Price Comparison (Zepto vs Blinkit vs Instamart)

A web page that shows Zepto, Blinkit and Instamart prices side by side, refreshed every 2 hours.
Works on any phone or laptop browser, no install needed.

## What is in this folder

| Path | What it does |
|---|---|
| `docs/index.html` | The web page: search, sort (click any column header), filters, cheapest price highlighted |
| `docs/data/prices.json` | The data the page reads. Currently **sample data**; the collector overwrites it |
| `scraper/scrape.py` | Opens each site in a headless browser (Bengaluru pincode), reads fruit & veg prices, merges them |
| `.github/workflows/update.yml` | Runs the collector every 2 hours and saves the new prices |

## Set up (about 10 minutes, free)

1. Create a free account at github.com and a new repository (e.g. `blr-prices`).
2. Upload all files from this folder to it, keeping the folder structure.
3. Repo **Settings > Pages**: Source = "Deploy from a branch", Branch = `main`, Folder = `/docs`. Save.
   Your page will be live at `https://<your-username>.github.io/blr-prices/`. Open that link on any device.
4. Repo **Settings > Actions > General > Workflow permissions**: choose "Read and write permissions". Save.
5. Repo **Actions** tab > "Update prices" > **Run workflow** to do the first collection now.
   After that it runs automatically every 2 hours.

## Important: first run will probably need tuning

I could not test the collector against the live sites from here (they block automated requests, and my environment has no internet access for the browser). The page, sorting, filters, matching and 2-hourly schedule are tested; the site-specific part (setting the delivery location and finding product tiles) is built generically and may need adjusting on first run.

If the first run reports 0 products for a platform:
- Open the failed run in the Actions tab and download the `debug` files (screenshots and page text from `scraper/debug/`).
- Share them with me and I'll adjust the selectors.

Known risks:
- **Blocking:** Blinkit actively blocks bots. GitHub's servers are in the US, and these services are India-only and may refuse them. If so, run the same script from a machine in India (home PC, a small Indian VPS or a Raspberry Pi) with a cron job `0 */2 * * *` and the same `git push` step.
- **Terms of use:** these sites restrict automated access in their terms. This is fine for personal price-checking at low frequency, but check before using it commercially.
- **Matching:** the same item is matched across sites by name (e.g. "Tomato Hybrid"). Odd names may show as separate rows; they can be merged by adding aliases.

## How the page behaves

- Competitor names are the column headers, with the time each was last collected.
- Prices show the pack size and price per kg/litre, and the cheapest is highlighted green (compared per kg where possible so 500 g and 1 kg packs are fair).
- If a platform fails on a run, its previous prices stay and a warning banner appears.
- If data is older than 3 hours, a banner warns that the collector may be failing.
- Change the pincode in `update.yml` (`PINCODE`) to any Bengaluru pincode.
