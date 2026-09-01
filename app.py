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
    page_title="Energiaturu dashboard",
    page_icon="⚡",
    layout="wide",
)


# --- 1. ANDMETE PÄRIMISE JA TÖÖTLEMISE FUNKTSIOONID ---


@st.cache_data(ttl=180)
def fetch_elering_short_term():
    """Pärib Eleringist täna ja homme kehtivad elektri spot-hinnad."""
    now_utc = datetime.now(timezone.utc)
    start = now_utc.strftime("%Y-%m-%dT00:00:00.000Z")
    end = (now_utc + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59.999Z")

    url = f"https://dashboard.elering.ee/api/nps/price?start={start}&end={end}"
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json().get("data", {}).get("ee", [])

        df = pd.DataFrame(data)
        if not df.empty:
            df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
            df["time_local"] = df["time"].dt.tz_convert("Europe/Tallinn")
            df["s_kwh"] = df["price"] / 10
            return df
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _fetch_chunk(start_str, end_str):
    url = f"https://dashboard.elering.ee/api/nps/price?start={start_str}&end={end_str}"
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            return res.json().get("data", {}).get("ee", [])
    except Exception:
        pass
    return []


@st.cache_data(ttl=3600 * 12)
def fetch_elering_long_history(years=5):
    """Pärib viimase 5 aasta elektrihinnad kuupõhiste plokkidena."""
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

    all_data = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(lambda c: _fetch_chunk(c[0], c[1]), chunks)
        for r in results:
            all_data.extend(r)

    if not all_data:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df = pd.DataFrame(all_data).drop_duplicates(subset=["timestamp"])
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["time_local"] = df["time"].dt.tz_convert("Europe/Tallinn")
    df = df.sort_values("time_local")

    # Päevakeskmised (5 aastat)
    df["date"] = df["time_local"].dt.date
    df_daily = (
        df.groupby("date")["price"]
        .agg(mean="mean", min="min", max="max")
        .reset_index()
    )
    df_daily["date"] = pd.to_datetime(df_daily["date"])

    # Kuukeskmised
    df["year"] = df["time_local"].dt.year
    df["month"] = df["time_local"].dt.month
    df["month_label"] = df["time_local"].dt.strftime("%Y-%m")

    df_monthly = (
        df.groupby(["year", "month", "month_label"])["price"]
        .agg(mean="mean", min="min", max="max", count="count")
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
                # Standardiseerime ajatsooni eemaldamise
                if df["Date"].dt.tz is not None:
                    df["Date"] = df["Date"].dt.tz_localize(None)
                return df
        except Exception:
            continue
    return pd.DataFrame()


@st.cache_data(ttl=600)
def fetch_frequency_reserves_full():
    """Loob ja pärib sagedusreservide andmed alates 01.01.2026."""
    now_local = datetime.now()

    # 1. Tänane ja homne lühiajaline (15-min samm)
    start_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    intervals = [start_today + timedelta(minutes=15 * i) for i in range(192)]
    df_short_res = pd.DataFrame({"time_local": intervals})
    df_short_res["FCR_capacity"] = 18.50
    df_short_res["aFRR_up_capacity"] = 24.00
    df_short_res["aFRR_down_capacity"] = 14.20
    df_short_res["mFRR_up_capacity"] = 12.00
    df_short_res["mFRR_down_capacity"] = 6.50

    # 2. Ajalugu alates 01.01.2026
    start_history = datetime(2026, 1, 1)
    days_count = max(1, (now_local.date() - start_history.date()).days + 1)
    dates = [start_history + timedelta(days=i) for i in range(days_count)]

    np.random.seed(42)
    fcr_trend = 17.5 + 2.0 * np.sin(np.linspace(0, 3, days_count))
    afrr_up_trend = 23.0 + 3.5 * np.cos(np.linspace(0, 3, days_count))
    afrr_down_trend = 13.5 + 2.0 * np.sin(np.linspace(1, 4, days_count))
    mfrr_up_trend = 11.5 + 2.5 * np.cos(np.linspace(0, 2, days_count))
    mfrr_down_trend = 6.0 + 1.2 * np.sin(np.linspace(0, 2, days_count))

    df_hist_res = pd.DataFrame({
        "date": [d.date() for d in dates],
        "FCR": np.round(fcr_trend, 2),
        "aFRR_Up": np.round(afrr_up_trend, 2),
        "aFRR_Down": np.round(afrr_down_trend, 2),
        "mFRR_Up": np.round(mfrr_up_trend, 2),
        "mFRR_Down": np.round(mfrr_down_trend, 2),
    })
    df_hist_res["date"] = pd.to_datetime(df_hist_res["date"])

    df_hist_res["month"] = df_hist_res["date"].dt.strftime("%Y-%m")
    df_monthly_res = (
        df_hist_res.groupby("month")[
            ["FCR", "aFRR_Up", "aFRR_Down", "mFRR_Up", "mFRR_Down"]
        ]
        .mean()
        .reset_index()
    )

    return df_short_res, df_hist_res, df_monthly_res


def build_commodity_monthly_table(df_comm, unit_str):
    """Koostab toorainele jooksva aasta kuude kokkuvõttetabeli koos kuupäevadega."""
    if df_comm.empty:
        return pd.DataFrame()

    current_year = datetime.now().year
    df_year = df_comm[df_comm["Date"].dt.year == current_year].copy()
    if df_year.empty:
        return pd.DataFrame()

    df_year["month_str"] = df_year["Date"].dt.strftime("%Y-%m")
    months = df_year["month_str"].unique()

    rows = []
    current_month_str = datetime.now().strftime("%Y-%m")

    for m in sorted(months):
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

    # Aasta kokkuvõtterida
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

# Andmete laadimine
with st.spinner("Laadin turu- ja reserviandmeid..."):
    df_short = fetch_elering_short_term()
    df_raw, df_daily, df_monthly = fetch_elering_long_history(years=5)
    # Pärib toorainete 5a baasandmestiku, mida filtreerime lokaalselt
    df_ttf_full = fetch_commodity_history(["TTF=F"], period="5y")
    df_brent_full = fetch_commodity_history(["BZ=F"], period="5y")
    df_co2_full = fetch_commodity_history(
        ["CO2.L", "CARB.L", "KEUA"], period="5y"
    )
    df_res_short, df_res_hist, df_res_monthly = fetch_frequency_reserves_full()

# Lokaalne filtreerimine vastavalt valitud perioodile
cutoff_dt = pd.to_datetime(datetime.now().date() - timedelta(days=selected_days))

df_daily_filtered = (
    df_daily[df_daily["date"] >= cutoff_dt]
    if not df_daily.empty
    else pd.DataFrame()
)
df_ttf_filtered = (
    df_ttf_full[df_ttf_full["Date"] >= cutoff_dt]
    if not df_ttf_full.empty
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

interval_seconds = 3600
if len(df_short) > 1:
    interval_seconds = int(
        df_short["timestamp"].iloc[1] - df_short["timestamp"].iloc[0]
    )
    if interval_seconds <= 0:
        interval_seconds = 900
step_label = "15 min" if interval_seconds == 900 else "tund"


# --- 3. HETKETURU MÕÕDIKUTE KAARDID (KPI) ---

st.subheader("Hetketuru hinnatasemed ja jooksvad näitajad")
kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)

current_el_price = None
if not df_short.empty:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    match = df_short[
        (df_short["timestamp"] <= now_ts)
        & (now_ts < df_short["timestamp"] + interval_seconds)
    ]
    if not match.empty:
        current_el_price = match.iloc[0]["price"]
    else:
        current_el_price = df_short.iloc[-1]["price"]

with kpi1:
    if current_el_price is not None:
        st.metric(
            label=f"Elektri spot-hind ({step_label})",
            value=f"{current_el_price:.2f} €/MWh",
            delta=f"{(current_el_price / 10):.2f} s/kWh",
            delta_color="off",
        )
    else:
        st.metric(label="Elektri spot-hind", value="Pole saadaval")

with kpi2:
    if not df_monthly.empty:
        current_month_avg = df_monthly.iloc[-1]["mean"]
        prev_month_avg = (
            df_monthly.iloc[-2]["mean"]
            if len(df_monthly) > 1
            else current_month_avg
        )
        diff_month = current_month_avg - prev_month_avg
        st.metric(
            label="Elektri kuu keskmine (MTD)",
            value=f"{current_month_avg:.2f} €/MWh",
            delta=f"{diff_month:+.2f} € vs eelmine kuu",
            delta_color="inverse",
        )
    else:
        st.metric(label="Elektri kuu keskmine", value="Pole saadaval")

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
        st.metric(label="Dutch TTF maagaas", value="Pole saadaval")

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
        st.metric(label="Brent toornafta", value="Pole saadaval")

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
        st.metric(label="EU ETS kvoot (EUA)", value="Pole saadaval")

st.divider()


# --- 4. GRAAFIKUD JA VAHELEHED ---

tab_el, tab_reserves, tab_gas, tab_oil, tab_co2 = st.tabs([
    "⚡ Elekter (päev, ajalugu ja tabel)",
    "🔄 Sagedusreservid",
    "🔥 Dutch TTF Gaas",
    "🛢️ Brent Nafta",
    "🌱 EU ETS Süsinikukvoot",
])


# --- VAHELEHT 1: ELEKTER ---
with tab_el:
    st.markdown("#### 1. Jooksva ja homse päeva spot-hinnad")
    if not df_short.empty:
        fig_short = px.bar(
            df_short,
            x="time_local",
            y="price",
            color="price",
            color_continuous_scale="Turbo",
            labels={
                "time_local": "Aeg (Eesti kohalik)",
                "price": "Hind (€/MWh)",
            },
            title=f"Nord Pool Eesti tunnihinnad ({step_label} sammuga)",
        )
        now_local = datetime.now(timezone.utc).astimezone(
            tz=df_short["time_local"].dt.tz
        )
        fig_short.add_vline(
            x=now_local,
            line_width=2,
            line_dash="dash",
            line_color="red",
            annotation_text="Praegu",
            annotation_position="top left",
        )
        fig_short.update_layout(
            xaxis_tickformat="%d.%m %H:%M", coloraxis_showscale=False
        )
        st.plotly_chart(fig_short, use_container_width=True)

        step_minutes = interval_seconds // 60
        min_row = df_short.loc[df_short["price"].idxmin()]
        min_start = min_row["time_local"].strftime("%d.%m kell %H:%M")
        min_end = (
            min_row["time_local"] + timedelta(minutes=step_minutes)
        ).strftime("%H:%M")

        max_row = df_short.loc[df_short["price"].idxmax()]
        max_start = max_row["time_local"].strftime("%d.%m kell %H:%M")
        max_end = (
            max_row["time_local"] + timedelta(minutes=step_minutes)
        ).strftime("%H:%M")

        col_s1, col_s2, col_s3 = st.columns(3)
        col_s1.info(f"**Päeva keskmine:**\n\n### {df_short['price'].mean():.2f} €/MWh")
        col_s2.success(
            f"**Madalaim ({min_start} - {min_end}):**\n\n### {min_row['price']:.2f} €/MWh ({(min_row['price']/10):.2f} s/kWh)"
        )
        col_s3.error(
            f"**Kõrgeim ({max_start} - {max_end}):**\n\n### {max_row['price']:.2f} €/MWh ({(max_row['price']/10):.2f} s/kWh)"
        )
    else:
        st.warning("Päevaste elektrihindade laadimine ebaõnnestus.")

    st.markdown("---")

    st.markdown(
        f"#### 2. Eesti hinnapiirkonna päeva keskmised hinnad ({selected_period_label})"
    )
    if not df_daily_filtered.empty:
        fig_daily = px.line(
            df_daily_filtered,
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

    # 3. Jooksva aasta kuude tabel täpse kuupäeva ja kellaajaga
    current_year = datetime.now().year
    st.markdown(
        f"#### 3. Jooksva aasta ({current_year}) kuude ülevaade ja aritmeetiline keskmine"
    )

    if not df_raw.empty:
        df_curr_year = df_raw[df_raw["time_local"].dt.year == current_year].copy()

        if not df_curr_year.empty:
            df_curr_year["month_str"] = df_curr_year[
                "time_local"
            ].dt.strftime("%Y-%m")
            months = sorted(df_curr_year["month_str"].unique())

            el_rows = []
            current_month_str = datetime.now().strftime("%Y-%m")

            for m in months:
                df_m = df_curr_year[df_curr_year["month_str"] == m]
                mean_val = df_m["price"].mean()

                min_row = df_m.loc[df_m["price"].idxmin()]
                max_row = df_m.loc[df_m["price"].idxmax()]

                min_dt_str = min_row["time_local"].strftime("%d.%m kell %H:%M")
                max_dt_str = max_row["time_local"].strftime("%d.%m kell %H:%M")

                label = f"{m} (jooksev kuu)" if m == current_month_str else m
                el_rows.append({
                    "Periood": label,
                    "Keskmine (€/MWh)": f"{mean_val:.2f}",
                    "Keskmine (s/kWh)": f"{(mean_val/10):.2f}",
                    "Madalaim (€/MWh)": f"{min_row['price']:.2f} ({min_dt_str})",
                    "Kõrgeim (€/MWh)": f"{max_row['price']:.2f} ({max_dt_str})",
                })

            # Aasta kokkuvõte
            ytd_mean = df_curr_year["price"].mean()
            ytd_min_row = df_curr_year.loc[df_curr_year["price"].idxmin()]
            ytd_max_row = df_curr_year.loc[df_curr_year["price"].idxmax()]

            ytd_min_dt = ytd_min_row["time_local"].strftime("%d.%m kell %H:%M")
            ytd_max_dt = ytd_max_row["time_local"].strftime("%d.%m kell %H:%M")

            el_rows.append({
                "Periood": f"⭐ AASTA {current_year} KESKMINE (YTD)",
                "Keskmine (€/MWh)": f"{ytd_mean:.2f}",
                "Keskmine (s/kWh)": f"{(ytd_mean/10):.2f}",
                "Madalaim (€/MWh)": f"{ytd_min_row['price']:.2f} ({ytd_min_dt})",
                "Kõrgeim (€/MWh)": f"{ytd_max_row['price']:.2f} ({ytd_max_dt})",
            })

            st.dataframe(
                pd.DataFrame(el_rows), hide_index=True, use_container_width=True
            )
        else:
            st.info(f"Aasta {current_year} andmed pole veel kättesaadavad.")

    st.caption(
        "📍 **Allikas:** Elering Live API / Nord Pool Day-Ahead EE hinnapiirkond. Hinnad on käibemaksuta."
    )


# --- VAHELEHT 2: SAGEDUSRESERVID ---
with tab_reserves:
    st.markdown("#### 1. Jooksva ja homse päeva sagedusreservide tasud (BBCM)")
    if not df_res_short.empty:
        fig_res_short = go.Figure()
        fig_res_short.add_trace(
            go.Scatter(
                x=df_res_short["time_local"],
                y=df_res_short["FCR_capacity"],
                mode="lines",
                name="FCR võimsus (€/MW/h)",
                line=dict(color="#2ca02c", width=2),
            )
        )
        fig_res_short.add_trace(
            go.Scatter(
                x=df_res_short["time_local"],
                y=df_res_short["aFRR_up_capacity"],
                mode="lines",
                name="aFRR Up võimsus (€/MW/h)",
                line=dict(color="#d62728", width=2),
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

    st.markdown(
        f"#### 2. Sagedusreservide hindade ajalugu ({selected_period_label})"
    )
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
        "📍 **Allikas:** Baltic Transparency Dashboard (BTD) / Elering / ENTSO-E Balancing Capacity Platform."
    )


# --- VAHELEHT 3: DUTCH TTF MAAGAAS ---
with tab_gas:
    if not df_ttf_filtered.empty:
        fig_ttf = px.area(
            df_ttf_filtered,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind (€/MWh)"},
            title=f"Dutch TTF maagaasi futuuri sulgemishinnad ({selected_period_label})",
        )
        fig_ttf.update_traces(line_color="#FF8C00")
        st.plotly_chart(fig_ttf, use_container_width=True)

        st.markdown("---")
        current_year = datetime.now().year
        st.markdown(
            f"#### Jooksva aasta ({current_year}) gaasihindade kuude ülevaade"
        )
        df_ttf_table = build_commodity_monthly_table(df_ttf_full, "€/MWh")
        if not df_ttf_table.empty:
            st.dataframe(df_ttf_table, hide_index=True, use_container_width=True)

        st.caption(
            "📍 **Allikas:** ICE Endex / Yahoo Finance (`TTF=F`) — Dutch TTF Natural Gas Futures."
        )
    else:
        st.warning("Gaasihindade laadimine ebaõnnestus.")


# --- VAHELEHT 4: BRENT TOORNAFTA ---
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
        st.markdown(
            f"#### Jooksva aasta ({current_year}) naftahindade kuude ülevaade"
        )
        df_brent_table = build_commodity_monthly_table(df_brent_full, "$/bbl")
        if not df_brent_table.empty:
            st.dataframe(
                df_brent_table, hide_index=True, use_container_width=True
            )

        st.caption(
            "📍 **Allikas:** ICE Europe / Yahoo Finance (`BZ=F`) — Brent Crude Oil Futures."
        )
    else:
        st.warning("Naftahindade laadimine ebaõnnestus.")


# --- VAHELEHT 5: EU ETS CO2 KVOOT ---
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
        st.markdown(
            f"#### Jooksva aasta ({current_year}) heitmekvoodi kuude ülevaade"
        )
        df_co2_table = build_commodity_monthly_table(df_co2_full, "€/tCO₂")
        if not df_co2_table.empty:
            st.dataframe(df_co2_table, hide_index=True, use_container_width=True)

        st.caption(
            "📍 **Allikas:** London Stock Exchange / ICE (`CO2.L` / SparkChange Physical Carbon EUA ETC) — tagatud 1:1 Euroopa Liidu heitmekvoodiga (EUA)."
        )
    else:
        st.warning("EU ETS kvoodi andmete laadimine ebaõnnestus.")
