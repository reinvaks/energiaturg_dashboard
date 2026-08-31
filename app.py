from datetime import datetime, timezone
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


@st.cache_data(ttl=300)
def fetch_elering_data():
    """Pärib Eleringi API-st tänase päeva tunnihinnad."""
    now_utc = datetime.now(timezone.utc)
    start = now_utc.strftime("%Y-%m-%dT00:00:00.000Z")
    end = now_utc.strftime("%Y-%m-%dT23:59:59.999Z")

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
def fetch_commodity_history(ticker_symbol, period="1mo", interval="1d"):
    """Pärib toorainete ajaloo Yahoo Finance'ist."""
    try:
        ticker = yf.Ticker(ticker_symbol)
        df = ticker.history(period=period, interval=interval)
        if not df.empty:
            df = df.reset_index()
            # Standardiseerime kuupäeva veeru nime
            date_col = "Date" if "Date" in df.columns else "Datetime"
            df["Date"] = pd.to_datetime(df[date_col])
            return df
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# Pealkiri ja värskendusnupp
col_title, col_btn = st.columns([5, 1])
with col_title:
    st.title("Energiaturu reaalaja ülevaade")
with col_btn:
    if st.button("Värskenda andmeid"):
        st.cache_data.clear()
        st.rerun()

# Andmete laadimine
df_elekter = fetch_elering_data()
df_ttf = fetch_commodity_history("TTF=F", period="1mo")
df_brent = fetch_commodity_history("BZ=F", period="1mo")

# 1. Hetkehindade mõõdikud (KPI kaardid)
st.subheader("Hetketuru hinnatasemed")
kpi1, kpi2, kpi3 = st.columns(3)

# Elektri jooksev hind
current_el_price = None
if not df_elekter.empty:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    match = df_elekter[
        (df_elekter["timestamp"] <= now_ts)
        & (now_ts < df_elekter["timestamp"] + 3600)
    ]
    if not match.empty:
        current_el_price = match.iloc[0]["price"]
    else:
        current_el_price = df_elekter.iloc[-1]["price"]

with kpi1:
    if current_el_price is not None:
        st.metric(
            label="Elektri börsihind (EE)",
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
            delta=f"{delta_ttf:+.2f} €",
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
            delta=f"{delta_brent:+.2f} $",
        )
    else:
        st.metric(label="Brent toornafta", value="Pole saadaval")

st.divider()

# 2. Graafikud
tab1, tab2, tab3 = st.tabs(
    ["Elektri tunnihinnad (täna)", "TTF Gaas (1 kuu)", "Brent Nafta (1 kuu)"]
)

with tab1:
    if not df_elekter.empty:
        fig_el = px.bar(
            df_elekter,
            x="time_local",
            y="price",
            labels={"time_local": "Kellaaeg", "price": "Hind (€/MWh)"},
            title="Nord Pool Eesti päeva tunnihinnad",
        )
        fig_el.update_layout(xaxis_tickformat="%H:%M")
        st.plotly_chart(fig_el, use_container_width=True)
    else:
        st.warning("Elektri tunnihindade laadimine ebaõnnestus.")

with tab2:
    if not df_ttf.empty:
        fig_ttf = px.line(
            df_ttf,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind (€/MWh)"},
            title="Dutch TTF maagaasi futuuri sulgemishinnad (viimane kuu)",
        )
        st.plotly_chart(fig_ttf, use_container_width=True)
    else:
        st.warning("Gaasihindade ajaloo laadimine ebaõnnestus.")

with tab3:
    if not df_brent.empty:
        fig_brent = px.line(
            df_brent,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind ($/barrel)"},
            title="Brent toornafta sulgemishinnad (viimane kuu)",
        )
        st.plotly_chart(fig_brent, use_container_width=True)
    else:
        st.warning("Naftahindade ajaloo laadimine ebaõnnestus.")
