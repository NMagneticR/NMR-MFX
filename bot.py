import discord
from discord.ext import commands, tasks
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time
import asyncio

# =========================================================================
# ⚙️ TIMEZONE CONFIGURATION
# =========================================================================
LOCAL_TZ = ZoneInfo("Asia/Manila")
# =========================================================================
# ⚙️ CONFIGURATION: YOUR DISCORD SETTINGS
# =========================================================================
SERVER_ID = 1550834844907343972  # Your actual Server ID
TARGET_CHANNEL_ID = 1563502563330887700  # <--- PUT YOUR SPECIFIC CHANNEL ID HERE

# 1. Set up bot intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# 2. Initialize bot
bot = commands.Bot(command_prefix="!", intents=intents)

CSV_BASE_URL = "https://raw.githubusercontent.com/NMagneticR/NMR-MFX/refs/heads/main/myfxbook_calendar.csv"
alerted_events = set()
started_events = set()
post_5min_events = set()
post_10min_events = set()
MY_GUILD_ID = discord.Object(id=SERVER_ID)

def load_calendar_data():
    """Helper to fetch and clean the CSV dataset from GitHub, bypassing cache."""
    try:
        live_url = f"{CSV_BASE_URL}?t={int(time.time())}"
        df = pd.read_csv(live_url, dtype=str)
        df = df.fillna("")
        return df
    except Exception as e:
        print(f"⚠️ Error loading CSV: {e}")
        return pd.DataFrame()

def parse_row_datetime(row):
    """Parses Section Date and Time into a timezone-aware datetime object (24-hour format)."""
    try:
        sec_date = str(row['Section Date']).strip()
        for day in ["Sunday, ", "Monday, ", "Tuesday, ", "Wednesday, ", "Thursday, ", "Friday, ", "Saturday, "]:
            sec_date = sec_date.replace(day, "")
        
        ev_time = str(row['Event Time']).strip()
        time_str = ev_time.split(',')[-1].strip() if ev_time else "00:00"
        if ":" not in time_str:
            time_str = "00:00"
        
        naive_dt = datetime.strptime(f"{sec_date} {time_str}", "%b %d, %Y %H:%M")
        return naive_dt.replace(tzinfo=LOCAL_TZ)
    except:
        return datetime.max.replace(tzinfo=timezone.utc)

def get_currency_flag(currency):
    """Maps currency codes to corresponding flag emojis."""
    flags = {
        "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
        "AUD": "🇦🇺", "NZD": "🇳🇿", "CAD": "🇨🇦", "CHF": "CHF"
    }
    return flags.get(currency.upper(), "🌐")

def get_affected_instruments(currency, event_name):
    """Maps currencies and event types to affected trading instruments based on reference criteria."""
    curr = currency.upper()
    name = event_name.lower()
    instruments = []

    if curr == "USD":
        instruments.extend(["Forex", "XAUUSD", "XAGUSD", "US30", "US100", "US500"])
    elif curr == "EUR":
        instruments.extend(["EUR Forex", "EU Indices"])
    elif curr == "GBP":
        instruments.extend(["GBP Forex", "UK Indices"])
    elif curr == "CAD":
        instruments.extend(["CAD Forex", "Oil (USOIL/UKOIL)"])
    elif curr == "AUD":
        instruments.extend(["AUD Forex"])
    elif curr == "NZD":
        instruments.extend(["NZD Forex"])
    elif curr == "CHF":
        instruments.extend(["CHF Forex"])
    elif curr == "JPY":
        instruments.extend(["JPY Forex", "Asian Indices"])
    else:
        instruments.append("Forex")

    if "oil" in name or "crude" in name:
        instruments.append("USOIL/UKOIL")

    return ", ".join(sorted(list(set(instruments))))

class CalendarPaginator(discord.ui.View):
    """Paginated view for economic calendar embeds with day splitting and multi-page chunking for busy days."""
    def __init__(self, df, title_type, impact, timeout=180):
        super().__init__(timeout=timeout)
        self.df = df
        self.title_type = title_type
        self.impact = impact
        self.current_page = 0
        self.pages_data, self.page_labels = self.split_into_daily_pages()
        self.update_button_states()

    def split_into_daily_pages(self):
        """Groups events day by day and chunks days with > 23 events into multiple sequential pages."""
        days_dict = {}
        for _, row in self.df.iterrows():
            dt = row['Parsed_DT']
            if pd.isna(dt):
                continue
            day_key = dt.strftime('%A, %b %d, %Y')
            if day_key not in days_dict:
                days_dict[day_key] = []
            days_dict[day_key].append(row)
            
        pages = []
        labels = []
        MAX_ITEMS_PER_PAGE = 23
        
        for day_key, rows in days_dict.items():
            total_chunks = (len(rows) + MAX_ITEMS_PER_PAGE - 1) // MAX_ITEMS_PER_PAGE
            for i in range(0, len(rows), MAX_ITEMS_PER_PAGE):
                chunk = rows[i:i + MAX_ITEMS_PER_PAGE]
                chunk_idx = (i // MAX_ITEMS_PER_PAGE) + 1
                
                if total_chunks > 1:
                    label = f"{day_key} (Part {chunk_idx}/{total_chunks})"
                else:
                    label = day_key
                    
                pages.append(chunk)
                labels.append(label)
                
        if not pages:
            return [[]], ["No Upcoming Days"]
            
        return pages, labels

    def update_button_states(self):
        self.prev_button.disabled = (self.current_page == 0)
        self.next_button.disabled = (self.current_page >= len(self.pages_data) - 1)

    def create_embed(self):
        page_rows = self.pages_data[self.current_page]
        current_day_label = self.page_labels[self.current_page]
        
        embed = discord.Embed(
            title=f"Economic Calendar • {self.title_type} ({current_day_label})",
            description=f"Filter: **{self.impact.upper()}** | Page {self.current_page + 1} of {len(self.pages_data)}\n*(NMR | Nyark)*",
            color=discord.Color.from_rgb(0, 168, 232)
        )

        if not page_rows:
            embed.add_field(name="No Events", value="⚠️ No economic events scheduled for this day/filter.", inline=False)
            embed.set_footer(text="Powered by ForexFactory & MyFXBook • Expanded News Feed")
            return embed

        embed.add_field(
            name=f"📅 {current_day_label}",
            value="────────────────────────",
            inline=False
        )

        for row in page_rows:
            section_date = str(row['Section Date']).strip()
            event_time = str(row['Event Time']).strip()
            currency = str(row['Currency']).strip()
            event_name = str(row['Event Name']).strip()
            impact_level = str(row['Impact']).upper()
            
            prev = str(row['Previous']).strip()
            cons = str(row['Consensus']).strip()
            actual = str(row['Actual']).strip()

            flag = get_currency_flag(currency)
            badge = "🔴 **HIGH**" if impact_level == 'HIGH' else "🟠 **MEDIUM**" if impact_level == 'MEDIUM' else "🟢 **LOW**" if impact_level == 'LOW' else "⚪ **HOLIDAY**"

            try:
                clean_date = section_date
                for day_name in ["Sunday, ", "Monday, ", "Tuesday, ", "Wednesday, ", "Thursday, ", "Friday, ", "Saturday, "]:
                    clean_date = clean_date.replace(day_name, "")
                
                time_str = event_time.split(',')[-1].strip() if event_time else ""
                
                if time_str and time_str.lower() != "nan" and ":" in time_str:
                    dt = datetime.strptime(f"{clean_date} {time_str}", "%b %d, %Y %H:%M")
                    epoch_time = int(dt.timestamp())
                    time_display = f"<t:{epoch_time}:d> <t:{epoch_time}:t>"
                else:
                    dt = datetime.strptime(clean_date, "%b %d, %Y")
                    epoch_time = int(dt.timestamp())
                    time_display = f"<t:{epoch_time}:D>"
            except Exception:
                time_display = f"{section_date}"

            cell_lines = [f"{badge} | {time_display}"]
            
            stats = []
            if cons and cons != "nan" and cons != "":
                stats.append(f"Fcst: `{cons}`")
            if prev and prev != "nan" and prev != "":
                stats.append(f"Prev: `{prev}`")
            if actual and actual != "nan" and actual != "":
                stats.append(f"Act: `{actual}`")

            if stats:
                cell_lines.append(" | ".join(stats))

            affected_text = get_affected_instruments(currency, event_name)
            cell_lines.append(f"🎯 `Affected: {affected_text}`")

            field_value = "\n".join(cell_lines)
            embed.add_field(name=f"{flag} {currency} — {event_name}", value=field_value, inline=True)

        embed.set_footer(text="Powered by ForexFactory & MyFXBook • Expanded News Feed")
        return embed

    @discord.ui.button(label="◀ Previous Day", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_button_states()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="Next Day ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.pages_data) - 1:
            self.current_page += 1
            self.update_button_states()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

def prepare_calendar_dataframe(time_frame: str = "week", impact: str = "all", day: str = "all", currency: str = "all"):
    """Fetches, parses, and filters calendar dataframe based on timeframe, impact, day, and currency."""
    df = load_calendar_data()
    if df.empty:
        return None, None, "❌ Error fetching calendar data from repository."

    df['Parsed_DT'] = df.apply(parse_row_datetime, axis=1)
    now = datetime.now(LOCAL_TZ)

    tf = time_frame.lower()
    
    if tf == "last_week":
        start_date = now - timedelta(days=7)
        df = df[(df['Parsed_DT'] >= start_date) & (df['Parsed_DT'] < now)]
        df = df.sort_values(by='Parsed_DT', ascending=True)
        title_type = "Forex News Last Week"
    elif tf == "today":
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_today = start_today + timedelta(days=1)
        df = df[(df['Parsed_DT'] >= start_today) & (df['Parsed_DT'] < end_today)]
        df = df.sort_values(by='Parsed_DT', ascending=True)
        title_type = "Today's Forex News"
    elif tf == "tomorrow":
        start_tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_tomorrow = start_tomorrow + timedelta(days=1)
        df = df[(df['Parsed_DT'] >= start_tomorrow) & (df['Parsed_DT'] < end_tomorrow)]
        df = df.sort_values(by='Parsed_DT', ascending=True)
        title_type = "Tomorrow's Forex News"
    else:
        df = df[df['Parsed_DT'] >= now]
        df = df.sort_values(by='Parsed_DT', ascending=True)
        title_type = "Forex News This Week"

    # Filter by Impact
    imp = impact.lower()
    if imp in ["high", "medium", "low", "none"]:
        df = df[df["Impact"].str.lower() == imp]

    # Filter by Specific Day if requested
    d_filter = day.lower()
    if d_filter != "all":
        days_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
        if d_filter in days_map:
            target_weekday = days_map[d_filter]
            df = df[df['Parsed_DT'].dt.weekday == target_weekday]

    # Filter by Specific Currency if requested
    curr_filter = currency.upper()
    if curr_filter != "ALL":
        df = df[df['Currency'].str.upper() == curr_filter]

    if df.empty:
        label = "today's" if tf == "today" else "tomorrow's" if tf == "tomorrow" else "last week's" if tf == "last_week" else "upcoming"
        return None, None, f"⚠️ No {label} events found matching your filters (Impact: **{impact.upper()}**, Day: **{day.capitalize()}**, Currency: **{currency.upper()}**)."

    return df, title_type, None

# ==========================================
# BACKGROUND TASK: 6:00 AM Daily/Weekly Announcements
# ==========================================
@tasks.loop(minutes=1)
async def daily_announcements():
    now = datetime.now(LOCAL_TZ)
    if now.hour == 6 and now.minute == 0:
        target_channel = bot.get_channel(TARGET_CHANNEL_ID)
        if not target_channel:
            return

        # Monday: Weekly Kickoff (High impact overview)
        if now.weekday() == 0:
            df, title_type, err = prepare_calendar_dataframe(time_frame="week", impact="high")
            if err or df is None:
                return
            paginator = CalendarPaginator(df, title_type, impact="high")
            await target_channel.send(
                content="@everyone 🚨 **New Trading Week Kickoff!**", 
                embed=paginator.create_embed(), 
                view=paginator,
                allowed_mentions=discord.AllowedMentions(everyone=True)
            )
        
        # Tuesday (1) to Friday (4): Today's News Announcement
        elif 1 <= now.weekday() <= 4:
            df, title_type, err = prepare_calendar_dataframe(time_frame="today", impact="high")
            if err or df is None:
                return
            paginator = CalendarPaginator(df, title_type, impact="high")
            day_name = now.strftime('%A, %b %d')
            await target_channel.send(
                content=f"@everyone 🌅 **Good Morning! Here is today's high-impact news lineup for {day_name}:**", 
                embed=paginator.create_embed(), 
                view=paginator,
                allowed_mentions=discord.AllowedMentions(everyone=True)
            )

# ==========================================
# BACKGROUND TASK: Pre-Event & Post-Event Alerts
# ==========================================
@tasks.loop(minutes=1)
async def event_reminder():
    df = load_calendar_data()
    if df.empty:
        return

    df['Parsed_DT'] = df.apply(parse_row_datetime, axis=1)
    now = datetime.now(LOCAL_TZ)

    for _, row in df.iterrows():
        event_dt = row['Parsed_DT']
        if event_dt == datetime.max.replace(tzinfo=timezone.utc):
            continue

        diff_minutes = (event_dt - now).total_seconds() / 60.0
        event_name = str(row['Event Name']).strip()
        impact = str(row['Impact']).strip().upper()
        currency = str(row['Currency']).strip()
        is_speech = 'speech' in event_name.lower()

        if impact == 'HIGH' or is_speech:
            event_id = f"{event_name}_{event_dt.strftime('%Y%m%d%H%M')}"
            epoch_time = int(event_dt.timestamp())
            flag = get_currency_flag(currency)
            affected_text = get_affected_instruments(currency, event_name)

            target_channel = bot.get_channel(TARGET_CHANNEL_ID)
            if not target_channel:
                continue

            # 1. 5-Minute Pre-Event Warning
            if 4 <= diff_minutes <= 5:
                warn_5_id = f"{event_id}_warning_5min"
                if warn_5_id not in alerted_events:
                    alerted_events.add(warn_5_id)
                    embed = discord.Embed(
                        title="⚠️ 5-Min Warning: High-Impact Event Approaching!",
                        description=f"**{flag} {currency} - {event_name}** starts in 5 minutes!",
                        color=discord.Color.gold()
                    )
                    embed.add_field(name="Time", value=f"<t:{epoch_time}:t> (<t:{epoch_time}:R>)", inline=True)
                    embed.add_field(name="Affected Instruments", value=f"`{affected_text}`", inline=False)
                    await target_channel.send(content="@everyone ⚡ **Market Alert (5 Min Warning):**", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))

            # 2. 10-15 Min Pre-Event Warning
            if 10 <= diff_minutes <= 15:
                warn_id = f"{event_id}_warning"
                if warn_id not in alerted_events:
                    alerted_events.add(warn_id)
                    embed = discord.Embed(
                        title="🚨 Upcoming High-Impact Event / Speech!",
                        description=f"**{flag} {currency} - {event_name}** is starting soon!",
                        color=discord.Color.orange()
                    )
                    embed.add_field(name="Impact", value="🔴 **HIGH impact**", inline=True)
                    embed.add_field(name="Scheduled Time", value=f"<t:{epoch_time}:f> (<t:{epoch_time}:R>)", inline=True)
                    embed.add_field(name="Affected Instruments", value=f"`{affected_text}`", inline=False)
                    embed.set_footer(text="Prepare your risk management and charts!")
                    await target_channel.send(content="@everyone ⚠️ **Market Warning (15 Min Warning):**", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))

            # 3. News Starting Now Alert (0 to 1 minute)
            if 0 <= diff_minutes <= 1:
                start_id = f"{event_id}_start"
                if start_id not in started_events:
                    started_events.add(start_id)

                    if is_speech:
                        embed = discord.Embed(
                            title="🎙️ Speech Started — Check Prop Firm Rules!",
                            description=f"**{flag} {currency} - {event_name}** has just started.",
                            color=discord.Color.blue()
                        )
                        embed.add_field(name="Status", value="🗣️ **SPEECH LIVE**", inline=True)
                        embed.add_field(name="Time", value=f"<t:{epoch_time}:t>", inline=True)
                        embed.add_field(name="Affected Instruments", value=f"`{affected_text}`", inline=False)
                        embed.set_footer(text="Be careful! Check your prop firm rules to see if you are allowed to trade during speeches.")
                        await target_channel.send(content="@everyone 📢 **Speech Notice — Check Prop Firm Rules:**", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
                    else:
                        embed = discord.Embed(
                            title="🔴 HIGH-Impact News Starting Now!",
                            description=f"**{flag} {currency} - {event_name}** has just started!",
                            color=discord.Color.red()
                        )
                        embed.add_field(name="Status", value="⚡ **LIVE NOW**", inline=True)
                        embed.add_field(name="Time", value=f"<t:{epoch_time}:t>", inline=True)
                        embed.add_field(name="Affected Instruments", value=f"`{affected_text}`", inline=False)
                        embed.set_footer(text="Manage your open positions and risk accordingly!")
                        await target_channel.send(content="@everyone 🚨 **Market Alert — News Started:**", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))

            # 4. Post-Event Announcements (Only for regular news, NOT speeches)
            if not is_speech:
                # 5 Minutes After News (-5 to -6 minutes past start)
                if -6.0 <= diff_minutes <= -5.0:
                    post_5_id = f"{event_id}_post_5min"
                    if post_5_id not in post_5min_events:
                        post_5min_events.add(post_5_id)
                        embed = discord.Embed(
                            title="⏱️ 5 Minutes Post-News Update",
                            description=f"**{flag} {currency} - {event_name}** concluded 5 minutes ago. Watch out for secondary market volatility and spread normalization.",
                            color=discord.Color.dark_orange()
                        )
                        embed.add_field(name="Affected Instruments", value=f"`{affected_text}`", inline=False)
                        await target_channel.send(embed=embed)

                # 10 Minutes After News (-11 to -10 minutes past start)
                if -11.0 <= diff_minutes <= -10.0:
                    post_10_id = f"{event_id}_post_10min"
                    if post_10_id not in post_10min_events:
                        post_10min_events.add(post_10_id)
                        embed = discord.Embed(
                            title="⏱️ 10 Minutes Post-News Update",
                            description=f"**{flag} {currency} - {event_name}** concluded 10 minutes ago. Market liquidity should be stabilizing.",
                            color=discord.Color.dark_green()
                        )
                        embed.add_field(name="Affected Instruments", value=f"`{affected_text}`", inline=False)
                        await target_channel.send(embed=embed)

# ==========================================
# 1. TRADITIONAL PREFIX COMMAND: !calendar
# ==========================================
@bot.command(name="calendar", help="Shows upcoming economic events (Usage: !calendar week all monday usd)")
async def calendar_prefix(ctx, time_frame: str = "week", impact: str = "all", day: str = "all", currency: str = "all"):
    if ctx.channel.id != TARGET_CHANNEL_ID:
        return
    
    df, title_type, err = prepare_calendar_dataframe(time_frame=time_frame, impact=impact, day=day, currency=currency)
    if err:
        await ctx.send(err)
    else:
        paginator = CalendarPaginator(df, title_type, impact=impact)
        await ctx.send(embed=paginator.create_embed(), view=paginator)

# ==========================================
# 2. DISCORD SLASH COMMAND: /calendar
# ==========================================
@bot.tree.command(name="calendar", description="View economic calendar events.", guild=MY_GUILD_ID)
@discord.app_commands.describe(
    time_frame="Select the time period to view",
    impact="Select the impact level filter",
    day="Filter by a specific day of the week",
    currency="Filter by a specific currency (e.g. USD, EUR)",
    ping_everyone="Choose whether to mention @everyone"
)
@discord.app_commands.choices(
    time_frame=[
        discord.app_commands.Choice(name="Today's News Only", value="today"),
        discord.app_commands.Choice(name="Tomorrow's News Only", value="tomorrow"),
        discord.app_commands.Choice(name="This Week's Outlook", value="week"),
        discord.app_commands.Choice(name="Last Week's News", value="last_week")
    ],
    impact=[
        discord.app_commands.Choice(name="High Impact Only", value="high"),
        discord.app_commands.Choice(name="Medium Impact Only", value="medium"),
        discord.app_commands.Choice(name="Low Impact Only", value="low"),
        discord.app_commands.Choice(name="Bank Holiday (None)", value="none"),
        discord.app_commands.Choice(name="All Impacts / Events", value="all")
    ],
    day=[
        discord.app_commands.Choice(name="All Days", value="all"),
        discord.app_commands.Choice(name="Monday", value="monday"),
        discord.app_commands.Choice(name="Tuesday", value="tuesday"),
        discord.app_commands.Choice(name="Wednesday", value="wednesday"),
        discord.app_commands.Choice(name="Thursday", value="thursday"),
        discord.app_commands.Choice(name="Friday", value="friday"),
        discord.app_commands.Choice(name="Saturday", value="saturday"),
        discord.app_commands.Choice(name="Sunday", value="sunday")
    ],
    currency=[
        discord.app_commands.Choice(name="All Currencies", value="all"),
        discord.app_commands.Choice(name="USD", value="usd"),
        discord.app_commands.Choice(name="EUR", value="eur"),
        discord.app_commands.Choice(name="GBP", value="gbp"),
        discord.app_commands.Choice(name="JPY", value="jpy"),
        discord.app_commands.Choice(name="AUD", value="aud"),
        discord.app_commands.Choice(name="NZD", value="nzd"),
        discord.app_commands.Choice(name="CAD", value="cad"),
        discord.app_commands.Choice(name="CHF", value="chf")
    ],
    ping_everyone=[
        discord.app_commands.Choice(name="Yes (@everyone)", value="yes"),
        discord.app_commands.Choice(name="No", value="no")
    ]
)
async def calendar_slash(interaction: discord.Interaction, time_frame: str = "week", impact: str = "all", day: str = "all", currency: str = "all", ping_everyone: str = "no"):
    if interaction.channel_id != TARGET_CHANNEL_ID:
        await interaction.response.send_message(f"❌ This command can only be used in <#{TARGET_CHANNEL_ID}>.", ephemeral=True)
        return

    await interaction.response.defer()
    df, title_type, err = prepare_calendar_dataframe(time_frame=time_frame, impact=impact, day=day, currency=currency)
    
    content = "@everyone 📊 **Economic Calendar Dashboard**" if ping_everyone == "yes" else None

    if err:
        await interaction.followup.send(err)
    else:
        paginator = CalendarPaginator(df, title_type, impact=impact)
        await interaction.followup.send(
            content=content, 
            embed=paginator.create_embed(), 
            view=paginator,
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )

# ==========================================
# 💻 INTERACTIVE CONSOLE CONTROLLER
# ==========================================
async def console_listener():
    """Listens for commands typed into the Python console with flexible keyword flags."""
    await asyncio.sleep(3)
    
    print("\n" + "="*70)
    print("💻 CONSOLE CONTROLLER ACTIVE (Keyword Flag Support & Reply Feature)")
    print("Available Console Commands:")
    print(" • calendar [flags]")
    print("   Flags: time:week|today|tomorrow|last_week")
    print("          impact:all|high|medium|low|none")
    print("          day:all|monday|tuesday|wednesday|thursday|friday|saturday|sunday")
    print("          curr:all|usd|eur|gbp|jpy|aud|nzd|cad|chf")
    print("          ping:yes|no")
    print("          chan:[channel_id]")
    print("   Examples:")
    print("     calendar")
    print("     calendar day:monday curr:usd ping:yes")
    print("     calendar impact:high time:today chan:1563502563330887700")
    print(" • say [flags] [message]")
    print("   Flags: chan:[channel_id] reply:[message_id]")
    print("   Examples:")
    print("     say @everyone Market update in 10 minutes!")
    print("     say chan:1563502563330887700 reply:132318912345678901 Got it!")
    print(" • help")
    print("="*70 + "\n")

    while True:
        try:
            cmd_text = await asyncio.to_thread(input, "Bot-Console > ")
            parts = cmd_text.strip().split()
            if not parts:
                continue
            
            action = parts[0].lower()
            raw_args = parts[1:]
            
            if action == "calendar":
                # Set default values
                tf = "week"
                imp = "all"
                d_val = "all"
                curr = "all"
                ping = "no"
                dest_channel_id = TARGET_CHANNEL_ID

                # Parse key:value flags dynamically
                for arg in raw_args:
                    if ":" in arg:
                        key, val = arg.split(":", 1)
                        key = key.lower()
                        val = val.strip()
                        if key in ["time", "tf", "time_frame"]:
                            tf = val
                        elif key in ["impact", "imp"]:
                            imp = val
                        elif key in ["day", "d"]:
                            d_val = val
                        elif key in ["curr", "currency", "c"]:
                            curr = val
                        elif key in ["ping", "p"]:
                            ping = val.lower()
                        elif key in ["chan", "channel", "ch"]:
                            if val.isdigit():
                                dest_channel_id = int(val)

                target_channel = bot.get_channel(dest_channel_id)
                if not target_channel:
                    print(f"⚠️ Error: Channel ID {dest_channel_id} not found or bot lacks permissions.")
                    continue

                print(f"🔄 [Console] Fetching calendar -> Time: {tf}, Impact: {imp}, Day: {d_val}, Curr: {curr}, Ping: {ping}, Channel: #{target_channel.name}...")
                
                df, title_type, err = prepare_calendar_dataframe(time_frame=tf, impact=imp, day=d_val, currency=curr)
                if err or df is None:
                    print(f"❌ Error: {err}")
                    continue

                content = "@everyone 📊 **Economic Calendar Dashboard (Console Trigger)**" if ping == "yes" else None
                paginator = CalendarPaginator(df, title_type, impact=imp)
                
                await target_channel.send(
                    content=content,
                    embed=paginator.create_embed(),
                    view=paginator,
                    allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True)
                )
                print(f"✅ Calendar embed successfully posted to #{target_channel.name}!")

            elif action == "say":
                if not raw_args:
                    print("⚠️ Usage error: 'say [flags] [message]'")
                    continue

                dest_channel_id = TARGET_CHANNEL_ID
                reply_message_id = None
                message_words = []

                # Parse optional flags for say (chan:ID reply:ID)
                for arg in raw_args:
                    if arg.lower().startswith("chan:"):
                        chan_val = arg.split(":", 1)[1]
                        if chan_val.isdigit():
                            dest_channel_id = int(chan_val)
                    elif arg.lower().startswith("reply:"):
                        reply_val = arg.split(":", 1)[1]
                        if reply_val.isdigit():
                            reply_message_id = int(reply_val)
                    else:
                        message_words.append(arg)

                message_text = " ".join(message_words)
                if not message_text:
                    print("⚠️ Usage error: Please provide a message body to send.")
                    continue

                target_channel = bot.get_channel(dest_channel_id)
                if not target_channel:
                    print(f"⚠️ Error: Channel ID {dest_channel_id} not found or bot lacks permissions.")
                    continue

                # Fetch reply reference if a message ID was provided
                message_reference = None
                if reply_message_id:
                    try:
                        ref_msg = await target_channel.fetch_message(reply_message_id)
                        message_reference = ref_msg.to_reference()
                    except Exception as e:
                        print(f"⚠️ Warning: Could not fetch message ID {reply_message_id} to reply to: {e}")

                print(f"🔄 [Console] Sending message to #{target_channel.name}...")
                await target_channel.send(
                    content=message_text,
                    reference=message_reference,
                    allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True)
                )
                print(f"✅ Message successfully sent to #{target_channel.name}!")

            elif action == "help":
                print("\n--- Console Commands Help ---")
                print(" 1. calendar [flags]")
                print("    - Use key:value pairs in any order. Omitted flags default to 'all' or 'week'.")
                print("    - Flags: time:week/today, impact:high, day:monday, curr:usd, ping:yes, chan:ID")
                print("    * Example: calendar day:monday curr:usd ping:yes")
                print(" 2. say [flags] [message]")
                print("    - Flags: chan:[channel_id] reply:[message_id]")
                print("    * Example: say reply:132318912345678901 Hello back!")
                print("-----------------------------\n")
            else:
                print(f"❓ Unknown command '{action}'. Type 'help' for available options.")

        except Exception as e:
            print(f"⚠️ Console Controller Error: {e}")

@bot.event
async def on_ready():
    print(f"🤖 Logged in as {bot.user} (ID: {bot.user.id})")
    
    try:
        synced = await bot.tree.sync(guild=MY_GUILD_ID)
        print(f"🔄 Successfully synced {len(synced)} command(s) to your server ID ({SERVER_ID}).")
    except Exception as e:
        print(f"⚠️ Failed to sync guild commands: {e}")

    if not daily_announcements.is_running():
        daily_announcements.start()
    if not event_reminder.is_running():
        event_reminder.start()

    # 👉 Start the interactive console controller
    bot.loop.create_task(console_listener())

    print("Bot is fully ready and dashboard background loops are active!")

# Run the bot
if __name__ == "__main__":
    import os
    TOKEN = os.getenv("DISCORD_TOKEN")
    bot.run(TOKEN)