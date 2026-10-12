# goaliepost_starters.py
"""
Reads today's starting goalies from goaliepost.com (as a real browser sees the
page) and writes them to a 'GoaliePost' tab in your Google Sheet, where the
NHL Odds Apps Script picks them up.

Usage:
  python goaliepost_starters.py                 # scrape + write to the sheet
  python goaliepost_starters.py --force         # ignore the "visited recently" pause
  python goaliepost_starters.py --parse page.txt   # test the parser on a saved page.txt

To be gentle on the site, a normal run does nothing if the last successful visit
was less than GOALIE_MIN_GAP_MINUTES ago (default 55).

Needs: pip install playwright gspread google-auth
"""
import os
import re
import sys
import argparse
from datetime import datetime, timezone, timedelta

URL = "https://goaliepost.com/"
TAB = "GoaliePost"
HEADERS = ["Date", "Away", "Home", "Team", "Goalie", "Number", "Status", "Source", "Note"]
META_CELL = "L2"            # last successful scrape time (UTC); L1 holds its label
TZ_ID = "Etc/GMT+6"         # fixed UTC-6 (Alberta), so the page's "today" matches yours
UTC_OFFSET_HOURS = -6

SERVICE_ACCOUNT_FILE = os.environ.get(
    "SERVICE_ACCOUNT_FILE", r"C:\Users\seane\OneDrive\gcp\nst-scraper-key.json")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "PASTE_THE_ID_OF_THE_ODDS_WORKBOOK_HERE")
MIN_GAP_MINUTES = int(os.environ.get("GOALIE_MIN_GAP_MINUTES", "55"))

# Goaliepost may spell some teams differently from the NHL's three-letter codes.
ALIASES = {"TB": "TBL", "NJ": "NJD", "SJ": "SJS", "LA": "LAK", "UTAH": "UTA",
           "VEG": "VGK", "WAS": "WSH", "MON": "MTL", "NAS": "NSH", "CLB": "CBJ"}

TEAM_RE = re.compile(r"^[A-Z]{2,4}$")
DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th),?\s*([A-Za-z]{3})\s*(\d{1,2}:\d{2}\s*[AP]M)")
AGO_RE = re.compile(r"^(\d+\s+(SECOND|MINUTE|HOUR|DAY)S?\s+AGO|JUST NOW)$", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def norm_team(t):
    t = t.upper()
    return ALIASES.get(t, t)


# ---------- Parsing the page text ----------
def parse_starters(text, year):
    """Turn the page's visible text into one row per listed goalie."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # The sidebar schedule repeats the "TEAM @ TEAM" lines, so stop before it.
    end = next((i for i, ln in enumerate(lines) if ln.upper() == "SCHEDULE"), len(lines))
    lines = lines[:end]

    heads = [i for i in range(len(lines) - 2)
             if TEAM_RE.match(lines[i]) and lines[i + 1] == "@" and TEAM_RE.match(lines[i + 2])]

    rows = []
    for gi, h in enumerate(heads):
        stop = heads[gi + 1] if gi + 1 < len(heads) else len(lines)
        block = lines[h:stop]
        away, home = norm_team(block[0]), norm_team(block[2])

        date_iso = ""
        for ln in block[3:12]:
            m = DATE_RE.search(ln)
            if m:
                month = MONTHS.get(m.group(2).lower())
                if month:
                    date_iso = f"{year}-{month:02d}-{int(m.group(1)):02d}"
                break

        ks = [k for k, ln in enumerate(block) if ln == "W-L-OT" and k >= 1]
        goalies = []
        for n, k in enumerate(ks):
            name = block[k - 1]
            number = block[k - 2] if k >= 2 and block[k - 2].isdigit() else ""

            if n + 1 < len(ks):
                nk = ks[n + 1]
                nxt = nk - 2 if nk >= 2 and block[nk - 2].isdigit() else nk - 1
            else:
                nxt = len(block)

            valid = (block[k + 2:k + 3] == ["GAA"] and block[k + 4:k + 5] == ["SV%"]
                     and block[k + 6:k + 7] == ["SO"])
            status = block[k + 8] if valid and k + 8 < nxt else ""
            content = block[k + 9:nxt] if status else []
            source = content[0] if content else ""
            note = content[1:]
            while note and (note[-1] == "R" or AGO_RE.match(note[-1])):
                note.pop()

            goalies.append({
                "Goalie": name.title(), "Number": number, "Status": status,
                "Source": source, "Note": " ".join(note)[:300],
            })

        for idx, g in enumerate(goalies):
            # First card is the away team, second is the home team. With only one
            # card we can't tell which side it is, so the sheet matches it by roster.
            team = away if (len(goalies) >= 2 and idx == 0) else home if (len(goalies) >= 2 and idx == 1) else ""
            rows.append({"Date": date_iso, "Away": away, "Home": home, "Team": team, **g})
    return rows


# ---------- Browser ----------
def fetch_text():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(timezone_id=TZ_ID, locale="en-US",
                                  viewport={"width": 1366, "height": 2000})
        page = ctx.new_page()
        page.goto(URL, timeout=60000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeout:
            pass
        text = page.inner_text("body")
        if "W-L-OT" not in text:          # cards may still be loading, or no games today
            page.wait_for_timeout(4000)
            text = page.inner_text("body")
        browser.close()
        return text


# ---------- Google Sheets ----------
def open_tab():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(GOOGLE_SHEET_ID)
    try:
        return sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=TAB, rows=60, cols=14)


def recently_scraped(ws):
    try:
        raw = ws.acell(META_CELL).value
        if not raw:
            return False
        last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last < timedelta(minutes=MIN_GAP_MINUTES)
    except Exception:
        return False


def write_rows(ws, rows):
    values = [HEADERS] + [[r[h] for h in HEADERS] for r in rows]
    ws.clear()
    ws.update(values=values, range_name="A1")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ws.update(values=[["last_scrape_utc"], [stamp]], range_name="L1:L2")


# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Goaliepost starting goalies -> Google Sheets.")
    ap.add_argument("--parse", metavar="FILE", help="Parse a saved page.txt and print the rows")
    ap.add_argument("--force", action="store_true", help="Ignore the 'visited recently' pause")
    args = ap.parse_args()

    year = (datetime.now(timezone.utc) + timedelta(hours=UTC_OFFSET_HOURS)).year

    if args.parse:
        with open(args.parse, encoding="utf-8") as f:
            for r in parse_starters(f.read(), year):
                print(r)
        return

    ws = open_tab()
    if not args.force and recently_scraped(ws):
        print(f"Last visit was under {MIN_GAP_MINUTES} minutes ago. Skipping.")
        return

    text = fetch_text()
    if "STARTERS" not in text.upper():
        print("Goaliepost didn't load as expected. Leaving the sheet untouched.")
        sys.exit(1)

    rows = parse_starters(text, year)
    write_rows(ws, rows)
    games = len({(r["Away"], r["Home"]) for r in rows})
    print(f"Wrote {len(rows)} goalies from {games} games to '{TAB}'.")


if __name__ == "__main__":
    main()
