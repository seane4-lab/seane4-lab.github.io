# nst_timhortons_picks_gsheets_v4.py
"""
Loads hockeychallengehelper.com in a real browser (Playwright), captures the
picks feed the page itself loads, and writes it to Google Sheets:

  - 'PicksJSON'!A1   the raw feed (the "NHL Odds" Apps Script reads this)
  - 'PicksJSON'!E1:E2  a note that today's pool has closed (used to skip pointless runs)
  - 'TH Picks' tab   a simple table: Pick, Date, Name, EV Line, PP Line,
                     Opponent, Unavailable

The site stops listing players once every game of the day has started. That is
normal, so v4 treats it as a quiet success and keeps the last capture.

Usage:
  python nst_timhortons_picks_gsheets_v4.py          # capture and write
  python nst_timhortons_picks_gsheets_v4.py --gate   # only check whether today's pool has closed

Needs: pip install playwright pandas gspread gspread-dataframe google-auth
"""
import os
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta

# ====== CONFIG ======
# In the cloud (GitHub Actions) these come from environment variables.
SERVICE_ACCOUNT_FILE = os.environ.get(
    "SERVICE_ACCOUNT_FILE", r"C:\Users\seane\OneDrive\gcp\nst-scraper-key.json")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "PASTE_THE_ID_OF_THE_ODDS_WORKBOOK_HERE")
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", r"C:\Users\seane\OneDrive")
# =====================

URL_HOME = "https://hockeychallengehelper.com"
DEFAULT_SHEET = "TH Picks"
UTC_OFFSET_HOURS = -6          # Alberta, fixed

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DROP_KEYS = {"linemates", "ppLinemates", "gameLogs"}   # not used by the odds script
CELL_LIMIT = 49000                                     # Sheets allows 50,000 characters per cell
FLAG_RANGE = "E1:E2"                                   # E1 label, E2 = date the pool closed
BLOCK_MARKERS = ("access denied", "just a moment", "attention required",
                 "verify you are human", "error 1020", "forbidden", "captcha")


def local_today():
    return (datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)).strftime("%Y-%m-%d")


# ---------- Google Sheets helpers ----------
def gs_open():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(GOOGLE_SHEET_ID)


def get_or_add_ws(sh, title, rows=200, cols=20):
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def write_picks_json(sh, raw):
    ws = get_or_add_ws(sh, "PicksJSON", rows=10, cols=5)
    ws.update_acell("A1", raw)
    return ws


def set_flag(ws, value):
    ws.update(values=[["pool_closed_date"], [value]], range_name=FLAG_RANGE)


def existing_pool_date(ws):
    """Date (YYYY-MM-DD) of the pool currently saved in A1, or ''."""
    try:
        raw = ws.acell("A1").value
        if not raw:
            return ""
        return str(json.loads(raw).get("dateTimeAvailable", ""))[:10]
    except Exception:
        return ""


def write_df_to_tab(sh, title, df):
    from gspread_dataframe import set_with_dataframe
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


def has_players(feed):
    return bool(feed) and any(lst.get("players") for lst in (feed.get("playerLists") or []))


def classify(have_data, saw_feed, nav_status, body_text):
    """
    ok            players captured
    blocked       page didn't load properly (blocked, error page, or unreachable)
    empty_feed    the site answered, but listed no players
    page_no_feed  the page loaded fine, but no picks feed appeared at all
    """
    if have_data:
        return "ok"
    text = (body_text or "").lower()
    if nav_status is None or nav_status >= 400 or any(m in text for m in BLOCK_MARKERS):
        return "blocked"
    return "empty_feed" if saw_feed else "page_no_feed"


def capture_picks():
    """Returns (status, data); see classify() for the statuses."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    with sync_playwright() as p:
        browser, page = open_page(p)

        matches = []
        page.on("response", lambda r: matches.append(r) if "/api/picks" in r.url else None)

        nav_status = None
        try:
            resp = page.goto(URL_HOME, timeout=60000, wait_until="domcontentloaded")
            nav_status = resp.status if resp else None
        except PWTimeout:
            nav_status = None
        try:
            page.wait_for_load_state("networkidle", timeout=60000)
        except PWTimeout:
            pass
        for _ in range(40):              # up to ~20 more seconds
            if matches:
                break
            page.wait_for_timeout(500)

        data, saw_feed = None, False
        for r in matches:
            if r.status == 200:
                try:
                    feed = r.json()
                except Exception:
                    continue
                saw_feed = True
                if has_players(feed):
                    data = feed
                    break

        body = ""
        try:
            body = page.inner_text("body")[:3000]
        except Exception:
            pass

        status = classify(data is not None, saw_feed, nav_status, body)
        if status != "ok":
            try:
                page.screenshot(path=os.path.join(ARTIFACT_DIR, "thpicks_failed.png"))
            except Exception:
                pass
        browser.close()
        return status, data


def slim(data):
    for lst in data.get("playerLists", []):
        for pl in lst.get("players", []):
            for k in DROP_KEYS:
                pl.pop(k, None)
    return data


def picks_df(data):
    import pandas as pd
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


# ---------- Gate: skip the expensive steps once the pool has closed ----------
def gate():
    skip = False
    try:
        ws = gs_open().worksheet("PicksJSON")
        skip = (ws.acell("E2").value or "").strip() == local_today()
    except Exception as e:
        print("Gate could not read the sheet; carrying on:", e)
    print("Today's pool has closed. Skipping the browser steps." if skip
          else "Pool still open (or unknown). Carrying on.")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("skip=" + ("true" if skip else "false") + "\n")
    return 0


# ---------- Main ----------
def run(sheet_tab=DEFAULT_SHEET):
    print("Capturing today's picks feed...")
    status, data = capture_picks()

    if status == "ok":
        data = slim(data)
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if len(raw) > CELL_LIMIT:
            print(f"Feed is {len(raw)} characters, over the {CELL_LIMIT} cell limit. Leaving the sheet untouched.")
            return 1
        sh = gs_open()
        ws = write_picks_json(sh, raw)
        set_flag(ws, "")                      # pool is open, so clear any 'closed' note
        df = picks_df(data)
        write_df_to_tab(sh, sheet_tab, df)
        print(f"Wrote the feed to 'PicksJSON' and {len(df)} players to '{sheet_tab}'.")
        return 0

    if status == "blocked":
        print("The site didn't load properly (blocked, error, or unreachable). Leaving the sheet untouched.")
        return 1

    # The page loaded but offered no players.
    ws = get_or_add_ws(gs_open(), "PicksJSON", rows=10, cols=5)
    today = local_today()
    if existing_pool_date(ws) == today:
        set_flag(ws, today)
        print("No players listed any more: today's pool has closed. Keeping the last capture; "
              "the remaining runs today will skip the browser.")
        return 0
    if status == "empty_feed":
        print("The site lists no players right now (pool not posted yet, or no games). Nothing to update.")
        return 0
    print("The page loaded but no picks feed appeared, and no pool was captured today. "
          "The site may have changed. Leaving the sheet untouched.")
    return 1


def main():
    ap = argparse.ArgumentParser(description="Capture the Hockey Challenge Helper picks feed -> Google Sheets.")
    ap.add_argument("--sheet", default=DEFAULT_SHEET, help=f"Table tab name (default: '{DEFAULT_SHEET}')")
    ap.add_argument("--gate", action="store_true", help="Only check whether today's pool has closed")
    args = ap.parse_args()
    sys.exit(gate() if args.gate else run(args.sheet))


if __name__ == "__main__":
    main()
