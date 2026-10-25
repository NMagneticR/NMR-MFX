import time
import re
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
import pandas as pd
from bs4 import BeautifulSoup

MANILA_TZ = ZoneInfo("Asia/Manila")
UTC_TZ = ZoneInfo("UTC")

def filter_economic_calendar(df):
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
    return filtered_df.drop(columns=['Event_Lower', 'Impact_Upper', 'Currency_Upper'])

def localize_mfx_dataframe(df):
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
    """Scrapes MyFXBook economic calendar with enhanced headers to bypass 403 blocks."""
    events = []
    url = "https://www.myfxbook.com/forex-economic-calendar"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.myfxbook.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }
    
    try:
        print("🌐 Fetching MyFXBook calendar via HTTP request...")
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"⚠️ MyFXBook HTTP error status: {response.status_code}")
            return pd.DataFrame()
            
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.find_all('tr')
        currentDate = ""

        for row in rows:
            text = row.get_text(strip=True)
            cols = row.find_all(['td', 'th'])
            
            if len(cols) <= 2 and ',' in text and ('202' in text or '203' in text):
                currentDate = text
                continue

            if len(cols) >= 5:
                colTexts = [col.get_text(strip=True) for col in cols if col.get_text(strip=True) != ""]
                if len(colTexts) >= 5:
                    events.append([currentDate] + colTexts)
                    
    except Exception as e:
        print(f"⚠️ MyFXBook parsing error: {e}")

    if not events:
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
    print("⚡ Fetching ForexFactory via official JSON API...")
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.forexfactory.com/"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        # Check if response returned actual text or an HTML block/error page
        if not response.text.strip().startswith("["):
            print(f"⚠️ ForexFactory returned non-JSON response: {response.text[:100]}")
            return pd.DataFrame()
            
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
                
            rows.append([
                section_date, event_time, "", currency, event_name, impact,
                str(item.get('previous', '')), str(item.get('forecast', '')), str(item.get('actual', ''))
            ])
            
        columns = ['Section Date', 'Event Time', 'Time Left', 'Currency', 'Event Name', 'Impact', 'Previous', 'Consensus', 'Actual']
        df = pd.DataFrame(rows, columns=columns)
        df['Source'] = 'ForexFactory'
        return df
    except Exception as e:
        print(f"⚠️ Error fetching ForexFactory JSON: {e}")
        return pd.DataFrame()

def main():
    try:
        subprocess.run(["git", "config", "--global", "user.email", "your-email@example.com"], check=False)
        subprocess.run(["git", "config", "--global", "user.name", "NMagneticR"], check=False)
        subprocess.run(["git", "remote", "set-url", "origin", "https://YOUR_GITHUB_TOKEN@github.com/NMagneticR/NMR-MFX.git"], check=False)
    except Exception as e:
        print(f"⚠️ Git config note: {e}")

    while True:
        try:
            output_filename = "myfxbook_calendar.csv"
            print("🚀 Starting calendar pipeline...")
            
            df_mfx = scrape_myfxbook()
            df_ff = fetch_forexfactory_json()

            combined_df = pd.concat([df_mfx, df_ff], ignore_index=True)
            if combined_df.empty:
                print("⚠️ No events fetched. Retrying in 15 minutes...")
                time.sleep(900)
                continue

            combined_df = combined_df.drop_duplicates(subset=['Section Date', 'Currency', 'Event Name'], keep='first')
            df_filtered = filter_economic_calendar(combined_df)

            df_filtered.to_csv(output_filename, index=False)
            print(f"📁 Saved {len(df_filtered)} events to {output_filename}")

            print("🔄 Pushing updated CSV to GitHub...")
            try:
                subprocess.run(["git", "add", output_filename], check=True)
                status = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
                if status.returncode != 0:
                    subprocess.run(["git", "commit", "-m", "Auto-update calendar CSV [skip ci]"], check=True)
                    subprocess.run(["git", "push"], check=True)
                    print("🚀 Successfully pushed to GitHub!")
                else:
                    print("ℹ️ CSV data is already up to date.")
            except Exception as e:
                print(f"⚠️ Git automation error: {e}")
                
            time.sleep(900)
            
        except Exception as e:
            print(f"⚠️ Critical loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()