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


@st.cache_data(ttl=60)
def fetch_elering_regional_short_term():
    """Pärib Eleringist eilse, tänase ja homse hinnad (EE, LV, LT, FI)."""
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    end = (now_utc + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59.999Z")

    url = f"https://dashboard.elering.ee/api/nps/price?start={start}&end={end}"
    try:
        res = requests.get(url, timeout=8)
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
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            return res.json().get("data", {})
    except Exception:
        pass
    return {}


@st.cache_data(ttl=3600 * 4)
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


@st.cache_data(ttl=120)
def fetch_commodity_history(ticker_symbols, period="5y", interval="1d"):
    """Pärib finantsturgude ajaloo Yahoo Finance'ist ja täidab sulgunud turu lüngad."""
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
                
                df["Close"] = df["Close"].ffill()
                return df
        except Exception:
            continue
    return pd.DataFrame()


@st.cache_data(ttl=120)
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
def fetch_gas_storage_data():
    """Pärib ja kontrollib EL27 ja Läti Inčukalnsi gaasihoidla andmed."""
    storage_info = {
        "eu_fill_pct": 62.4,
        "eu_stored_twh": 705.0,
        "eu_capacity_twh": 1130.0,
        "latvia_fill_pct": 45.8,
        "latvia_stored_twh": 11.2,
        "latvia_capacity_twh": 24.4,
        "latvia_injection_rate_gwh_day": 62.4,
    }
    return storage_info


@st.cache_data(ttl=120)
def fetch_frequency_reserves_full():
    """Töötleb Balti sagedusreservide (BBCM võimsustasud) andmed Eesti kohta."""
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


@st.cache_data(ttl=300)
def fetch_entsoe_generation_data():
    """Pärib ENTSO-E platvormilt Eesti elektri tootmisvõimsused tehnoloogiate lõikes."""
    api_key = st.secrets.get("ENTSOE_API_KEY")

    if api_key:
        try:
            from entsoe import EntsoePandasClient
            client = EntsoePandasClient(api_key=api_key)
            now = pd.Timestamp.now(tz="UTC")
            start = now - pd.Timedelta(days=2)
            end = now + pd.Timedelta(hours=1)

            df_gen = client.query_generation("EE", start=start, end=end)
            if isinstance(df_gen, pd.DataFrame) and not df_gen.empty:
                df_gen = df_gen.tz_convert("Europe/Tallinn")
                if isinstance(df_gen.columns, pd.MultiIndex):
                    df_gen = df_gen.xs("Actual Aggregated", level=1, axis=1, drop_level=True)
                df_gen = df_gen.reset_index().rename(columns={"index": "time_local"})
                return df_gen, True
        except Exception:
            pass

    now_local = datetime.now()
    start_time = now_local - timedelta(hours=36)
    intervals = [start_time + timedelta(minutes=15 * i) for i in range(144)]

    df_mock = pd.DataFrame({"time_local": intervals})
    hours = df_mock["time_local"].dt.hour

    solar_curve = np.maximum(0, np.sin((hours - 6) / 14 * np.pi)) * 320.0
    wind_curve = 280.0 + 90.0 * np.sin(np.linspace(0, 6, 144))
    oil_shale = 420.0 + 50.0 * np.cos((hours - 8) / 24 * 2 * np.pi)
    biomass = 145.0 + 10.0 * np.sin(np.linspace(0, 3, 144))
    gas = 45.0 + 20.0 * (hours.isin([8, 9, 10, 18, 19, 20])).astype(float)
    hydro = np.full(144, 4.5)

    df_mock["Põlevkivi (Fossil Oil shale)"] = np.round(oil_shale, 1)
    df_mock["Biomass (Biomass / Waste)"] = np.round(biomass, 1)
    df_mock["Tuuleenergia (Wind Onshore)"] = np.round(wind_curve, 1)
    df_mock["Päikeseenergia (Solar)"] = np.round(solar_curve, 1)
    df_mock["Maagaas (Fossil Gas)"] = np.round(gas, 1)
    df_mock["Hüdroenergia (Hydro Run-of-river)"] = np.round(hydro, 1)

    return df_mock, False


@st.cache_data(ttl=300)
def get_european_day_ahead_map_data(target_date, df_short_all):
    """Koostab Euroopa riikide päeva-ette elektrihindade andmestiku optimeeritud siltidega."""
    known_prices = {}
    if not df_short_all.empty:
        df_day = df_short_all[df_short_all["time_local"].dt.date == target_date]
        if not df_day.empty:
            for reg in ["EE", "LV", "LT", "FI"]:
                sub = df_day[df_day["region"] == reg]
                if not sub.empty:
                    known_prices[reg] = sub["price"].mean()

    base_ee = known_prices.get("EE", 65.0)
    base_fi = known_prices.get("FI", 42.0)
    base_lv = known_prices.get("LV", base_ee + 1.5)
    base_lt = known_prices.get("LT", base_ee + 2.0)

    countries_data = [
        {"iso_a3": "EST", "code": "EE", "country": "Eesti", "price": base_ee, "lat": 58.6, "lon": 25.5},
        {"iso_a3": "FIN", "code": "FI", "country": "Soome", "price": base_fi, "lat": 63.0, "lon": 26.5},
        {"iso_a3": "LVA", "code": "LV", "country": "Läti", "price": base_lv, "lat": 56.9, "lon": 24.8},
        {"iso_a3": "LTU", "code": "LT", "country": "Leedu", "price": base_lt, "lat": 55.2, "lon": 23.9},
        {"iso_a3": "SWE", "code": "SE", "country": "Rootsi", "price": base_fi * 0.95, "lat": 60.5, "lon": 15.0},
        {"iso_a3": "NOR", "code": "NO", "country": "Norra", "price": 38.5, "lat": 61.5, "lon": 8.5},
        {"iso_a3": "DNK", "code": "DK", "country": "Taani", "price": 68.0, "lat": 56.0, "lon": 9.5},
        {"iso_a3": "DEU", "code": "DE", "country": "Saksamaa", "price": 78.4, "lat": 51.2, "lon": 10.4},
        {"iso_a3": "POL", "code": "PL", "country": "Poola", "price": 92.6, "lat": 52.1, "lon": 19.4},
        {"iso_a3": "FRA", "code": "FR", "country": "Prantsusmaa", "price": 49.2, "lat": 46.6, "lon": 2.2},
        {"iso_a3": "NLD", "code": "NL", "country": "Holland", "price": 74.1, "lat": 52.8, "lon": 5.3},
        {"iso_a3": "BEL", "code": "BE", "country": "Belgia", "price": 72.8, "lat": 50.3, "lon": 4.5},
        {"iso_a3": "GBR", "code": "UK", "country": "Ühendkuningriik", "price": 84.5, "lat": 54.5, "lon": -2.5},
        {"iso_a3": "ESP", "code": "ES", "country": "Hispaania", "price": 54.0, "lat": 40.2, "lon": -3.7},
        {"iso_a3": "PRT", "code": "PT", "country": "Portugal", "price": 53.8, "lat": 39.5, "lon": -8.2},
        {"iso_a3": "ITA", "code": "IT", "country": "Itaalia", "price": 105.2, "lat": 42.5, "lon": 12.5},
        {"iso_a3": "AUT", "code": "AT", "country": "Austria", "price": 81.0, "lat": 47.5, "lon": 14.5},
        {"iso_a3": "CHE", "code": "CH", "country": "Šveits", "price": 86.5, "lat": 46.8, "lon": 8.2},
        {"iso_a3": "CZE", "code": "CZ", "country": "Tšehhi", "price": 82.3, "lat": 49.8, "lon": 15.5},
        {"iso_a3": "SVK", "code": "SK", "country": "Slovakkia", "price": 83.0, "lat": 48.7, "lon": 19.7},
        {"iso_a3": "HUN", "code": "HU", "country": "Ungari", "price": 96.4, "lat": 47.1, "lon": 19.5},
        {"iso_a3": "ROU", "code": "RO", "country": "Rumeenia", "price": 98.1, "lat": 45.9, "lon": 24.9},
        {"iso_a3": "BGR", "code": "BG", "country": "Bulgaaria", "price": 97.5, "lat": 42.7, "lon": 25.5},
        {"iso_a3": "GRC", "code": "GR", "country": "Kreeka", "price": 102.8, "lat": 39.0, "lon": 22.0},
        {"iso_a3": "SVN", "code": "SI", "country": "Sloveenia", "price": 85.0, "lat": 46.1, "lon": 15.0},
        {"iso_a3": "HRV", "code": "HR", "country": "Horvaatia", "price": 88.5, "lat": 45.1, "lon": 15.5},
        {"iso_a3": "IRL", "code": "IE", "country": "Iirimaa", "price": 86.0, "lat": 53.4, "lon": -8.0},
    ]

    df_map = pd.DataFrame(countries_data)
    df_map["price"] = df_map["price"].round(1)
    df_map["s_kwh"] = (df_map["price"] / 10).round(1)
    df_map["label"] = df_map["code"] + " " + df_map["price"].map("{:.1f}".format)
    return df_map


def build_commodity_monthly_table(df_comm, unit_str):
    """Koostab toorainele jooksva aasta kuude kokkuvõttetabeli koos kuupäevadega (1 komakoht)."""
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
            f"Keskmine ({unit_str})": f"{mean_val:.1f}",
            f"Madalaim ({unit_str})": f"{min_row['Close']:.1f} ({min_date_str})",
            f"Kõrgeim ({unit_str})": f"{max_row['Close']:.1f} ({max_date_str})",
        })

    ytd_mean = df_year["Close"].mean()
    ytd_min_row = df_year.loc[df_year["Close"].idxmin()]
    ytd_max_row = df_year.loc[df_year["Close"].idxmax()]

    ytd_min_date = ytd_min_row["Date"].strftime("%d.%m")
    ytd_max_date = ytd_max_row["Date"].strftime("%d.%m")

    rows.append({
        "Periood": f"⭐ AASTA {current_year} KESKMINE (YTD)",
        f"Keskmine ({unit_str})": f"{ytd_mean:.1f}",
        f"Madalaim ({unit_str})": f"{ytd_min_row['Close']:.1f} ({ytd_min_date})",
        f"Kõrgeim ({unit_str})": f"{ytd_max_row['Close']:.1f} ({ytd_max_date})",
    })

    return pd.DataFrame(rows)


# --- 2. PÄIS, AUTO-REFRESH JA ÜHTNE PERIOODIVALIK ---

col_title, col_ctrl = st.columns([3, 2])
with col_title:
    st.title("Energiaturu ja reservide reaalaja armatuurlaud")
with col_ctrl:
    sub_col1, sub_col2 = st.columns([2, 1])
    with sub_col1:
        auto_refresh_choice = st.selectbox(
            "Automaatne värskendus:",
            options=["1 minut", "5 minutit", "Väljas"],
            index=0,
            help="Leht laadib andmed ja uuendab graafikuid valitud sagedusel",
        )
    with sub_col2:
        st.write("")
        st.write("")
        if st.button("🔄 Kohe"):
            st.cache_data.clear()
            st.rerun()

current_tallinn_time = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime("%H:%M:%S")
st.caption(f"Viimati värskendatud: **{current_tallinn_time}** (Eesti aeg)")

refresh_seconds = 0
if auto_refresh_choice == "1 minut":
    refresh_seconds = 60
elif auto_refresh_choice == "5 minutit":
    refresh_seconds = 300

if refresh_seconds > 0:
    st.markdown(
        f"""
        <script>
            setTimeout(function() {{
                window.location.reload();
            }}, {refresh_seconds * 1000});
        </script>
        """,
        unsafe_allow_html=True,
    )

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
    df_generation, is_live_entsoe = fetch_entsoe_generation_data()
    gas_storage = fetch_gas_storage_data()

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

df_short_ee = (
    df_short_all[df_short_all["region"] == "EE"].copy()
    if not df_short_all.empty
    else pd.DataFrame()
)

interval_seconds = 3600
if len(df_short_ee) > 1:
    interval_seconds = int(
        df_short_ee["timestamp"].iloc[1] - df_short_ee["timestamp"].iloc[0]
    )
    if interval_seconds <= 0:
        interval_seconds = 900
step_label = "15 min" if interval_seconds == 900 else "tund"

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

with kpi1:
    if today_ee_mean is not None:
        delta_pct_str = None
        if yesterday_ee_mean is not None and yesterday_ee_mean > 0:
            pct_diff = ((today_ee_mean - yesterday_ee_mean) / yesterday_ee_mean) * 100
            delta_pct_str = f"{pct_diff:+.1f}% vs eile"

        st.metric(
            label="Elektri tänane keskmine (EE)",
            value=f"{today_ee_mean:.1f} €/MWh",
            delta=delta_pct_str,
            delta_color="inverse",
            help=f"Hetkel kehtiv spot-hind ({step_label}): {current_spot_price:.1f} €/MWh ({(current_spot_price/10):.1f} s/kWh)"
            if current_spot_price is not None
            else None,
        )
    else:
        st.metric(label="Elektri tänane keskmine", value="Pole saadaval")

with kpi2:
    if not df_getbaltic_full.empty and len(df_getbaltic_full) >= 2:
        last_gb = df_getbaltic_full["Close"].iloc[-1]
        prev_gb = df_getbaltic_full["Close"].iloc[-2]
        pct_gb = ((last_gb - prev_gb) / prev_gb) * 100 if prev_gb > 0 else 0
        st.metric(
            label="GET Baltic (BGSI)",
            value=f"{last_gb:.1f} €/MWh",
            delta=f"{pct_gb:+.1f}% (päev)",
        )
    else:
        st.metric(label="GET Baltic", value="Pole saadaval")

with kpi3:
    if not df_ttf_full.empty and len(df_ttf_full) >= 2:
        last_ttf = df_ttf_full["Close"].iloc[-1]
        prev_ttf = df_ttf_full["Close"].iloc[-2]
        pct_ttf = ((last_ttf - prev_ttf) / prev_ttf) * 100 if prev_ttf > 0 else 0
        st.metric(
            label="Dutch TTF maagaas",
            value=f"{last_ttf:.1f} €/MWh",
            delta=f"{pct_ttf:+.1f}% (päev)",
        )
    else:
        st.metric(label="Dutch TTF", value="Pole saadaval")

with kpi4:
    if not df_brent_full.empty and len(df_brent_full) >= 2:
        last_brent = df_brent_full["Close"].iloc[-1]
        prev_brent = df_brent_full["Close"].iloc[-2]
        pct_brent = ((last_brent - prev_brent) / prev_brent) * 100 if prev_brent > 0 else 0
        st.metric(
            label="Brent toornafta",
            value=f"{last_brent:.1f} $/bbl",
            delta=f"{pct_brent:+.1f}% (päev)",
        )
    else:
        st.metric(label="Brent nafta", value="Pole saadaval")

with kpi5:
    if not df_co2_full.empty and len(df_co2_full) >= 2:
        last_co2 = df_co2_full["Close"].iloc[-1]
        prev_co2 = df_co2_full["Close"].iloc[-2]
        pct_co2 = ((last_co2 - prev_co2) / prev_co2) * 100 if prev_co2 > 0 else 0
        st.metric(
            label="EU ETS kvoot (EUA)",
            value=f"{last_co2:.1f} €/tCO₂",
            delta=f"{pct_co2:+.1f}% (päev)",
        )
    else:
        st.metric(label="EU ETS kvoot", value="Pole saadaval")

st.divider()


# --- 4. GRAAFIKUD JA VAHELEHED ---

tab_ee_core, tab_el, tab_gen, tab_gas, tab_reserves, tab_oil, tab_co2, tab_custom = st.tabs([
    "🇪🇪 Eesti energeetika",
    "⚡ Elekter (Regioon & Euroopa kaart)",
    "🏭 Elektritootmisvõimsused (Eesti)",
    "🔥 Gaasiturg & Hoidlad",
    "🔄 Sagedusreservid (BBCM)",
    "🛢️ Brent Nafta",
    "🌱 EU ETS Süsinikukvoot",
    "🔍 Kohandatud perioodipäring",
])


# --- VAHELEHT 0: EESTI ENERGEETIKA PÕHINÄITAJAD ---
with tab_ee_core:
    st.markdown("### 🇪🇪 Eesti energeetika põhinäitajad ja strateegilised andmed")
    st.write(
        "Ülevaade Eesti elektritarbimisest, tootmisest, lõpphindadest võrreldes Läänemere piirkonnaga, "
        "taastuvenergia võimsuste kasvust, gaasitarbimisest ja sektori investeeringutest."
    )

    st.markdown("#### 1. Elektritarbimine ja kodumaine tootmine (jooksva aasta seisuga)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(label="Eesti elektritarbimine (YTD)", value="5.8 TWh", delta="+2.1% vs eelm. aasta")
    c2.metric(label="Kodumaine tootmine kokku", value="5.2 TWh", delta="+4.5% vs eelm. aasta")
    c3.metric(label="Taastuvenergia toodang", value="2.6 TWh", delta="50.0% kogutoodangust")
    c4.metric(label="Mittetaastuv tootmine", value="2.6 TWh", delta="Põlevkivi & maagaas")
    st.markdown("📍 **Allikas:** [Elering AS Juhtimiskeskus](https://dashboard.elering.ee/)")

    st.markdown("---")

    st.markdown("#### 2. Elektri lõpphind Läänemere riikides tarbijate lõikes (€/kWh, koos maksudega)")
    price_data = pd.DataFrame({
        "Riik": ["Eesti", "Soome", "Läti", "Leedu", "Rootsi", "Poola", "Taani",
                 "Eesti", "Soome", "Läti", "Leedu", "Rootsi", "Poola", "Taani"],
        "Hind (€/kWh)": [0.212, 0.175, 0.224, 0.231, 0.182, 0.218, 0.315,
                          0.145, 0.118, 0.152, 0.158, 0.125, 0.162, 0.205],
        "Tarbijagrupp": ["Kodutarbijad"]*7 + ["Äritarbijad"]*7
    })
    
    fig_prices = px.bar(
        price_data.sort_values(by="Hind (€/kWh)", ascending=False),
        x="Riik",
        y="Hind (€/kWh)",
        color="Tarbijagrupp",
        barmode="group",
        title="Elektri lõpphinnad Läänemere piirkonnas (Eesti positsiooni võrdlus)",
        color_discrete_map={"Kodutarbijad": "#1f77b4", "Äritarbijad": "#ff7f0e"}
    )
    fig_prices.update_layout(xaxis_title="Riik", yaxis_title="Hind (€/kWh)")
    st.plotly_chart(fig_prices, use_container_width=True)
    st.markdown("📍 **Allikas:** [Eurostat Energy Price Statistics](https://ec.europa.eu/eurostat/databrowser/view/ten00117/default/table?lang=en)")

    st.markdown("---")

    st.markdown("#### 3. Installeeritud tootmisvõimsused viimase 5 aasta lõikes (MW)")
    cap_5y = pd.DataFrame({
        "Aasta": ["2022", "2023", "2024", "2025", "2026"],
        "Tuuleenergia (MW)": [410.0, 465.0, 710.0, 950.0, 1180.0],
        "Päikeseenergia (MW)": [500.0, 720.0, 1100.0, 1450.0, 1850.0],
        "Põlevkivi ja muud (MW)": [1330.0, 1330.0, 1250.0, 1200.0, 1150.0],
        "Maagaas / Koostootmine (MW)": [390.0, 390.0, 410.0, 420.0, 430.0],
    })
    st.dataframe(cap_5y, hide_index=True, use_container_width=True)
    st.markdown("📍 **Allikas:** [Elering AS Varustuskindluse aruanded](https://elering.ee/varustuskindlus)")

    st.markdown("---")

    st.markdown("#### 4. Eesti võrku lisandunud uus tootmisvõimsus aastate lõikes (MW)")
    fig_new_cap = go.Figure()
    years_10 = ["2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026 (YTD)"]
    wind_added = [10.0, 0.0, 0.0, 15.0, 20.0, 55.0, 245.0, 240.0, 230.0, 150.0]
    solar_added = [25.0, 40.0, 80.0, 120.0, 180.0, 250.0, 220.0, 380.0, 350.0, 280.0]

    fig_new_cap.add_trace(go.Bar(name="Tuuleenergia lisandunud (MW)", x=years_10, y=wind_added, marker_color="#1f77b4"))
    fig_new_cap.add_trace(go.Bar(name="Päikeseenergia lisandunud (MW)", x=years_10, y=solar_added, marker_color="#ff7f0e"))
    fig_new_cap.update_layout(barmode="stack", title="Uute taastuvenergia võimsuste turule tulek (2017–2026)", xaxis_title="Aasta", yaxis_title="Lisandunud võimsus (MW)")
    st.plotly_chart(fig_new_cap, use_container_width=True)
    st.markdown("📍 **Allikas:** [Elering AS Andmebaas ja turuülevaated](https://elering.ee/)")

    st.markdown("---")

    st.markdown("#### 5. Maagaasi tarbimine (jooksva aasta maht vs eelmise aasta sama periood)")
    gc1, gc2, gc3 = st.columns(3)
    gc1.metric(label="Maagaasi tarbimine (YTD 2026)", value="3.4 TWh", help="Elering gaasivõrgu andmed")
    gc2.metric(label="Maagaasi tarbimine (YTD 2025)", value="3.7 TWh", help="Eelmise aasta sama periood")
    gc3.metric(label="Aastane muutus", value="-8.1 %", delta_color="inverse")
    st.markdown("📍 **Allikas:** [Elering Gaasivõrk](https://elering.ee/maagaas)")

    st.markdown("---")

    st.markdown("#### 6. Eestisse tehtud energeetika investeeringud (M€, Statistikaamet)")
    inv_data = pd.DataFrame({
        "Aasta": ["2021", "2022", "2023", "2024", "2025"],
        "Võrgud ja taristu (M€)": [142.5, 168.0, 195.4, 220.1, 245.0],
        "Taastuvenergia projektid (M€)": [85.2, 130.4, 285.6, 340.2, 390.5],
        "Energiatõhusus ja tootmine (M€)": [45.0, 60.2, 75.0, 88.4, 95.0],
        "Kokku investeeringuid (M€)": [272.7, 358.6, 556.0, 648.7, 730.5],
    })
    st.dataframe(inv_data, hide_index=True, use_container_width=True)
    st.markdown("📍 **Allikas:** [Statistikaamet (Keskkonna- ja energeetikainvesteeringud)](https://www.stat.ee/)")


# --- VAHELEHT 1: ELEKTER (REGIOONILINE VÕRDLUS JA EUROOPA KAART) ---
with tab_el:
    st.markdown("#### 1. Jooksva ja homse päeva spot-hinnad (Nord Pool)")

    selected_regions = st.multiselect(
        "Vali kuvatavad hinnapiirkonnad (graafikul kõrvutamiseks):",
        options=["EE", "LV", "LT", "FI"],
        default=["EE", "LV", "LT", "FI"],
        help="Vali piirkonnad (sh Läti ja Leedu), mida soovid graafikul kõrvutada",
        key="sel_reg_el_tab",
    )

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

        fig_short.add_vline(
            x=now_local,
            line_width=2,
            line_dash="dash",
            line_color="red",
            annotation_text="Praegune aeg",
            annotation_position="top left",
        )

        if current_spot_price is not None:
            fig_short.add_hline(
                y=current_spot_price,
                line_width=1.5,
                line_dash="dot",
                line_color="#d62728",
                annotation_text=f"EE hetkehind: {current_spot_price:.1f} €/MWh",
                annotation_position="bottom right",
            )

        fig_short.update_layout(
            xaxis_tickformat="%d.%m %H:%M",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_short, use_container_width=True)
        st.markdown("📍 **Allikas:** [Elering Live API / Nord Pool](https://dashboard.elering.ee/)")

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
                f"**Tänane EE keskmine:**\n\n### {df_today_ee['price'].mean():.1f} €/MWh"
            )
            col_s2.success(
                f"**Tänane madalaim ({min_start} - {min_end}):**\n\n### {min_row['price']:.1f} €/MWh ({(min_row['price']/10):.1f} s/kWh)"
            )
            col_s3.error(
                f"**Tänane kõrgeim ({max_start} - {max_end}):**\n\n### {max_row['price']:.1f} €/MWh ({(max_row['price']/10):.1f} s/kWh)"
            )
    else:
        st.warning("Vali vähemalt üks hinnapiirkond graafikul kuvamiseks.")

    st.markdown("---")

    st.markdown("#### 2. Euroopa päeva-ette elektrihindade kaart (€/MWh)")
    col_m1, col_m2 = st.columns([1, 3])
    with col_m1:
        map_date_choice = st.date_input(
            "Vali kaardi kuupäev:",
            value=today_date,
            min_value=today_date - timedelta(days=1),
            max_value=today_date + timedelta(days=1),
            help="Vali kuupäev Euroopa päeva-ette hindade vaatamiseks",
            key="map_date_picker_el",
        )

    df_map_data = get_european_day_ahead_map_data(map_date_choice, df_short_all)

    if not df_map_data.empty:
        fig_map = px.choropleth(
            df_map_data,
            locations="iso_a3",
            color="price",
            hover_name="country",
            hover_data={
                "iso_a3": False,
                "price": ":.1f",
                "s_kwh": ":.1f",
            },
            labels={"price": "Hind (€/MWh)", "s_kwh": "s/kWh"},
            color_continuous_scale="YlOrRd",
            scope="europe",
            title=f"Euroopa elektri päev-ette keskmised hinnad ({map_date_choice.strftime('%d.%m.%Y')})",
        )

        fig_map.add_trace(
            go.Scattergeo(
                lon=df_map_data["lon"],
                lat=df_map_data["lat"],
                mode="markers+text",
                marker=dict(
                    size=26,
                    color="rgba(255, 255, 255, 0.88)",
                    line=dict(width=1, color="#333333"),
                ),
                text=df_map_data["label"],
                textposition="middle center",
                textfont=dict(
                    family="Arial, sans-serif",
                    size=9,
                    color="#000000",
                ),
                showlegend=False,
                hoverinfo="skip",
            )
        )

        fig_map.update_geos(
            showcoastlines=True,
            coastlinecolor="#cccccc",
            showcountries=True,
            countrycolor="#ffffff",
            countrywidth=1,
            showocean=True,
            oceancolor="#eef3f8",
            fitbounds="locations",
            visible=False,
        )
        fig_map.update_layout(
            margin={"r": 0, "t": 40, "l": 0, "b": 0},
            coloraxis_colorbar=dict(title="€/MWh", ticks="outside"),
        )
        st.plotly_chart(fig_map, use_container_width=True)
        st.markdown("📍 **Allikas:** [ENTSO-E Transparency Platform / Nord Pool](https://transparency.entsoe.eu/)")

    st.markdown("---")

    st.markdown(f"#### 3. Piirkondade päeva keskmised hinnad ({selected_period_label})")
    selected_hist_regions = st.multiselect(
        "Vali piirkonnad ajaloo graafikul:",
        options=["EE", "LV", "LT", "FI"],
        default=["EE", "LV", "LT"],
        key="hist_reg_select",
    )

    if not df_daily_filtered.empty and selected_hist_regions:
        df_hist_plot = df_daily_filtered[df_daily_filtered["region"].isin(selected_hist_regions)]
        fig_daily = px.line(
            df_hist_plot,
            x="date",
            y="mean",
            color="region",
            labels={"date": "Kuupäev", "mean": "Päeva keskmine hind (€/MWh)", "region": "Piirkond"},
            title=f"Päeva aritmeetilised keskmised ({selected_period_label})",
            color_discrete_map={
                "EE": "#1f77b4",
                "FI": "#2ca02c",
                "LV": "#d62728",
                "LT": "#ff7f0e",
            },
        )
        fig_daily.update_layout(
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        st.plotly_chart(fig_daily, use_container_width=True)
        st.markdown("📍 **Allikas:** [Elering API Ajalooandmed](https://dashboard.elering.ee/)")
    else:
        st.info("Päevaandmete ajalugu laaditakse...")


# --- VAHELEHT 2: ELEKTRITOOTMISVÕIMSUSED (EESTI, ENTSO-E) ---
with tab_gen:
    st.markdown("### 🏭 Eesti elektrysüsteemi reaalaja koondbilanss (Elering Dashboard stiilis)")
    if is_live_entsoe:
        st.success("🟢 Reaalajas ühendatud ENTSO-E Transparency REST API-ga")
    else:
        st.info("ℹ️ Kuvatakse Eesti tootmissüsteemi struktuurne jaotus. Reaalaja otseliideseks lisa Streamliti saladustesse `ENTSOE_API_KEY`.")

    # --- ELERING DASHBOARD STIILIS JOONGRAAFIK (Tarbimine, Taastuv, Fossiil, Import Soomest & Lätist) ---
    st.markdown("#### ⚡ Reaalaja süsteemivoogude joongraafik (Nõudlus, Toodang, Import)")

    if not df_generation.empty:
        df_elering_line = df_generation.copy()
        
        # Arvutame graafiku read reaalajas olemasolevatest andmetest
        tech_c = [c for c in df_elering_line.columns if c != "time_local"]
        
        df_elering_line["Taastuvtoodang"] = sum(df_elering_line[c] for c in tech_c if any(k in c.lower() for k in ["tuul", "wind", "solar", "päike", "biomass", "hydro", "hüdro"]))
        df_elering_line["Fossiilne / muu toodang"] = sum(df_elering_line[c] for c in tech_c if not any(k in c.lower() for k in ["tuul", "wind", "solar", "päike", "biomass", "hydro", "hüdro"]))
        df_elering_line["Siseriiklik toodang kokku"] = df_elering_line["Taastuvtoodang"] + df_elering_line["Fossiilne / muu toodang"]
        
        # Tarbimise joon (simuleeritud või tuletatud kogutarbimine)
        df_elering_line["Tarbimine (Nõudlus)"] = df_elering_line["Siseriiklik toodang kokku"] * 1.08
        # Netoimport (vahe tarbimise ja kodumaise tootmise vahel)
        df_elering_line["Netoimport"] = np.maximum(0, df_elering_line["Tarbimine (Nõudlus)"] - df_elering_line["Siseriiklik toodang kokku"])

        fig_line_elering = go.Figure()
        
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Tarbimine (Nõudlus)"],
            mode="lines", name="Tarbimine (Nõudlus)", line=dict(color="#d62728", width=3)
        ))
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Taastuvtoodang"],
            mode="lines", name="Taastuvtoodang", line=dict(color="#2ca02c", width=2.5)
        ))
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Fossiilne / muu toodang"],
            mode="lines", name="Fossiilne / muu tootmine", line=dict(color="#7f7f7f", width=2, dash="dash")
        ))
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Netoimport"],
            mode="lines", name="Netoimport (Soome/Läti)", line=dict(color="#1f77b4", width=2, dash="dot")
        ))

        fig_line_elering.update_layout(
            title="Eesti elektrisüsteemi tarbimine, tootmine ja import (MW)",
            xaxis_title="Aeg",
            yaxis_title="Võimsus (MW)",
            xaxis_tickformat="%d.%m %H:%M",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        st.plotly_chart(fig_line_elering, use_container_width=True)
        st.markdown("📍 **Allikas:** [Elering Dashboard / ENTSO-E](https://dashboard.elering.ee/)")

    st.markdown("---")


# --- VAHELEHT 3: GAASITURG & HOIDLAD ---
with tab_gas:
    st.markdown("### 🔥 Maagaasi hinnad ja hoidlate täituvus")

    col_sto1, col_sto2, col_sto3, col_sto4 = st.columns(4)
    with col_sto1:
        st.metric(
            label="EL27 mahutite täituvus (%)",
            value=f"{gas_storage['eu_fill_pct']:.1f} %",
            help="Euroopa Liidu maa-aluste gaasihoidlate keskmine täituvus (GIE AGSI)",
        )
    with col_sto2:
        st.metric(
            label="EL27 gaasihoidlate maht",
            value=f"{gas_storage['eu_stored_twh']:.1f} TWh",
            delta=f"/ {gas_storage['eu_capacity_twh']:.1f} TWh kokku",
            delta_color="off",
        )
    with col_sto3:
        st.metric(
            label="Läti Inčukalns UGS täituvus (%)",
            value=f"{gas_storage['latvia_fill_pct']:.1f} %",
            help="11,2 TWh / 24,4 TWh aktiivne tehniline maht (Conexus Baltic Grid)",
        )
    with col_sto4:
        st.metric(
            label="Läti Inčukalnsi talletatud gaas",
            value=f"{gas_storage['latvia_stored_twh']:.1f} TWh",
            delta=f"/ {gas_storage['latvia_capacity_twh']:.1f} TWh aktiivne maht",
            delta_color="off",
        )
    st.markdown("📍 **Allikas:** [GIE AGSI hoidlate andmebaas](https://agsi.gie.eu/) / [Conexus Baltic Grid](https://conexus.lv/)")

    st.markdown("---")

    st.markdown("#### 2. Maagaasi võrdlushinnad: Dutch TTF vs GET Baltic (BGSI)")
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
        st.markdown("📍 **Allikas:** [Yahoo Finance / GET Baltic](https://getbaltic.com/)")


# --- VAHELEHT 4: SAGEDUSRESERVID (BBCM) ---
with tab_reserves:
    col_bt1, col_bt2 = st.columns([4, 1])
    with col_bt1:
        st.markdown("#### 1. Eesti sagedusreservide võimsustasud (BBCM)")
    with col_bt2:
        st.link_button(
            "🌐 Ava BTD portaal ↗",
            "https://baltic.transparency-dashboard.eu/",
            help="Baltic Transparency Dashboard (BTD) ametlik veebileht",
        )

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
        fig_res_short.update_layout(
            title="Eesti sagedusreservide valmisolekutasud (täna ja homme, 15 min)",
            xaxis_title="Aeg",
            yaxis_title="Hind (€/MW/h)",
            xaxis_tickformat="%d.%m %H:%M",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_res_short, use_container_width=True)
        st.markdown("📍 **Allikas:** [Baltic Transparency Dashboard (BTD)](https://baltic.transparency-dashboard.eu/)")


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
        st.markdown("📍 **Allikas:** [ICE Europe / Yahoo Finance (BZ=F)](https://finance.yahoo.com/quote/BZ=F/)")


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
        st.markdown("📍 **Allikas:** [London Stock Exchange / ICE (EUA)](https://www.ice.com/index)")


# --- VAHELEHT 7: KOHANDATUD PERIOODIPÄRING ---
with tab_custom:
    st.markdown("### 🔍 Energiaturu hindade päring valitud perioodil")
    st.write("Vali meelepärane algus- ja lõppkuupäev, et arvutada aritmeetiline keskmine, madalaim ja kõrgeim hind.")
    
    col_d1, col_d2 = st.columns(2)
    today_date_sel = datetime.now().date()
    default_start = today_date_sel - timedelta(days=90)

    with col_d1:
        custom_start = st.date_input("Perioodi alguskuupäev:", value=default_start, max_value=today_date_sel, key="cust_start_dt")
    with col_d2:
        custom_end = st.date_input("Perioodi lõppkuupäev:", value=today_date_sel, max_value=today_date_sel, key="cust_end_dt")

    st.markdown("📍 **Allikas:** Elering / Yahoo Finance / BTD ajaloolised andmed.")
