import time
import re
import os
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests
import pandas as pd
from playwright.sync_api import sync_playwright

MANILA_TZ = ZoneInfo("Asia/Manila")
UTC_TZ = ZoneInfo("UTC")

def filter_economic_calendar(df):
    """
    Precision filter for economic calendar:
    - Retains ALL HIGH impact events for major currencies.
    - Retains MEDIUM impact events ONLY if they are speeches, rate decisions, or tier-1 macro data.
    - Retains NONE impact events ONLY if they are holidays.
    - Excludes LOW impact noise.
    """
    if df.empty:
        return df

    df['Event_Lower'] = df['Event Name'].astype(str).str.strip().str.lower()
    df['Impact_Upper'] = df['Impact'].astype(str).str.strip().str.upper()
    df['Currency_Upper'] = df['Currency'].astype(str).str.strip().str.upper()

    major_currencies = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'NZD', 'CHF', 'JPY', 'CNY']

    tier1_medium_keywords = [
        'speech', 'rate', 'statement', 'press conference', 'ecb', 'boe', 'boc', 
        'rba', 'rbnz', 'snb', 'boj', 'fed', 'cpi', 'inflation', 'gdp', 'employment', 
        'unemployment', 'nfp', 'non-farm', 'pmi', 'retail sales', 'pce'
    ]

    holiday_keywords = ['holiday', 'national day', 'labor day', 'bank holiday']

    cond_high = (df['Impact_Upper'] == 'HIGH') & (df['Currency_Upper'].isin(major_currencies))
    
    pattern_medium = '|'.join(tier1_medium_keywords)
    cond_medium = (df['Impact_Upper'] == 'MEDIUM') & df['Event_Lower'].str.contains(pattern_medium, na=False)

    pattern_holiday = '|'.join(holiday_keywords)
    cond_holidays = (df['Impact_Upper'] == 'NONE') & df['Event_Lower'].str.contains(pattern_holiday, na=False)

    filtered_df = df[cond_high | cond_medium | cond_holidays].copy()
    filtered_df = filtered_df.drop(columns=['Event_Lower', 'Impact_Upper', 'Currency_Upper'])
    
    return filtered_df

def localize_mfx_dataframe(df):
    """Converts MyFXBook UTC scraped times to explicit Asia/Manila time strings for the CSV."""
    if df.empty:
        return df
    
    updated_rows = []
    for _, row in df.iterrows():
        sec_date = str(row['Section Date']).strip()
        ev_time = str(row['Event Time']).strip()
        
        try:
            time_match = re.search(r'(\d{1,2}):(\d{2})', ev_time)
            if not time_match:
                updated_rows.append(row.to_dict())
                continue
            h, m = time_match.groups()
            
            clean_date = sec_date
            for day in ["Sunday, ", "Monday, ", "Tuesday, ", "Wednesday, ", "Thursday, ", "Friday, ", "Saturday, "]:
                clean_date = clean_date.replace(day, "").strip()
            
            date_obj = pd.to_datetime(clean_date, errors='coerce')
            if pd.isna(date_obj):
                updated_rows.append(row.to_dict())
                continue
            
            dt_utc = datetime(date_obj.year, date_obj.month, date_obj.day, int(h), int(m), tzinfo=UTC_TZ)
            dt_manila = dt_utc.astimezone(MANILA_TZ)
            
            row['Section Date'] = dt_manila.strftime('%A, %b %d, %Y')
            row['Event Time'] = dt_manila.strftime('%b %d, %H:%M')
        except Exception as e:
            print(f"⚠️ Localization error for row: {e}")
            
        updated_rows.append(row.to_dict())
        
    return pd.DataFrame(updated_rows)

def scrape_myfxbook():
    """Scrapes economic calendar from MyFXBook silently with a fresh browser instance each time."""
    events = []
    with sync_playwright() as p:
        print("🌐 Launching fresh headless browser for MyFXBook...")
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            java_script_enabled=True
        )
        page = context.new_page()

        try:
            print("Navigating to MyFXBook Economic Calendar...")
            page.goto("https://www.myfxbook.com/forex-economic-calendar", timeout=60000, wait_until="domcontentloaded")
            print("Waiting for calendar table to render...")
            page.wait_for_selector("table", timeout=40000)
            time.sleep(10)

            events = page.evaluate("""() => {
                const rows = document.querySelectorAll('tr');
                let parsedData = [];
                let currentDate = "";

                rows.forEach(row => {
                    const text = row.innerText.trim();
                    const cols = row.querySelectorAll('td, th');
                    
                    if (cols.length <= 2 && text.includes(',') && (text.includes('202') || text.includes('203'))) {
                        currentDate = text;
                        return;
                    }

                    if (cols.length >= 5) {
                        let colTexts = Array.from(cols).map(col => col.innerText.trim()).filter(t => t !== "");
                        if (colTexts.length >= 5) {
                            parsedData.push([currentDate, ...colTexts]);
                        }
                    }
                });
                return parsedData;
            }""")
        except Exception as e:
            print(f"⚠️ MyFXBook scraping error (possible temporary block/timeout): {e}")
        finally:
            browser.close()

    if not events:
        print("⚠️ MyFXBook returned 0 events on this cycle.")
        return pd.DataFrame()

    max_cols = max(len(r) for r in events)
    normalized = [r + [''] * (max_cols - len(r)) for r in events]
    columns = ['Section Date', 'Event Time', 'Time Left', 'Currency', 'Event Name', 'Impact', 'Previous', 'Consensus', 'Actual']
    cleaned = [r[:len(columns)] for r in normalized]
    
    df = pd.DataFrame(cleaned, columns=columns)
    df = df[df['Event Name'] != '']
    df['Source'] = 'MyFXBook'
    
    df = localize_mfx_dataframe(df)
    return df

def fetch_forexfactory_json():
    """Fetches ForexFactory economic calendar via JSON API (UTC) and converts to Manila Time."""
    print("⚡ Fetching ForexFactory via official JSON API (UTC -> Manila)...")
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        
        rows = []
        for item in data:
            dt_str = item.get('date', '')
            try:
                dt = pd.to_datetime(dt_str)
                if dt.tzinfo is None:
                    dt = dt.tz_localize(UTC_TZ)
                else:
                    dt = dt.tz_convert(UTC_TZ)
                
                dt_manila = dt.tz_convert(MANILA_TZ)
                section_date = dt_manila.strftime('%A, %b %d, %Y')
                event_time = dt_manila.strftime('%b %d, %H:%M')
            except:
                section_date = dt_str
                event_time = ""
                
            currency = item.get('country', '').strip().upper()
            event_name = item.get('title', '').strip()
            impact_raw = item.get('impact', 'None').strip().capitalize()
            
            if impact_raw == 'High':
                impact = 'HIGH'
            elif impact_raw == 'Medium':
                impact = 'MEDIUM'
            elif impact_raw == 'Low':
                impact = 'LOW'
            else:
                impact = 'NONE'
                
            previous = item.get('previous', '')
            consensus = item.get('forecast', '')
            actual = item.get('actual', '')
            
            rows.append([
                section_date,
                event_time,
                "",
                currency,
                event_name,
                impact,
                str(previous),
                str(consensus),
                str(actual)
            ])
            
        columns = ['Section Date', 'Event Time', 'Time Left', 'Currency', 'Event Name', 'Impact', 'Previous', 'Consensus', 'Actual']
        df = pd.DataFrame(rows, columns=columns)
        df['Source'] = 'ForexFactory'
        print(f"📊 ForexFactory JSON events fetched and localized: {len(df)}")
        return df

    except Exception as e:
        print(f"⚠️ Error fetching ForexFactory JSON: {e}")
        return pd.DataFrame()

def main():
    check_count = 0
    while True:
        try:
            check_count += 1
            output_filename = "myfxbook_calendar.csv"
            
            print(f"\n==================================================")
            print(f"🚀 [Check #{check_count}] Starting multi-source calendar pipeline...")
            print(f"🕒 Current Time: {datetime.now(MANILA_TZ).strftime('%b %d, %Y - %H:%M:%S')}")
            print(f"==================================================")
            
            df_mfx = scrape_myfxbook()
            print(f"📊 MyFXBook events scraped: {len(df_mfx)}")

            df_ff = fetch_forexfactory_json()

            combined_df = pd.concat([df_mfx, df_ff], ignore_index=True)
            
            if combined_df.empty:
                print("⚠️ No events fetched from any source. Waiting for next interval...")
            else:
                combined_df = combined_df.drop_duplicates(subset=['Section Date', 'Currency', 'Event Name'], keep='first')
                print(f"📊 Total unique events combined before filtering: {len(combined_df)}")

                df_filtered = filter_economic_calendar(combined_df)

                print(f"✅ Successfully filtered down to {len(df_filtered)} relevant economic events!")
                if not df_filtered.empty:
                    print(df_filtered[['Section Date', 'Source', 'Currency', 'Event Name', 'Impact']].head(10).to_string())

                df_filtered.to_csv(output_filename, index=False)
                print(f"📁 Clean filtered data successfully saved to {output_filename}")

                # --- AUTOMATED GIT SYNC ---
                print("🔄 Pushing updated CSV to GitHub repository...")
                try:
                    subprocess.run(["git", "add", output_filename], check=True)
                    status = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
                    if status.returncode != 0:
                        subprocess.run(["git", "commit", "-m", f"Auto-update calendar CSV (Check #{check_count}) [skip ci]"], check=True)
                        subprocess.run(["git", "push"], check=True)
                        print("🚀 Successfully committed and pushed to GitHub!")
                    else:
                        print("ℹ️ CSV data is already up to date. No git commit needed.")
                except Exception as e:
                    print(f"⚠️ Git automation error: {e}")

            # Clear terminal screen every 3 checks to keep it clean
            if check_count >= 3:
                print("\n🧹 Reached 3 checks. Clearing terminal screen for a fresh view...")
                time.sleep(3)
                os.system('cls' if os.name == 'nt' else 'clear')
                check_count = 0  # Reset counter

            # Calculate exact wait time until the next 15-minute mark (:00, :15, :30, :45)
            now = datetime.now(MANILA_TZ)
            current_minute = now.minute
            next_minute_block = ((current_minute // 15) + 1) * 15
            
            if next_minute_block >= 60:
                # Roll over to the next hour
                next_run = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            else:
                next_run = now.replace(minute=next_minute_block, second=0, microsecond=0)
                
            sleep_seconds = (next_run - now).total_seconds()
            if sleep_seconds < 5:  # Safety fallback if it triggers right on the edge
                sleep_seconds = 15
                
            print(f"\n⏳ Waiting until next scheduled mark ({next_run.strftime('%H:%M:%S')}) — sleeping for {int(sleep_seconds)} seconds...")
            time.sleep(sleep_seconds)
            
        except Exception as e:
            print(f"⚠️ Critical loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()