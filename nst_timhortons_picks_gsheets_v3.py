# nst_timhortons_picks_gsheets_v3.py
"""
Loads hockeychallengehelper.com in a real browser (Playwright), captures the
picks feed the page itself loads, and writes it to Google Sheets:

  - 'PicksJSON'!A1   the raw feed (the "NHL Odds" Apps Script reads this)
  - 'TH Picks' tab   a simple table: Pick, Date, Name, EV Line, PP Line,
                     Opponent, Unavailable

Usage:
  python nst_timhortons_picks_gsheets_v3.py
  python nst_timhortons_picks_gsheets_v3.py --probe https://goaliepost.com/

Needs: pip install playwright pandas gspread gspread-dataframe google-auth
"""
import os
import sys
import json
import argparse

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import gspread
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe

# ====== CONFIG ======
# In the cloud (GitHub Actions) these come from environment variables.
# On your PC you can leave the environment alone and edit the fallback values here.
SERVICE_ACCOUNT_FILE = os.environ.get(
    "SERVICE_ACCOUNT_FILE", r"C:\Users\seane\OneDrive\gcp\nst-scraper-key.json")
# Must be the workbook that holds the NHL Odds Apps Script (the long ID in its URL),
# and it must be shared with the service account as Editor.
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "PASTE_THE_ID_OF_THE_ODDS_WORKBOOK_HERE")
# =====================

URL_HOME = "https://hockeychallengehelper.com"
DEFAULT_SHEET = "TH Picks"
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", r"C:\Users\seane\OneDrive")   # screenshots / probe output

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Fields the odds script doesn't use; dropped so the JSON fits in one cell.
DROP_KEYS = {"linemates", "ppLinemates", "gameLogs"}
CELL_LIMIT = 49000   # Google Sheets allows 50,000 characters per cell


# ---------- Google Sheets helpers ----------
def gs_open():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(GOOGLE_SHEET_ID)


def get_or_add_ws(sh, title, rows=200, cols=20):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def write_picks_json(sh, raw):
    ws = get_or_add_ws(sh, "PicksJSON", rows=10, cols=5)
    ws.update_acell("A1", raw)


def write_df_to_tab(sh, title, df):
    ws = get_or_add_ws(sh, title)
    ws.clear()
    set_with_dataframe(ws, df, include_index=False, include_column_header=True, resize=True)


# ---------- Browser ----------
def open_page(p):
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(
        user_agent=UA,
        viewport={"width": 1366, "height": 900},
        java_script_enabled=True,
    )
    return browser, ctx.new_page()


def capture_picks():
    """Load the home page and return the parsed /api/picks response (or None)."""
    with sync_playwright() as p:
        browser, page = open_page(p)

        matches = []
        page.on("response", lambda r: matches.append(r) if "/api/picks" in r.url else None)

        page.goto(URL_HOME, timeout=60000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=60000)
        except PWTimeout:
            pass
        for _ in range(40):              # up to ~20 more seconds
            if matches:
                break
            page.wait_for_timeout(500)

        data = None
        for r in matches:
            if r.status == 200:
                try:
                    data = r.json()
                    break
                except Exception:
                    pass

        if data is None:
            try:
                page.screenshot(path=os.path.join(ARTIFACT_DIR, "thpicks_failed.png"))
            except Exception:
                pass
        browser.close()
        return data


def slim(data):
    for lst in data.get("playerLists", []):
        for pl in lst.get("players", []):
            for k in DROP_KEYS:
                pl.pop(k, None)
    return data


def picks_df(data):
    day = str(data.get("dateTimeAvailable", ""))[:10]
    rows = []
    for lst in data.get("playerLists", []):
        for pl in lst.get("players", []):
            name = pl.get("fullName") or f"{pl.get('firstName', '')} {pl.get('lastName', '')}".strip()
            rows.append({
                "Pick": f"Pick #{lst.get('id')}",
                "Date": day,
                "Name": name,
                "EV Line": pl.get("line", ""),
                "PP Line": pl.get("ppLine", ""),
                "Opponent": pl.get("opponentTeam", ""),
                "Unavailable": "Yes" if pl.get("unavailable") else "",
            })
    return pd.DataFrame(rows, columns=["Pick", "Date", "Name", "EV Line", "PP Line", "Opponent", "Unavailable"])


# ---------- Probe: list the background requests a page makes ----------
def probe(url):
    out_dir = os.path.join(ARTIFACT_DIR, "probe")
    os.makedirs(out_dir, exist_ok=True)
    with sync_playwright() as p:
        browser, page = open_page(p)

        hits = []
        page.on("response", lambda r: hits.append(r) if r.request.resource_type in ("xhr", "fetch") else None)

        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=60000)
        except PWTimeout:
            pass
        page.wait_for_timeout(5000)

        for i, r in enumerate(hits):
            ctype = r.headers.get("content-type", "")
            print(f"{i:02d}  {r.status}  {ctype[:28]:28}  {r.url}")
            try:
                body = r.text()
            except Exception:
                continue
            ext = "json" if "json" in ctype else "txt"
            with open(os.path.join(out_dir, f"{i:02d}.{ext}"), "w", encoding="utf-8") as f:
                f.write(body)
        browser.close()
    print(f"\nSaved response bodies to {out_dir}")


# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Capture the Hockey Challenge Helper picks feed -> Google Sheets.")
    ap.add_argument("--sheet", default=DEFAULT_SHEET, help=f"Table tab name (default: '{DEFAULT_SHEET}')")
    ap.add_argument("--probe", metavar="URL", help="Load a URL and list the background requests it makes")
    args = ap.parse_args()

    if args.probe:
        probe(args.probe)
        return

    print("Capturing today's picks feed...")
    data = capture_picks()
    if not data or not data.get("playerLists"):
        print("No picks feed captured. Leaving the sheet untouched.")
        sys.exit(1)

    data = slim(data)
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if len(raw) > CELL_LIMIT:
        print(f"Feed is {len(raw)} characters, over the {CELL_LIMIT} cell limit. Leaving the sheet untouched.")
        sys.exit(1)

    sh = gs_open()
    write_picks_json(sh, raw)
    df = picks_df(data)
    write_df_to_tab(sh, args.sheet, df)
    print(f"Wrote the feed to 'PicksJSON' and {len(df)} players to '{args.sheet}'.")


if __name__ == "__main__":
    main()
