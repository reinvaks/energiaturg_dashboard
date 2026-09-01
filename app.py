from datetime import datetime, timedelta, timezone
import pandas as pd
import plotly.express as px
import requests
import streamlit as st
import yfinance as yf

# Lehe seadistus
st.set_page_config(
    page_title="Energiaturu armatuurlaud",
    page_icon="⚡",
    layout="wide",
)


@st.cache_data(ttl=180)
def fetch_elering_data():
    """Pärib Eleringi API-st elektrihinnad (15-min või 1h resolutsiooniga)."""
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(days=0)).strftime("%Y-%m-%dT00:00:00.000Z")
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


@st.cache_data(ttl=900)
def fetch_commodity_history(ticker_symbols, period="1y", interval="1d"):
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
                return df
        except Exception:
            continue
    return pd.DataFrame()


# Päise riba
col_title, col_btn = st.columns([5, 1])
with col_title:
    st.title("Energiaturu reaalaja armatuurlaud")
with col_btn:
    if st.button("🔄 Värskenda"):
        st.cache_data.clear()
        st.rerun()

# Perioodi valik
period_map = {
    "1 nädal": "7d",
    "1 kuu": "1mo",
    "3 kuud": "3mo",
    "6 kuud": "6mo",
    "12 kuud": "1y",
}
selected_period_label = st.segmented_control(
    "Ajaloo periood:",
    options=list(period_map.keys()),
    default="12 kuud",
)
selected_period = period_map[selected_period_label]

# Andmete laadimine
df_elekter = fetch_elering_data()
df_ttf = fetch_commodity_history(["TTF=F"], period=selected_period)
df_brent = fetch_commodity_history(["BZ=F"], period=selected_period)
df_co2 = fetch_commodity_history(
    ["CO2.L", "CARB.L", "KEUA"], period=selected_period
)

# Tuvastame elektri andmete sammu (15 min = 900s, 1h = 3600s)
interval_seconds = 3600
if len(df_elekter) > 1:
    interval_seconds = int(
        df_elekter["timestamp"].iloc[1] - df_elekter["timestamp"].iloc[0]
    )
    if interval_seconds <= 0:
        interval_seconds = 900

# --- 1. MÕÕDIKUTE KAARDID (KPI) ---
st.subheader("Hetketuru hinnatasemed")
kpi1, kpi2, kpi3, kpi4 = st.columns(4)

# Elektri jooksev 15-min / hetkehind
current_el_price = None
if not df_elekter.empty:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    match = df_elekter[
        (df_elekter["timestamp"] <= now_ts)
        & (now_ts < df_elekter["timestamp"] + interval_seconds)
    ]
    if not match.empty:
        current_el_price = match.iloc[0]["price"]
    else:
        current_el_price = df_elekter.iloc[-1]["price"]

step_label = "15 min" if interval_seconds == 900 else "tund"

with kpi1:
    if current_el_price is not None:
        st.metric(
            label=f"Elektri börsihind (EE, jooksev {step_label})",
            value=f"{current_el_price:.2f} €/MWh",
            delta=f"{(current_el_price / 10):.2f} s/kWh",
            delta_color="off",
        )
    else:
        st.metric(label="Elektri börsihind (EE)", value="Pole saadaval")

# TTF Maagaas
with kpi2:
    if not df_ttf.empty:
        last_ttf = df_ttf["Close"].iloc[-1]
        prev_ttf = (
            df_ttf["Close"].iloc[-2] if len(df_ttf) > 1 else last_ttf
        )
        delta_ttf = last_ttf - prev_ttf
        st.metric(
            label="Dutch TTF maagaas",
            value=f"{last_ttf:.2f} €/MWh",
            delta=f"{delta_ttf:+.2f} € (päev)",
        )
    else:
        st.metric(label="Dutch TTF maagaas", value="Pole saadaval")

# Brent toornafta
with kpi3:
    if not df_brent.empty:
        last_brent = df_brent["Close"].iloc[-1]
        prev_brent = (
            df_brent["Close"].iloc[-2] if len(df_brent) > 1 else last_brent
        )
        delta_brent = last_brent - prev_brent
        st.metric(
            label="Brent toornafta",
            value=f"{last_brent:.2f} $/bbl",
            delta=f"{delta_brent:+.2f} $ (päev)",
        )
    else:
        st.metric(label="Brent toornafta", value="Pole saadaval")

# EU ETS EUA CO2 kvoot
with kpi4:
    if not df_co2.empty:
        last_co2 = df_co2["Close"].iloc[-1]
        prev_co2 = (
            df_co2["Close"].iloc[-2] if len(df_co2) > 1 else last_co2
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

# --- 2. GRAAFIKUD ---
tab_el, tab_gas, tab_oil, tab_co2 = st.tabs([
    "⚡ Elektrihinnad (15 min / täna + homme)",
    "🔥 TTF Gaas",
    "🛢️ Brent Nafta",
    "🌱 EU ETS Heitmekvoot (EUA)",
])

with tab_el:
    if not df_elekter.empty:
        fig_el = px.bar(
            df_elekter,
            x="time_local",
            y="price",
            color="price",
            color_continuous_scale="Turbo",
            labels={
                "time_local": "Aeg (Eesti kohalik)",
                "price": "Hind (€/MWh)",
            },
            title=f"Nord Pool Eesti hinnad ({step_label} resolutsiooniga)",
        )
        now_local = datetime.now(timezone.utc).astimezone(
            tz=df_elekter["time_local"].dt.tz
        )
        fig_el.add_vline(
            x=now_local,
            line_width=2,
            line_dash="dash",
            line_color="red",
            annotation_text="Praegu",
            annotation_position="top left",
        )
        fig_el.update_layout(
            xaxis_tickformat="%d.%m %H:%M", coloraxis_showscale=False
        )
        st.plotly_chart(fig_el, use_container_width=True)

        # Statistika arvutamine täpse intervalliga (15 min või 1h)
        step_minutes = interval_seconds // 60
        avg_price = df_elekter["price"].mean()

        min_row = df_elekter.loc[df_elekter["price"].idxmin()]
        min_start = min_row["time_local"].strftime("%d.%m kell %H:%M")
        min_end = (
            min_row["time_local"] + timedelta(minutes=step_minutes)
        ).strftime("%H:%M")

        max_row = df_elekter.loc[df_elekter["price"].idxmax()]
        max_start = max_row["time_local"].strftime("%d.%m kell %H:%M")
        max_end = (
            max_row["time_local"] + timedelta(minutes=step_minutes)
        ).strftime("%H:%M")

        col_s1, col_s2, col_s3 = st.columns(3)
        col_s1.info(f"**Päeva keskmine hind:**\n\n### {avg_price:.2f} €/MWh")
        col_s2.success(
            f"**Madalaim intervall ({min_start} - {min_end}):**\n\n### {min_row['price']:.2f} €/MWh ({(min_row['price']/10):.2f} s/kWh)"
        )
        col_s3.error(
            f"**Kõrgeim intervall ({max_start} - {max_end}):**\n\n### {max_row['price']:.2f} €/MWh ({(max_row['price']/10):.2f} s/kWh)"
        )
    else:
        st.warning("Elektrihindade laadimine ebaõnnestus.")

with tab_gas:
    if not df_ttf.empty:
        fig_ttf = px.area(
            df_ttf,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind (€/MWh)"},
            title=f"Dutch TTF maagaasi futuur ({selected_period_label})",
        )
        fig_ttf.update_traces(line_color="#FF8C00")
        st.plotly_chart(fig_ttf, use_container_width=True)
    else:
        st.warning("Gaasihindade laadimine ebaõnnestus.")

with tab_oil:
    if not df_brent.empty:
        fig_brent = px.area(
            df_brent,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind ($/bbl)"},
            title=f"Brent toornafta sulgemishinnad ({selected_period_label})",
        )
        fig_brent.update_traces(line_color="#1E90FF")
        st.plotly_chart(fig_brent, use_container_width=True)
    else:
        st.warning("Naftahindade laadimine ebaõnnestus.")

with tab_co2:
    if not df_co2.empty:
        fig_co2 = px.line(
            df_co2,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind (€/tCO₂)"},
            title=f"EU ETS heitmekvoodi (EUA) sulgemishinnad ({selected_period_label})",
        )
        fig_co2.update_traces(line_color="#2E8B57")
        st.plotly_chart(fig_co2, use_container_width=True)
    else:
        st.warning("EU ETS kvoodi andmete laadimine ebaõnnestus.")
