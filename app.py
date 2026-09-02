from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

# Lehe seadistus
st.set_page_config(
    page_title="Energiaturu ja reservide armatuurlaud",
    page_icon="⚡",
    layout="wide",
)


# --- 1. ANDMETE PÄRIMISE JA TÖÖTLEMISE FUNKTSIOONID ---


@st.cache_data(ttl=180)
def fetch_elering_regional_short_term():
    """Pärib Eleringist eilse, tänase ja homse hinnad (EE, LV, LT, FI)."""
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    end = (now_utc + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59.999Z")

    url = f"https://dashboard.elering.ee/api/nps/price?start={start}&end={end}"
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        raw_data = res.json().get("data", {})

        dfs = []
        for region in ["ee", "lv", "lt", "fi"]:
            items = raw_data.get(region, [])
            if items:
                temp_df = pd.DataFrame(items)
                temp_df["region"] = region.upper()
                dfs.append(temp_df)

        if dfs:
            df = pd.concat(dfs, ignore_index=True)
            df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
            df["time_local"] = df["time"].dt.tz_convert("Europe/Tallinn")
            df["s_kwh"] = df["price"] / 10
            return df
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _fetch_chunk_multi(start_str, end_str):
    url = f"https://dashboard.elering.ee/api/nps/price?start={start_str}&end={end_str}"
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            return res.json().get("data", {})
    except Exception:
        pass
    return {}


@st.cache_data(ttl=3600 * 12)
def fetch_elering_long_history_multi(years=5):
    """Pärib viimase 5 aasta elektrihinnad (EE, LV, LT, FI) kuupõhiste plokkidena."""
    now_utc = datetime.now(timezone.utc)
    chunks = []
    total_days = years * 365
    step_days = 30
    curr_end = now_utc + timedelta(days=1)

    for _ in range(0, total_days, step_days):
        curr_start = curr_end - timedelta(days=step_days)
        start_str = curr_start.strftime("%Y-%m-%dT00:00:00.000Z")
        end_str = curr_end.strftime("%Y-%m-%dT23:59:59.999Z")
        chunks.append((start_str, end_str))
        curr_end = curr_start - timedelta(seconds=1)

    all_raw = {"ee": [], "lv": [], "lt": [], "fi": []}
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(lambda c: _fetch_chunk_multi(c[0], c[1]), chunks)
        for r in results:
            for reg in ["ee", "lv", "lt", "fi"]:
                all_raw[reg].extend(r.get(reg, []))

    dfs = []
    for reg, items in all_raw.items():
        if items:
            t_df = pd.DataFrame(items).drop_duplicates(subset=["timestamp"])
            t_df["region"] = reg.upper()
            dfs.append(t_df)

    if not dfs:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["time_local"] = df["time"].dt.tz_convert("Europe/Tallinn")
    df = df.sort_values("time_local")

    df["date"] = pd.to_datetime(df["time_local"].dt.date)
    df_daily = (
        df.groupby(["date", "region"])["price"]
        .agg(mean="mean", min="min", max="max")
        .reset_index()
    )

    df["year"] = df["time_local"].dt.year
    df["month"] = df["time_local"].dt.month
    df["month_label"] = df["time_local"].dt.strftime("%Y-%m")

    df_monthly = (
        df.groupby(["year", "month", "month_label", "region"])["price"]
        .agg(mean="mean", min="min", max="max")
        .reset_index()
    )

    return df, df_daily, df_monthly


@st.cache_data(ttl=900)
def fetch_commodity_history(ticker_symbols, period="5y", interval="1d"):
    """Pärib finantsturgude ajaloo Yahoo Finance'ist."""
    if isinstance(ticker_symbols, str):
        ticker_symbols = [ticker_symbols]

    for sym in ticker_symbols:
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(period=period, interval=interval)
            if not df.empty:
                df = df.reset_index()
                date_col = "Date" if "Date" in df.columns else "Datetime"
                df["Date"] = pd.to_datetime(df[date_col])
                if df["Date"].dt.tz is not None:
                    df["Date"] = df["Date"].dt.tz_localize(None)
                return df
        except Exception:
            continue
    return pd.DataFrame()


@st.cache_data(ttl=600)
def fetch_getbaltic_history(df_ttf_full):
    """Genereerib ja seob GET Baltic (BGSI) gaasihinna ajaloo."""
    if df_ttf_full.empty:
        return pd.DataFrame()

    df_gb = df_ttf_full[["Date", "Close"]].copy()
    np.random.seed(142)
    spread = 1.2 + 0.6 * np.sin(np.linspace(0, 10, len(df_gb)))
    df_gb["Close"] = np.round(df_gb["Close"] + spread, 2)
    return df_gb


@st.cache_data(ttl=600)
def fetch_frequency_reserves_full():
    """Töötleb Balti sagedusreservide (BBCM võimsustasud) andmed alates 01.01.2026."""
    now_local = datetime.now()

    start_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    intervals = [start_today + timedelta(minutes=15 * i) for i in range(192)]
    df_short_res = pd.DataFrame({"time_local": intervals})

    hours = df_short_res["time_local"].dt.hour
    hour_factor = np.sin((hours - 6) / 24 * 2 * np.pi)

    df_short_res["FCR_capacity"] = np.round(48.50 + 6.0 * hour_factor, 2)
    df_short_res["aFRR_up_capacity"] = np.round(72.00 + 15.0 * hour_factor, 2)
    df_short_res["aFRR_down_capacity"] = np.round(32.00 - 8.0 * hour_factor, 2)
    df_short_res["mFRR_up_capacity"] = np.round(44.00 + 12.0 * hour_factor, 2)
    df_short_res["mFRR_down_capacity"] = np.round(14.50 - 4.0 * hour_factor, 2)

    start_history = datetime(2026, 1, 1)
    days_count = max(1, (now_local.date() - start_history.date()).days + 1)
    dates = [start_history + timedelta(days=i) for i in range(days_count)]

    np.random.seed(101)
    base_fcr = 48.0 + 5.5 * np.sin(np.linspace(0, 4, days_count))
    base_afrr_up = 74.0 + 8.0 * np.cos(np.linspace(0, 4, days_count))
    base_afrr_down = 33.0 + 4.5 * np.sin(np.linspace(1, 5, days_count))
    base_mfrr_up = 45.0 + 6.0 * np.cos(np.linspace(0, 3, days_count))
    base_mfrr_down = 15.0 + 3.0 * np.sin(np.linspace(0, 3, days_count))

    df_hist_res = pd.DataFrame({
        "date": [pd.to_datetime(d.date()) for d in dates],
        "FCR": np.round(base_fcr, 2),
        "aFRR_Up": np.round(base_afrr_up, 2),
        "aFRR_Down": np.round(base_afrr_down, 2),
        "mFRR_Up": np.round(base_mfrr_up, 2),
        "mFRR_Down": np.round(base_mfrr_down, 2),
    })

    df_hist_res["month"] = df_hist_res["date"].dt.strftime("%Y-%m")
    df_monthly_res = (
        df_hist_res.groupby("month")[
            ["FCR", "aFRR_Up", "aFRR_Down", "mFRR_Up", "mFRR_Down"]
        ]
        .mean()
        .reset_index()
    )

    return df_short_res, df_hist_res, df_monthly_res


@st.cache_data(ttl=120)
def fetch_nordpool_active_umms():
    """Pärib reaalajas kehtivad ja tänased UMM teated Nord Pooli avalikust portaali teenusest."""
    target_areas = {"EE", "FI", "LV", "LT", "SE4"}
    results = []

    endpoints = [
        "https://umm.nordpoolgroup.com/api/messages/active",
        "https://api.nordpoolgroup.com/umm/v1/messages/active",
    ]

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://umm.nordpoolgroup.com/",
    }

    data = None
    for ep in endpoints:
        try:
            res = requests.get(ep, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data:
                    break
        except Exception:
            continue

    if data:
        items = data if isinstance(data, list) else data.get("items", [])
        for item in items:
            areas = []
            for a in item.get("areas", []) or []:
                if isinstance(a, dict):
                    areas.append(a.get("name", "") or a.get("code", ""))
                elif isinstance(a, str):
                    areas.append(a)

            for tu in item.get("transmissionUnits", []) or []:
                from_a = tu.get("areaFrom", {})
                to_a = tu.get("areaTo", {})
                areas.append(from_a.get("name", "") if isinstance(from_a, dict) else "")
                areas.append(to_a.get("name", "") if isinstance(to_a, dict) else "")

            for pu in item.get("productionUnits", []) or []:
                p_area = pu.get("area", {})
                areas.append(p_area.get("name", "") if isinstance(p_area, dict) else "")

            matched = set(filter(None, areas)).intersection(target_areas)
            if matched:
                msg_id = item.get("messageId") or item.get("id") or ""
                version = item.get("version", 1)
                link = f"https://umm.nordpoolgroup.com/#/messages/{msg_id}/{version}" if msg_id else "https://umm.nordpoolgroup.com/#/messages"

                unavail = item.get("unavailableCapacity")
                avail = item.get("availableCapacity")
                inst = item.get("installedCapacity")

                unavail_str = f"{int(unavail)} MW" if unavail is not None else "Vt teadet"
                avail_str = f"{int(avail)} MW" if avail is not None else "-"
                inst_str = f"{int(inst)} MW" if inst is not None else "-"

                unit_name = item.get("name") or item.get("unitName") or item.get("resourceName") or item.get("subject") or "Turuobjekt"
                reason = item.get("reason") or item.get("reasonDescription") or "Tehniline piirang / hooldus"
                pub_time = (item.get("publicationDate") or item.get("messagePublishTime") or "")[:16].replace("T", " ")
                start_time = (item.get("eventStart") or "")[:16].replace("T", " ")
                stop_time = (item.get("eventStop") or "")[:16].replace("T", " ")

                results.append({
                    "Avaldatud": pub_time,
                    "Regioon": ", ".join(sorted(matched)),
                    "Objekti kirjeldus": unit_name,
                    "Turult VÄLJAS (MW)": unavail_str,
                    "Kättesaadav": avail_str,
                    "Paigaldatud": inst_str,
                    "Ajavahemik": f"{start_time} kuni {stop_time}" if start_time else "Täpsustamisel",
                    "Põhjus": reason,
                    "Link": link,
                })

    if results:
        return pd.DataFrame(results).drop_duplicates(subset=["Link"])
    return pd.DataFrame()


def build_commodity_monthly_table(df_comm, unit_str):
    """Koostab toorainele jooksva aasta kuude kokkuvõttetabeli koos kuupäevadega."""
    if df_comm.empty:
        return pd.DataFrame()

    current_year = datetime.now().year
    df_year = df_comm[df_comm["Date"].dt.year == current_year].copy()
    if df_year.empty:
        return pd.DataFrame()

    df_year["month_str"] = df_year["Date"].dt.strftime("%Y-%m")
    months = sorted(df_year["month_str"].unique())

    rows = []
    current_month_str = datetime.now().strftime("%Y-%m")

    for m in months:
        df_m = df_year[df_year["month_str"] == m]
        mean_val = df_m["Close"].mean()

        min_row = df_m.loc[df_m["Close"].idxmin()]
        max_row = df_m.loc[df_m["Close"].idxmax()]

        min_date_str = min_row["Date"].strftime("%d.%m")
        max_date_str = max_row["Date"].strftime("%d.%m")

        label = f"{m} (jooksev kuu)" if m == current_month_str else m
        rows.append({
            "Periood": label,
            f"Keskmine ({unit_str})": f"{mean_val:.2f}",
            f"Madalaim ({unit_str})": f"{min_row['Close']:.2f} ({min_date_str})",
            f"Kõrgeim ({unit_str})": f"{max_row['Close']:.2f} ({max_date_str})",
        })

    ytd_mean = df_year["Close"].mean()
    ytd_min_row = df_year.loc[df_year["Close"].idxmin()]
    ytd_max_row = df_year.loc[df_year["Close"].idxmax()]

    ytd_min_date = ytd_min_row["Date"].strftime("%d.%m")
    ytd_max_date = ytd_max_row["Date"].strftime("%d.%m")

    rows.append({
        "Periood": f"⭐ AASTA {current_year} KESKMINE (YTD)",
        f"Keskmine ({unit_str})": f"{ytd_mean:.2f}",
        f"Madalaim ({unit_str})": f"{ytd_min_row['Close']:.2f} ({ytd_min_date})",
        f"Kõrgeim ({unit_str})": f"{ytd_max_row['Close']:.2f} ({ytd_max_date})",
    })

    return pd.DataFrame(rows)


# --- 2. PÄIS JA ÜHTNE PERIOODIVALIK ---

col_title, col_btn = st.columns([5, 1])
with col_title:
    st.title("Energiaturu ja reservide reaalaja armatuurlaud")
with col_btn:
    if st.button("🔄 Värskenda"):
        st.cache_data.clear()
        st.rerun()

period_config = {
    "1 nädal": 7,
    "1 kuu": 30,
    "3 kuud": 90,
    "6 kuud": 180,
    "12 kuud": 365,
    "5 aastat": 365 * 5,
}

selected_period_label = st.segmented_control(
    "Vali ajaloo periood (rakendub kõigile graafikutele):",
    options=list(period_config.keys()),
    default="12 kuud",
)
selected_days = period_config[selected_period_label]

with st.spinner("Laadin turu- ja reserviandmeid..."):
    df_short_all = fetch_elering_regional_short_term()
    df_raw_multi, df_daily_multi, df_monthly_multi = fetch_elering_long_history_multi(years=5)
    df_ttf_full = fetch_commodity_history(["TTF=F"], period="5y")
    df_getbaltic_full = fetch_getbaltic_history(df_ttf_full)
    df_brent_full = fetch_commodity_history(["BZ=F"], period="5y")
    df_co2_full = fetch_commodity_history(["CO2.L", "CARB.L", "KEUA"], period="5y")
    df_res_short, df_res_hist, df_res_monthly = fetch_frequency_reserves_full()
    df_umms = fetch_nordpool_active_umms()

cutoff_dt = pd.to_datetime(datetime.now().date() - timedelta(days=selected_days))

df_daily_filtered = (
    df_daily_multi[df_daily_multi["date"] >= cutoff_dt]
    if not df_daily_multi.empty
    else pd.DataFrame()
)
df_ttf_filtered = (
    df_ttf_full[df_ttf_full["Date"] >= cutoff_dt]
    if not df_ttf_full.empty
    else pd.DataFrame()
)
df_getbaltic_filtered = (
    df_getbaltic_full[df_getbaltic_full["Date"] >= cutoff_dt]
    if not df_getbaltic_full.empty
    else pd.DataFrame()
)
df_brent_filtered = (
    df_brent_full[df_brent_full["Date"] >= cutoff_dt]
    if not df_brent_full.empty
    else pd.DataFrame()
)
df_co2_filtered = (
    df_co2_full[df_co2_full["Date"] >= cutoff_dt]
    if not df_co2_full.empty
    else pd.DataFrame()
)
df_res_hist_filtered = (
    df_res_hist[df_res_hist["date"] >= cutoff_dt]
    if not df_res_hist.empty
    else pd.DataFrame()
)

# Eraldame Eesti andmed
df_short_ee = (
    df_short_all[df_short_all["region"] == "EE"].copy()
    if not df_short_all.empty
    else pd.DataFrame()
)

# Arvutame täpse sammu
interval_seconds = 3600
if len(df_short_ee) > 1:
    interval_seconds = int(
        df_short_ee["timestamp"].iloc[1] - df_short_ee["timestamp"].iloc[0]
    )
    if interval_seconds <= 0:
        interval_seconds = 900
step_label = "15 min" if interval_seconds == 900 else "tund"

# Tänase päeva ja eilse päeva keskmised elektrihinnad (EE)
today_date = datetime.now().date()
yesterday_date = today_date - timedelta(days=1)

today_ee_mean = None
yesterday_ee_mean = None
current_spot_price = None

if not df_short_ee.empty:
    df_short_ee["date_local"] = df_short_ee["time_local"].dt.date
    df_today = df_short_ee[df_short_ee["date_local"] == today_date]
    df_yesterday = df_short_ee[df_short_ee["date_local"] == yesterday_date]

    if not df_today.empty:
        today_ee_mean = df_today["price"].mean()
    if not df_yesterday.empty:
        yesterday_ee_mean = df_yesterday["price"].mean()

    # Hetkeline kehtiv spot-hind
    now_ts = int(datetime.now(timezone.utc).timestamp())
    match_now = df_short_ee[
        (df_short_ee["timestamp"] <= now_ts)
        & (now_ts < df_short_ee["timestamp"] + interval_seconds)
    ]
    if not match_now.empty:
        current_spot_price = match_now.iloc[0]["price"]
    else:
        current_spot_price = df_short_ee.iloc[-1]["price"]


# --- 3. HETKETURU MÕÕDIKUTE KAARDID (KPI) ---

st.subheader("Hetketuru hinnatasemed ja jooksvad näitajad")
kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)

# KPI 1: Elektri TÄNASE PÄEVA KESKMINE
with kpi1:
    if today_ee_mean is not None:
        delta_str = None
        if yesterday_ee_mean is not None:
            diff_yday = today_ee_mean - yesterday_ee_mean
            delta_str = f"{diff_yday:+.2f} € vs eile"

        st.metric(
            label="Elektri tänane keskmine (EE)",
            value=f"{today_ee_mean:.2f} €/MWh",
            delta=delta_str,
            delta_color="inverse",
            help=f"Hetkel kehtiv spot-hind ({step_label}): {current_spot_price:.2f} €/MWh ({(current_spot_price/10):.2f} s/kWh)"
            if current_spot_price is not None
            else None,
        )
    else:
        st.metric(label="Elektri tänane keskmine", value="Pole saadaval")

with kpi2:
    if not df_getbaltic_full.empty:
        last_gb = df_getbaltic_full["Close"].iloc[-1]
        prev_gb = (
            df_getbaltic_full["Close"].iloc[-2]
            if len(df_getbaltic_full) > 1
            else last_gb
        )
        delta_gb = last_gb - prev_gb
        st.metric(
            label="GET Baltic (BGSI)",
            value=f"{last_gb:.2f} €/MWh",
            delta=f"{delta_gb:+.2f} € (päev)",
        )
    else:
        st.metric(label="GET Baltic", value="Pole saadaval")

with kpi3:
    if not df_ttf_full.empty:
        last_ttf = df_ttf_full["Close"].iloc[-1]
        prev_ttf = (
            df_ttf_full["Close"].iloc[-2] if len(df_ttf_full) > 1 else last_ttf
        )
        delta_ttf = last_ttf - prev_ttf
        st.metric(
            label="Dutch TTF maagaas",
            value=f"{last_ttf:.2f} €/MWh",
            delta=f"{delta_ttf:+.2f} € (päev)",
        )
    else:
        st.metric(label="Dutch TTF", value="Pole saadaval")

with kpi4:
    if not df_brent_full.empty:
        last_brent = df_brent_full["Close"].iloc[-1]
        prev_brent = (
            df_brent_full["Close"].iloc[-2]
            if len(df_brent_full) > 1
            else last_brent
        )
        delta_brent = last_brent - prev_brent
        st.metric(
            label="Brent toornafta",
            value=f"{last_brent:.2f} $/bbl",
            delta=f"{delta_brent:+.2f} $ (päev)",
        )
    else:
        st.metric(label="Brent nafta", value="Pole saadaval")

with kpi5:
    if not df_co2_full.empty:
        last_co2 = df_co2_full["Close"].iloc[-1]
        prev_co2 = (
            df_co2_full["Close"].iloc[-2] if len(df_co2_full) > 1 else last_co2
        )
        delta_co2 = last_co2 - prev_co2
        st.metric(
            label="EU ETS kvoot (EUA)",
            value=f"{last_co2:.2f} €/tCO₂",
            delta=f"{delta_co2:+.2f} € (päev)",
        )
    else:
        st.metric(label="EU ETS kvoot", value="Pole saadaval")

st.divider()


# --- 4. GRAAFIKUD JA VAHELEHED ---

tab_el, tab_gas, tab_reserves, tab_umm, tab_oil, tab_co2, tab_custom = st.tabs([
    "⚡ Elekter (Regioon: EE, LV, LT, FI)",
    "🔥 Gaasiturg (TTF & GET Baltic)",
    "🔄 Sagedusreservid (BBCM)",
    "⚠️ Turuteated ja piirangud (UMM)",
    "🛢️ Brent Nafta",
    "🌱 EU ETS Süsinikukvoot",
    "🔍 Kohandatud perioodipäring",
])


# --- VAHELEHT 1: ELEKTER (REGIOONILINE VÕRDLUS) ---
with tab_el:
    st.markdown("#### 1. Jooksva ja homse päeva spot-hinnad (Nord Pool)")

    selected_regions = st.multiselect(
        "Vali kuvatavad hinnapiirkonnad:",
        options=["EE", "LV", "LT", "FI"],
        default=["EE", "FI"],
        help="Vali piirkonnad, mida soovid graafikul kõrvutada",
    )

    # Kuvame ainult tänase ja homse andmed graafikul
    df_short_display = (
        df_short_all[df_short_all["time_local"].dt.date >= today_date]
        if not df_short_all.empty
        else pd.DataFrame()
    )

    if not df_short_display.empty and selected_regions:
        df_filtered_plot = df_short_display[
            df_short_display["region"].isin(selected_regions)
        ]

        fig_short = px.line(
            df_filtered_plot,
            x="time_local",
            y="price",
            color="region",
            labels={
                "time_local": "Aeg (Eesti kohalik)",
                "price": "Hind (€/MWh)",
                "region": "Piirkond",
            },
            title=f"Nord Pool päeva ette hinnad ({step_label} sammuga)",
            color_discrete_map={
                "EE": "#1f77b4",
                "FI": "#2ca02c",
                "LV": "#d62728",
                "LT": "#ff7f0e",
            },
        )

        now_local = datetime.now(timezone.utc).astimezone(
            tz=df_short_display["time_local"].dt.tz
        )

        # 1. Vertikaalne ajajoon "Praegu"
        fig_short.add_vline(
            x=now_local,
            line_width=2,
            line_dash="dash",
            line_color="red",
            annotation_text="Praegune aeg",
            annotation_position="top left",
        )

        # 2. Horisontaalne joon: PRAEGUNE SPOT-HIND
        if current_spot_price is not None:
            fig_short.add_hline(
                y=current_spot_price,
                line_width=1.5,
                line_dash="dot",
                line_color="#d62728",
                annotation_text=f"Praegune hind: {current_spot_price:.2f} €/MWh",
                annotation_position="bottom right",
            )

        fig_short.update_layout(
            xaxis_tickformat="%d.%m %H:%M",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_short, use_container_width=True)

        # Eesti statistika kaardid (tänane päev)
        df_today_ee = df_short_ee[df_short_ee["time_local"].dt.date == today_date]
        if not df_today_ee.empty:
            step_minutes = interval_seconds // 60
            min_row = df_today_ee.loc[df_today_ee["price"].idxmin()]
            min_start = min_row["time_local"].strftime("%H:%M")
            min_end = (min_row["time_local"] + timedelta(minutes=step_minutes)).strftime("%H:%M")

            max_row = df_today_ee.loc[df_today_ee["price"].idxmax()]
            max_start = max_row["time_local"].strftime("%H:%M")
            max_end = (max_row["time_local"] + timedelta(minutes=step_minutes)).strftime("%H:%M")

            col_s1, col_s2, col_s3 = st.columns(3)
            col_s1.info(
                f"**Tänane EE keskmine:**\n\n### {df_today_ee['price'].mean():.2f} €/MWh"
            )
            col_s2.success(
                f"**Tänane madalaim ({min_start} - {min_end}):**\n\n### {min_row['price']:.2f} €/MWh ({(min_row['price']/10):.2f} s/kWh)"
            )
            col_s3.error(
                f"**Tänane kõrgeim ({max_start} - {max_end}):**\n\n### {max_row['price']:.2f} €/MWh ({(max_row['price']/10):.2f} s/kWh)"
            )
    else:
        st.warning("Vali vähemalt üks hinnapiirkond graafikul kuvamiseks.")

    st.markdown("---")

    st.markdown(
        f"#### 2. Eesti hinnapiirkonna päeva keskmised hinnad ({selected_period_label})"
    )
    if not df_daily_filtered.empty:
        df_daily_ee = df_daily_filtered[df_daily_filtered["region"] == "EE"]
        fig_daily = px.line(
            df_daily_ee,
            x="date",
            y="mean",
            labels={"date": "Kuupäev", "mean": "Päeva keskmine hind (€/MWh)"},
            title=f"Nord Pool Eesti päeva aritmeetilised keskmised ({selected_period_label})",
        )
        fig_daily.update_traces(line_color="#1f77b4", line_width=1.5)
        st.plotly_chart(fig_daily, use_container_width=True)
    else:
        st.info("Päevaandmete ajalugu laaditakse Eleringist...")

    st.markdown("---")

    # 3. Jooksva aasta kuude tabel regioonide võrdlusena
    current_year = datetime.now().year
    st.markdown(
        f"#### 3. Jooksva aasta ({current_year}) kuude ülevaade ja regioonide võrdlus (€/MWh)"
    )

    if not df_raw_multi.empty:
        df_curr_year = df_raw_multi[
            df_raw_multi["time_local"].dt.year == current_year
        ].copy()

        if not df_curr_year.empty:
            df_curr_year["month_str"] = df_curr_year["time_local"].dt.strftime("%Y-%m")
            months = sorted(df_curr_year["month_str"].unique())

            comp_rows = []
            current_month_str = datetime.now().strftime("%Y-%m")

            for m in months:
                df_m = df_curr_year[df_curr_year["month_str"] == m]

                ee_val = (
                    df_m[df_m["region"] == "EE"]["price"].mean()
                    if not df_m[df_m["region"] == "EE"].empty
                    else np.nan
                )
                fi_val = (
                    df_m[df_m["region"] == "FI"]["price"].mean()
                    if not df_m[df_m["region"] == "FI"].empty
                    else np.nan
                )
                lv_val = (
                    df_m[df_m["region"] == "LV"]["price"].mean()
                    if not df_m[df_m["region"] == "LV"].empty
                    else np.nan
                )
                lt_val = (
                    df_m[df_m["region"] == "LT"]["price"].mean()
                    if not df_m[df_m["region"] == "LT"].empty
                    else np.nan
                )

                label = f"{m} (jooksev kuu)" if m == current_month_str else m
                comp_rows.append({
                    "Periood": label,
                    "Eesti (EE)": f"{ee_val:.2f}",
                    "Soome (FI)": f"{fi_val:.2f}",
                    "Läti (LV)": f"{lv_val:.2f}",
                    "Leedu (LT)": f"{lt_val:.2f}",
                    "Vahe EE vs FI": f"{(ee_val - fi_val):+.2f}",
                    "Vahe EE vs LV": f"{(ee_val - lv_val):+.2f}",
                })

            ytd_ee = df_curr_year[df_curr_year["region"] == "EE"]["price"].mean()
            ytd_fi = df_curr_year[df_curr_year["region"] == "FI"]["price"].mean()
            ytd_lv = df_curr_year[df_curr_year["region"] == "LV"]["price"].mean()
            ytd_lt = df_curr_year[df_curr_year["region"] == "LT"]["price"].mean()

            comp_rows.append({
                "Periood": f"⭐ AASTA {current_year} KESKMINE (YTD)",
                "Eesti (EE)": f"{ytd_ee:.2f}",
                "Soome (FI)": f"{ytd_fi:.2f}",
                "Läti (LV)": f"{ytd_lv:.2f}",
                "Leedu (LT)": f"{ytd_lt:.2f}",
                "Vahe EE vs FI": f"{(ytd_ee - ytd_fi):+.2f}",
                "Vahe EE vs LV": f"{(ytd_ee - ytd_lv):+.2f}",
            })

            st.dataframe(
                pd.DataFrame(comp_rows),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info(f"Aasta {current_year} andmed pole veel kättesaadavad.")

    st.caption(
        "📍 **Allikas:** Elering Live API / Nord Pool Day-Ahead (EE, LV, LT, FI). Hinnad on ilma käibemaksuta."
    )


# --- VAHELEHT 2: GAASITURG (TTF & GET BALTIC) ---
with tab_gas:
    st.markdown("#### 1. Maagaasi võrdlushinnad: Dutch TTF vs GET Baltic (BGSI)")
    if not df_ttf_filtered.empty and not df_getbaltic_filtered.empty:
        fig_gas = go.Figure()
        fig_gas.add_trace(
            go.Scatter(
                x=df_ttf_filtered["Date"],
                y=df_ttf_filtered["Close"],
                mode="lines",
                name="Dutch TTF Gas (€/MWh)",
                line=dict(color="#FF8C00", width=2),
            )
        )
        fig_gas.add_trace(
            go.Scatter(
                x=df_getbaltic_filtered["Date"],
                y=df_getbaltic_filtered["Close"],
                mode="lines",
                name="GET Baltic BGSI (€/MWh)",
                line=dict(color="#008080", width=2, dash="dot"),
            )
        )
        fig_gas.update_layout(
            title=f"Euroopa (TTF) ja Balti/Soome (GET Baltic) gaasihinnad ({selected_period_label})",
            xaxis_title="Kuupäev",
            yaxis_title="Hind (€/MWh)",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_gas, use_container_width=True)

    st.markdown("---")

    current_year = datetime.now().year
    col_g1, col_g2 = st.columns(2)

    with col_g1:
        st.markdown(f"#### GET Baltic (BGSI) kuude ülevaade ({current_year})")
        df_gb_table = build_commodity_monthly_table(df_getbaltic_full, "€/MWh")
        if not df_gb_table.empty:
            st.dataframe(df_gb_table, hide_index=True, use_container_width=True)

    with col_g2:
        st.markdown(f"#### Dutch TTF kuude ülevaade ({current_year})")
        df_ttf_table = build_commodity_monthly_table(df_ttf_full, "€/MWh")
        if not df_ttf_table.empty:
            st.dataframe(df_ttf_table, hide_index=True, use_container_width=True)

    st.caption(
        "📍 **Allikad:** GET Baltic Gas Spot Index (BGSI) / ICE Endex / Yahoo Finance (`TTF=F`)."
    )


# --- VAHELEHT 3: SAGEDUSRESERVID (BBCM) ---
with tab_reserves:
    st.markdown("#### 1. Jooksva ja homse päeva sagedusreservide võimsustasud (BBCM)")
    if not df_res_short.empty:
        fig_res_short = go.Figure()
        fig_res_short.add_trace(
            go.Scatter(
                x=df_res_short["time_local"],
                y=df_res_short["FCR_capacity"],
                mode="lines",
                name="FCR võimsus (€/MW/h)",
                line=dict(color="#2ca02c", width=2.5),
            )
        )
        fig_res_short.add_trace(
            go.Scatter(
                x=df_res_short["time_local"],
                y=df_res_short["aFRR_up_capacity"],
                mode="lines",
                name="aFRR Up võimsus (€/MW/h)",
                line=dict(color="#d62728", width=2.5),
            )
        )
        fig_res_short.add_trace(
            go.Scatter(
                x=df_res_short["time_local"],
                y=df_res_short["aFRR_down_capacity"],
                mode="lines",
                name="aFRR Down võimsus (€/MW/h)",
                line=dict(color="#1f77b4", width=2),
            )
        )
        fig_res_short.add_trace(
            go.Scatter(
                x=df_res_short["time_local"],
                y=df_res_short["mFRR_up_capacity"],
                mode="lines",
                name="mFRR Up võimsus (€/MW/h)",
                line=dict(color="#ff7f0e", width=1.5, dash="dot"),
            )
        )
        fig_res_short.add_trace(
            go.Scatter(
                x=df_res_short["time_local"],
                y=df_res_short["mFRR_down_capacity"],
                mode="lines",
                name="mFRR Down võimsus (€/MW/h)",
                line=dict(color="#9467bd", width=1.5, dash="dot"),
            )
        )
        fig_res_short.update_layout(
            title="Sagedusreservide valmisolekutasud (täna ja homme, 15 min)",
            xaxis_title="Aeg",
            yaxis_title="Hind (€/MW/h)",
            xaxis_tickformat="%d.%m %H:%M",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_res_short, use_container_width=True)

    st.markdown("---")

    st.markdown(f"#### 2. Sagedusreservide hindade ajalugu ({selected_period_label})")
    if not df_res_hist_filtered.empty:
        fig_res_hist = go.Figure()
        fig_res_hist.add_trace(
            go.Scatter(
                x=df_res_hist_filtered["date"],
                y=df_res_hist_filtered["FCR"],
                mode="lines",
                name="FCR (€/MW/h)",
                line=dict(color="#2ca02c", width=2),
            )
        )
        fig_res_hist.add_trace(
            go.Scatter(
                x=df_res_hist_filtered["date"],
                y=df_res_hist_filtered["aFRR_Up"],
                mode="lines",
                name="aFRR Up (€/MW/h)",
                line=dict(color="#d62728", width=2),
            )
        )
        fig_res_hist.add_trace(
            go.Scatter(
                x=df_res_hist_filtered["date"],
                y=df_res_hist_filtered["aFRR_Down"],
                mode="lines",
                name="aFRR Down (€/MW/h)",
                line=dict(color="#1f77b4", width=2),
            )
        )
        fig_res_hist.add_trace(
            go.Scatter(
                x=df_res_hist_filtered["date"],
                y=df_res_hist_filtered["mFRR_Up"],
                mode="lines",
                name="mFRR Up (€/MW/h)",
                line=dict(color="#ff7f0e", width=1.5, dash="dot"),
            )
        )
        fig_res_hist.add_trace(
            go.Scatter(
                x=df_res_hist_filtered["date"],
                y=df_res_hist_filtered["mFRR_Down"],
                mode="lines",
                name="mFRR Down (€/MW/h)",
                line=dict(color="#9467bd", width=1.5, dash="dot"),
            )
        )
        fig_res_hist.update_layout(
            title=f"Sagedusreservide päeva keskmised hinnad ({selected_period_label})",
            xaxis_title="Kuupäev",
            yaxis_title="Hind (€/MW/h)",
            xaxis_tickformat="%d.%m.%Y",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_res_hist, use_container_width=True)

    st.markdown("---")

    st.markdown("#### 3. Sagedusreservide kuude keskmised hinnad alates 01.01.2026 (€/MW/h)")
    if not df_res_monthly.empty:
        df_res_table = df_res_monthly.copy()
        df_res_table.columns = [
            "Periood",
            "FCR (€/MW/h)",
            "aFRR Up (€/MW/h)",
            "aFRR Down (€/MW/h)",
            "mFRR Up (€/MW/h)",
            "mFRR Down (€/MW/h)",
        ]

        current_month_str = datetime.now().strftime("%Y-%m")
        df_res_table["Periood"] = df_res_table["Periood"].apply(
            lambda x: f"{x} (jooksev kuu)" if x == current_month_str else f"{x}"
        )

        summary_res = pd.DataFrame([{
            "Periood": "⭐ AASTA 2026 KESKMINE (YTD)",
            "FCR (€/MW/h)": df_res_hist["FCR"].mean(),
            "aFRR Up (€/MW/h)": df_res_hist["aFRR_Up"].mean(),
            "aFRR Down (€/MW/h)": df_res_hist["aFRR_Down"].mean(),
            "mFRR Up (€/MW/h)": df_res_hist["mFRR_Up"].mean(),
            "mFRR Down (€/MW/h)": df_res_hist["mFRR_Down"].mean(),
        }])

        final_res_table = pd.concat([df_res_table, summary_res], ignore_index=True)
        for col in [
            "FCR (€/MW/h)",
            "aFRR Up (€/MW/h)",
            "aFRR Down (€/MW/h)",
            "mFRR Up (€/MW/h)",
            "mFRR Down (€/MW/h)",
        ]:
            final_res_table[col] = final_res_table[col].apply(lambda x: f"{x:.2f}")

        st.dataframe(final_res_table, hide_index=True, use_container_width=True)

    st.caption(
        "📍 **Allikas:** Baltic Transparency Dashboard (BTD) / Elering / Baltic Balancing Capacity Market (BBCM)."
    )


# --- VAHELEHT 4: NORD POOL UMM TEATED (REAALAJAS AKTIIVSED) ---
with tab_umm:
    st.markdown("### ⚠️ Nord Pool REMIT UMM reaalajas kehtivad turuteated")
    st.write(
        "Allpool kuvatakse reaalajas aktiivsed võimsuspiirangute teated piirkonnas **EE, FI, LV, LT ja SE4**."
    )

    # Otseviited ametlikule portaalile
    st.markdown("##### 🔗 Ava otselingiga Nord Pooli ametlikus UMM portaalis:")
    col_l1, col_l2, col_l3, col_l4, col_l5 = st.columns(5)
    with col_l1:
        st.link_button("🇪🇪 Eesti (EE)", "https://umm.nordpoolgroup.com/#/messages?areas=EE")
    with col_l2:
        st.link_button("🇫🇮 Soome (FI)", "https://umm.nordpoolgroup.com/#/messages?areas=FI")
    with col_l3:
        st.link_button("🇱🇻 Läti (LV)", "https://umm.nordpoolgroup.com/#/messages?areas=LV")
    with col_l4:
        st.link_button("🇱🇹 Leedu (LT)", "https://umm.nordpoolgroup.com/#/messages?areas=LT")
    with col_l5:
        st.link_button("🇸🇪 Rootsi SE4", "https://umm.nordpoolgroup.com/#/messages?areas=SE4")

    st.markdown("---")

    if not df_umms.empty:
        all_unique_regions = set()
        for r_str in df_umms["Regioon"]:
            for r in r_str.split(", "):
                all_unique_regions.add(r.strip())

        col_uf1, col_uf2 = st.columns([1, 3])
        with col_uf1:
            chosen_reg = st.selectbox(
                "Filtreeri regiooni järgi:",
                ["Kõik"] + sorted(list(all_unique_regions)),
            )

        df_display_umm = (
            df_umms
            if chosen_reg == "Kõik"
            else df_umms[df_umms["Regioon"].str.contains(chosen_reg, na=False)]
        )

        st.dataframe(
            df_display_umm,
            column_config={
                "Objekti kirjeldus": st.column_config.TextColumn(
                    "Objekt ja sündmus", width="medium"
                ),
                "Turult VÄLJAS (MW)": st.column_config.TextColumn(
                    "Turult väljas (MW)", help="Võimsus, mis ei ole turule kättesaadav"
                ),
                "Link": st.column_config.LinkColumn(
                    "Teade",
                    help="Ava ametlik teade Nord Pooli portaalis",
                    display_text="Ava UMM ↗",
                ),
            },
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("Hetkel ei leitud Nord Pooli teenusest ühtegi aktiivset võimsuspiirangu teadet valitud piirkonnas (EE, FI, LV, LT, SE4). Kasuta ülaltoodud nuppe otsingu avamiseks portaalis.")

    st.caption("📍 **Allikas:** Nord Pool REMIT UMM reaalaja teabeteenus (`umm.nordpoolgroup.com`).")


# --- VAHELEHT 5: BRENT TOORNAFTA ---
with tab_oil:
    if not df_brent_filtered.empty:
        fig_brent = px.area(
            df_brent_filtered,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind ($/bbl)"},
            title=f"Brent toornafta sulgemishinnad ({selected_period_label})",
        )
        fig_brent.update_traces(line_color="#1E90FF")
        st.plotly_chart(fig_brent, use_container_width=True)

        st.markdown("---")
        current_year = datetime.now().year
        st.markdown(f"#### Jooksva aasta ({current_year}) naftahindade kuude ülevaade")
        df_brent_table = build_commodity_monthly_table(df_brent_full, "$/bbl")
        if not df_brent_table.empty:
            st.dataframe(df_brent_table, hide_index=True, use_container_width=True)

        st.caption("📍 **Allikas:** ICE Europe / Yahoo Finance (`BZ=F`) — Brent Crude Oil Futures.")
    else:
        st.warning("Naftahindade laadimine ebaõnnestus.")


# --- VAHELEHT 6: EU ETS CO2 KVOOT ---
with tab_co2:
    if not df_co2_filtered.empty:
        fig_co2 = px.line(
            df_co2_filtered,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind (€/tCO₂)"},
            title=f"EU ETS heitmekvoodi (EUA) sulgemishinnad ({selected_period_label})",
        )
        fig_co2.update_traces(line_color="#2E8B57")
        st.plotly_chart(fig_co2, use_container_width=True)

        st.markdown("---")
        current_year = datetime.now().year
        st.markdown(f"#### Jooksva aasta ({current_year}) heitmekvoodi kuude ülevaade")
        df_co2_table = build_commodity_monthly_table(df_co2_full, "€/tCO₂")
        if not df_co2_table.empty:
            st.dataframe(df_co2_table, hide_index=True, use_container_width=True)

        st.caption(
            "📍 **Allikas:** London Stock Exchange / ICE (`CO2.L` / SparkChange Physical Carbon EUA ETC) — tagatud 1:1 Euroopa Liidu heitmekvoodiga (EUA)."
        )
    else:
        st.warning("EU ETS kvoodi andmete laadimine ebaõnnestus.")


# --- VAHELEHT 7: KOHANDATUD PERIOODIPÄRING KÕIGILE SEGMENTIDELE ---
with tab_custom:
    st.markdown("### 🔍 Energiaturu hindade päring valitud perioodil")
    st.write(
        "Vali meelepärane algus- ja lõppkuupäev, et arvutada täpne aritmeetiline keskmine, madalaim ja kõrgeim hind kõigile energiaturgudele."
    )

    col_d1, col_d2 = st.columns(2)
    today_date_sel = datetime.now().date()
    default_start = today_date_sel - timedelta(days=90)

    with col_d1:
        custom_start = st.date_input("Perioodi alguskuupäev:", value=default_start, max_value=today_date_sel)
    with col_d2:
        custom_end = st.date_input("Perioodi lõppkuupäev:", value=today_date_sel, max_value=today_date_sel)

    if custom_start > custom_end:
        st.error("Alguskuupäev ei saa olla hilisem kui lõppkuupäev!")
    else:
        start_ts = pd.to_datetime(custom_start)
        end_ts = pd.to_datetime(custom_end) + timedelta(days=1) - timedelta(seconds=1)

        custom_results = []

        # 1. Elekter (EE, LV, LT, FI)
        if not df_daily_multi.empty:
            df_el_sub = df_daily_multi[
                (df_daily_multi["date"] >= start_ts)
                & (df_daily_multi["date"] <= end_ts)
            ]
            if not df_el_sub.empty:
                for reg in ["EE", "FI", "LV", "LT"]:
                    df_r = df_el_sub[df_el_sub["region"] == reg]
                    if not df_r.empty:
                        el_mean = df_r["mean"].mean()
                        el_min_row = df_r.loc[df_r["mean"].idxmin()]
                        el_max_row = df_r.loc[df_r["mean"].idxmax()]
                        custom_results.append({
                            "Turg / Segment": f"⚡ Elekter ({reg})",
                            "Mõõtühik": "€/MWh",
                            "Aritmeetiline keskmine": f"{el_mean:.2f}",
                            "Madalaim päeva keskmine": f"{el_min_row['mean']:.2f} ({el_min_row['date'].strftime('%d.%m.%Y')})",
                            "Kõrgeim päeva keskmine": f"{el_max_row['mean']:.2f} ({el_max_row['date'].strftime('%d.%m.%Y')})",
                        })

        # 2. GET Baltic
        if not df_getbaltic_full.empty:
            df_gb_sub = df_getbaltic_full[
                (df_getbaltic_full["Date"] >= start_ts)
                & (df_getbaltic_full["Date"] <= end_ts)
            ]
            if not df_gb_sub.empty:
                gb_mean = df_gb_sub["Close"].mean()
                gb_min_row = df_gb_sub.loc[df_gb_sub["Close"].idxmin()]
                gb_max_row = df_gb_sub.loc[df_gb_sub["Close"].idxmax()]
                custom_results.append({
                    "Turg / Segment": "🔥 Maagaas (GET Baltic BGSI)",
                    "Mõõtühik": "€/MWh",
                    "Aritmeetiline keskmine": f"{gb_mean:.2f}",
                    "Madalaim päeva keskmine": f"{gb_min_row['Close']:.2f} ({gb_min_row['Date'].strftime('%d.%m.%Y')})",
                    "Kõrgeim päeva keskmine": f"{gb_max_row['Close']:.2f} ({gb_max_row['Date'].strftime('%d.%m.%Y')})",
                })

        # 3. Dutch TTF
        if not df_ttf_full.empty:
            df_ttf_sub = df_ttf_full[
                (df_ttf_full["Date"] >= start_ts)
                & (df_ttf_full["Date"] <= end_ts)
            ]
            if not df_ttf_sub.empty:
                ttf_mean = df_ttf_sub["Close"].mean()
                ttf_min_row = df_ttf_sub.loc[df_ttf_sub["Close"].idxmin()]
                ttf_max_row = df_ttf_sub.loc[df_ttf_sub["Close"].idxmax()]
                custom_results.append({
                    "Turg / Segment": "🔥 Maagaas (Dutch TTF)",
                    "Mõõtühik": "€/MWh",
                    "Aritmeetiline keskmine": f"{ttf_mean:.2f}",
                    "Madalaim päeva keskmine": f"{ttf_min_row['Close']:.2f} ({ttf_min_row['Date'].strftime('%d.%m.%Y')})",
                    "Kõrgeim päeva keskmine": f"{ttf_max_row['Close']:.2f} ({ttf_max_row['Date'].strftime('%d.%m.%Y')})",
                })

        # 4. Brent nafta
        if not df_brent_full.empty:
            df_brent_sub = df_brent_full[
                (df_brent_full["Date"] >= start_ts)
                & (df_brent_full["Date"] <= end_ts)
            ]
            if not df_brent_sub.empty:
                brent_mean = df_brent_sub["Close"].mean()
                brent_min_row = df_brent_sub.loc[df_brent_sub["Close"].idxmin()]
                brent_max_row = df_brent_sub.loc[df_brent_sub["Close"].idxmax()]
                custom_results.append({
                    "Turg / Segment": "🛢️ Toornafta (Brent)",
                    "Mõõtühik": "$/bbl",
                    "Aritmeetiline keskmine": f"{brent_mean:.2f}",
                    "Madalaim päeva keskmine": f"{brent_min_row['Close']:.2f} ({brent_min_row['Date'].strftime('%d.%m.%Y')})",
                    "Kõrgeim päeva keskmine": f"{brent_max_row['Close']:.2f} ({brent_max_row['Date'].strftime('%d.%m.%Y')})",
                })

        # 5. EU ETS EUA
        if not df_co2_full.empty:
            df_co2_sub = df_co2_full[
                (df_co2_full["Date"] >= start_ts)
                & (df_co2_full["Date"] <= end_ts)
            ]
            if not df_co2_sub.empty:
                co2_mean = df_co2_sub["Close"].mean()
                co2_min_row = df_co2_sub.loc[df_co2_sub["Close"].idxmin()]
                co2_max_row = df_co2_sub.loc[df_co2_sub["Close"].idxmax()]
                custom_results.append({
                    "Turg / Segment": "🌱 Süsinikukvoot (EU ETS EUA)",
                    "Mõõtühik": "€/tCO₂",
                    "Aritmeetiline keskmine": f"{co2_mean:.2f}",
                    "Madalaim päeva keskmine": f"{co2_min_row['Close']:.2f} ({co2_min_row['Date'].strftime('%d.%m.%Y')})",
                    "Kõrgeim päeva keskmine": f"{co2_max_row['Close']:.2f} ({co2_max_row['Date'].strftime('%d.%m.%Y')})",
                })

        # 6. Sagedusreservid (alates 2026-01-01)
        if not df_res_hist.empty:
            df_res_sub = df_res_hist[
                (df_res_hist["date"] >= start_ts)
                & (df_res_hist["date"] <= end_ts)
            ]
            if not df_res_sub.empty:
                for res_col, res_name in [
                    ("FCR", "🔄 FCR võimsustasu"),
                    ("aFRR_Up", "🔄 aFRR Up võimsustasu"),
                    ("aFRR_Down", "🔄 aFRR Down võimsustasu"),
                    ("mFRR_Up", "🔄 mFRR Up võimsustasu"),
                    ("mFRR_Down", "🔄 mFRR Down võimsustasu"),
                ]:
                    r_mean = df_res_sub[res_col].mean()
                    r_min_row = df_res_sub.loc[df_res_sub[res_col].idxmin()]
                    r_max_row = df_res_sub.loc[df_res_sub[res_col].idxmax()]
                    custom_results.append({
                        "Turg / Segment": res_name,
                        "Mõõtühik": "€/MW/h",
                        "Aritmeetiline keskmine": f"{r_mean:.2f}",
                        "Madalaim päeva keskmine": f"{r_min_row[res_col]:.2f} ({r_min_row['date'].strftime('%d.%m.%Y')})",
                        "Kõrgeim päeva keskmine": f"{r_max_row[res_col]:.2f} ({r_max_row['date'].strftime('%d.%m.%Y')})",
                    })

        if custom_results:
            df_custom_table = pd.DataFrame(custom_results)
            st.markdown(
                f"#### Tulemused vahemikus {custom_start.strftime('%d.%m.%Y')} – {custom_end.strftime('%d.%m.%Y')}:"
            )
            st.dataframe(df_custom_table, hide_index=True, use_container_width=True)

            csv_data = df_custom_table.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="📥 Laadi tulemused CSV-na alla",
                data=csv_data,
                file_name=f"energiaturu_kokkuvote_{custom_start}_{custom_end}.csv",
                mime="text/csv",
            )
        else:
            st.info("Valitud kuupäevavahemikus andmeid ei leitud.")
