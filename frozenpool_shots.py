# frozenpool_shots.py
"""
Downloads the Shots report (SOG 0-15 / 16-30 / 31-45 by player) from
frozenpool.dobbersports.com and writes it to your Google Sheet:

  'ShotsCur'    2026-27 regular season (refreshed about once a day)
  'ShotsPrior'  2025-26 regular season (refreshed about once a week)

The NHL Odds script reads those two tabs for the shot-quality adjustment.

Usage:
  python frozenpool_shots.py                      # scrape what is due, write to the sheet
  python frozenpool_shots.py --force              # ignore the "scraped recently" pause
  python frozenpool_shots.py --parse file.csv     # test the CSV reader on a downloaded file

Needs: pip install playwright gspread google-auth
"""
import os
import re
import csv
import sys
import argparse
from datetime import datetime, timezone, timedelta

URL = "https://frozenpool.dobbersports.com/frozenpool_report.php"
REPORT_NAME = "Shots"
REQUIRED = ["Name", "Pos", "Team", "GP", "SOG", "SOG 0-15", "SOG 16-30"]

SERVICE_ACCOUNT_FILE = os.environ.get(
    "SERVICE_ACCOUNT_FILE", r"C:\Users\seane\OneDrive\gcp\nst-scraper-key.json")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "PASTE_THE_ID_OF_THE_ODDS_WORKBOOK_HERE")
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", ".")
TZ_ID = "Etc/GMT+6"

CUR_MIN_GAP_HOURS = float(os.environ.get("SHOTS_CUR_GAP_HOURS", "20"))
PRIOR_MIN_GAP_HOURS = float(os.environ.get("SHOTS_PRIOR_GAP_HOURS", str(24 * 7)))
META_CELL = "Z2"            # last good scrape (UTC); Z1 holds its label

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def season_labels():
    """('2026-2027 Regular Season', '2025-2026 Regular Season') for the current date."""
    now = datetime.now(timezone.utc) + timedelta(hours=-6)
    y = now.year if now.month >= 9 else now.year - 1
    return (f"{y}-{y + 1} Regular Season", f"{y - 1}-{y} Regular Season")


# ---------- CSV reading ----------
def read_csv_rows(path):
    """Return [header] + data rows, with numbers kept as text for the sheet to parse."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError("The CSV is empty.")
    missing = [c for c in REQUIRED if c not in rows[0]]
    if missing:
        raise ValueError("The CSV is missing columns: " + ", ".join(missing))
    return rows


def to_numbers(rows):
    """Convert numeric-looking cells to numbers so Sheets stores them as numbers."""
    out = [rows[0]]
    for r in rows[1:]:
        new = []
        for cell in r:
            c = cell.strip()
            if re.fullmatch(r"-?\d+", c):
                new.append(int(c))
            elif re.fullmatch(r"-?\d*\.\d+", c):
                new.append(float(c))
            else:
                new.append(c)
        out.append(new)
    return out


# ---------- Browser ----------
def download_csv(season_label, dest):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, timezone_id=TZ_ID, locale="en-US",
                                  viewport={"width": 1366, "height": 1600},
                                  accept_downloads=True)
        page = ctx.new_page()
        try:
            page.goto(URL, timeout=60000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                pass

            # Settings: Skaters, any team, minimum GP 0, chosen season.
            page.locator("select").filter(has_text="Regular Season").first.select_option(label=season_label)
            try:
                page.get_by_label("Minimum GP").fill("0")
            except Exception:
                pass

            # Pick the Shots report.
            page.locator(f":text-is('{REPORT_NAME}')").first.click()
            page.wait_for_selector("table#reportdata tbody tr, table tbody tr", timeout=30000)
            page.wait_for_timeout(1500)

            # Show every row so the download is the full list.
            try:
                page.locator("select[name='reportdata_length']").select_option(label="All")
            except Exception:
                pass

            header = page.locator("table thead").first.inner_text()
            if "SOG 0-15" not in header:
                raise RuntimeError("The table that appeared has no 'SOG 0-15' column. "
                                   "The Shots button or its columns may have changed.")

            with page.expect_download(timeout=30000) as dl:
                page.get_by_text("Download CSV File").first.click()
            dl.value.save_as(dest)
        except Exception:
            try:
                page.screenshot(path=os.path.join(ARTIFACT_DIR, "shots_failed.png"), full_page=True)
            except Exception:
                pass
            raise
        finally:
            browser.close()


# ---------- Google Sheets ----------
def open_book():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(GOOGLE_SHEET_ID)


def get_tab(sh, title):
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=1000, cols=30)


def due(ws, gap_hours):
    try:
        raw = ws.acell(META_CELL).value
        if not raw or ws.row_count < 2 or not ws.acell("A2").value:
            return True
        last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last >= timedelta(hours=gap_hours)
    except Exception:
        return True


def write_tab(ws, rows):
    ws.clear()
    ws.update(values=rows, range_name="A1")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ws.update(values=[["last_scrape_utc"], [stamp]], range_name="Z1:Z2")


def main():
    ap = argparse.ArgumentParser(description="Frozen Pool shot-location report -> Google Sheets.")
    ap.add_argument("--parse", metavar="CSV", help="Read a downloaded CSV and print a summary")
    ap.add_argument("--force", action="store_true", help="Ignore the 'scraped recently' pause")
    args = ap.parse_args()

    if args.parse:
        rows = read_csv_rows(args.parse)
        print(f"{len(rows) - 1} players; columns: {rows[0]}")
        for r in rows[1:4]:
            print(r)
        return

    cur_label, prior_label = season_labels()
    sh = open_book()
    jobs = [("ShotsCur", cur_label, CUR_MIN_GAP_HOURS), ("ShotsPrior", prior_label, PRIOR_MIN_GAP_HOURS)]
    failed = False
    for tab, label, gap in jobs:
        ws = get_tab(sh, tab)
        if not args.force and not due(ws, gap):
            print(f"{tab}: scraped recently, skipping.")
            continue
        dest = os.path.join(ARTIFACT_DIR, f"{tab}.csv")
        try:
            download_csv(label, dest)
            rows = to_numbers(read_csv_rows(dest))
            if len(rows) < 2 and tab == "ShotsPrior":
                raise RuntimeError("Prior-season download had no players.")
            write_tab(ws, rows)
            print(f"{tab}: wrote {len(rows) - 1} players ({label}).")
        except Exception as e:
            failed = True
            print(f"{tab}: FAILED - {e}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
