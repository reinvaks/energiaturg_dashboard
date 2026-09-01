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
def fetch_elering_short_term():
    """Pärib Eleringi API-st täna ja homme kehtivad hinnad (15-min / 1h täpsusega)."""
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


@st.cache_data(ttl=3600 * 12)  # Pikk ajalugu uueneb kord 12h jooksul
def fetch_elering_long_history(years=5):
    """Pärib Eleringist viimase 5 aasta elektrihinnad ja arvutab kuude ning päevade keskmised."""
    now_utc = datetime.now(timezone.utc)
    all_data = []

    # Pätime andmed aasta kaupa, et mitte ületada API päringu mahtu
    for y in range(years, -1, -1):
        start_dt = now_utc - timedelta(days=(y + 1) * 365)
        end_dt = now_utc - timedelta(days=y * 365)
        if y == 0:
            end_dt = now_utc + timedelta(days=1)

        start_str = start_dt.strftime("%Y-%m-%dT00:00:00.000Z")
        end_str = end_dt.strftime("%Y-%m-%dT23:59:59.999Z")

        url = f"https://dashboard.elering.ee/api/nps/price?start={start_str}&end={end_str}"
        try:
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                data = res.json().get("data", {}).get("ee", [])
                all_data.extend(data)
        except Exception:
            continue

    if not all_data:
        return pd.DataFrame(), pd.DataFrame()

    df = pd.DataFrame(all_data).drop_duplicates(subset=["timestamp"])
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["time_local"] = df["time"].dt.tz_convert("Europe/Tallinn")
    df = df.sort_values("time_local")

    # Kuude keskmised
    df["year_month"] = df["time_local"].dt.to_period("M").dt.to_timestamp()
    df_monthly = (
        df.groupby("year_month")["price"]
        .agg(mean="mean", min="min", max="max")
        .reset_index()
    )
    df_monthly["s_kwh"] = df_monthly["mean"] / 10

    # Päevade keskmised pikaks graafikuks
    df["date"] = df["time_local"].dt.date
    df_daily = (
        df.groupby("date")["price"]
        .agg(mean="mean", min="min", max="max")
        .reset_index()
    )
    df_daily["date"] = pd.to_datetime(df_daily["date"])

    return df_monthly, df_daily


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

# Perioodi valik toorainete graafikutele
period_map = {
    "1 nädal": "7d",
    "1 kuu": "1mo",
    "3 kuud": "3mo",
    "6 kuud": "6mo",
    "12 kuud": "1y",
    "5 aastat": "5y",
}
selected_period_label = st.segmented_control(
    "Ajaloo periood (toorained):",
    options=list(period_map.keys()),
    default="12 kuud",
)
selected_period = period_map[selected_period_label]

# Andmete laadimine
df_elekter_short = fetch_elering_short_term()
df_el_monthly, df_el_daily = fetch_elering_long_history(years=5)
df_ttf = fetch_commodity_history(["TTF=F"], period=selected_period)
df_brent = fetch_commodity_history(["BZ=F"], period=selected_period)
df_co2 = fetch_commodity_history(
    ["CO2.L", "CARB.L", "KEUA"], period=selected_period
)

# Intervalli pikkuse tuvastus
interval_seconds = 3600
if len(df_elekter_short) > 1:
    interval_seconds = int(
        df_elekter_short["timestamp"].iloc[1]
        - df_elekter_short["timestamp"].iloc[0]
    )
    if interval_seconds <= 0:
        interval_seconds = 900
step_label = "15 min" if interval_seconds == 900 else "tund"

# --- 1. MÕÕDIKUTE KAARDID (KPI) ---
st.subheader("Hetketuru ja jooksvad tasemed")
kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)

# 1. Elektri hetkehind
current_el_price = None
if not df_elekter_short.empty:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    match = df_elekter_short[
        (df_elekter_short["timestamp"] <= now_ts)
        & (now_ts < df_elekter_short["timestamp"] + interval_seconds)
    ]
    if not match.empty:
        current_el_price = match.iloc[0]["price"]
    else:
        current_el_price = df_elekter_short.iloc[-1]["price"]

with kpi1:
    if current_el_price is not None:
        st.metric(
            label=f"Elektri hetkehind ({step_label})",
            value=f"{current_el_price:.2f} €/MWh",
            delta=f"{(current_el_price / 10):.2f} s/kWh",
            delta_color="off",
        )
    else:
        st.metric(label="Elektri hetkehind", value="Pole saadaval")

# 2. Elektri jooksva kuu keskmine
with kpi2:
    if not df_el_monthly.empty:
        current_month_avg = df_el_monthly.iloc[-1]["mean"]
        prev_month_avg = (
            df_el_monthly.iloc[-2]["mean"]
            if len(df_el_monthly) > 1
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

# 3. TTF Maagaas
with kpi3:
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

# 4. Brent toornafta
with kpi4:
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

# 5. EU ETS EUA CO2 kvoot
with kpi5:
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
    "⚡ Elektrihinnad ja ajalugu (Eesti)",
    "🔥 TTF Gaas",
    "🛢️ Brent Nafta",
    "🌱 EU ETS Heitmekvoot (EUA)",
])

# Elektri tab: Lühiajaline + 5a Ajalugu ja Kuukeskmised
with tab_el:
    st.markdown("#### 1. Jooksva ja homse päeva hinnad")
    if not df_elekter_short.empty:
        fig_el_short = px.bar(
            df_elekter_short,
            x="time_local",
            y="price",
            color="price",
            color_continuous_scale="Turbo",
            labels={
                "time_local": "Aeg (Eesti kohalik)",
                "price": "Hind (€/MWh)",
            },
            title=f"Nord Pool Eesti hinnad ({step_label} sammuga)",
        )
        now_local = datetime.now(timezone.utc).astimezone(
            tz=df_elekter_short["time_local"].dt.tz
        )
        fig_el_short.add_vline(
            x=now_local,
            line_width=2,
            line_dash="dash",
            line_color="red",
            annotation_text="Praegu",
            annotation_position="top left",
        )
        fig_el_short.update_layout(
            xaxis_tickformat="%d.%m %H:%M", coloraxis_showscale=False
        )
        st.plotly_chart(fig_el_short, use_container_width=True)

        step_minutes = interval_seconds // 60
        avg_price = df_elekter_short["price"].mean()

        min_row = df_elekter_short.loc[df_elekter_short["price"].idxmin()]
        min_start = min_row["time_local"].strftime("%d.%m kell %H:%M")
        min_end = (
            min_row["time_local"] + timedelta(minutes=step_minutes)
        ).strftime("%H:%M")

        max_row = df_elekter_short.loc[df_elekter_short["price"].idxmax()]
        max_start = max_row["time_local"].strftime("%d.%m kell %H:%M")
        max_end = (
            max_row["time_local"] + timedelta(minutes=step_minutes)
        ).strftime("%H:%M")

        col_s1, col_s2, col_s3 = st.columns(3)
        col_s1.info(f"**Päeva keskmine hind:**\n\n### {avg_price:.2f} €/MWh")
        col_s2.success(
            f"**Madalaim ({min_start} - {min_end}):**\n\n### {min_row['price']:.2f} €/MWh ({(min_row['price']/10):.2f} s/kWh)"
        )
        col_s3.error(
            f"**Kõrgeim ({max_start} - {max_end}):**\n\n### {max_row['price']:.2f} €/MWh ({(max_row['price']/10):.2f} s/kWh)"
        )
    else:
        st.warning("Lühiajaliste elektrihindade laadimine ebaõnnestus.")

    st.markdown("---")
    st.markdown("#### 2. Viimase 5 aasta kuude keskmised hinnad")

    if not df_el_monthly.empty:
        fig_monthly = px.bar(
            df_el_monthly,
            x="year_month",
            y="mean",
            labels={"year_month": "Kuu", "mean": "Keskmine hind (€/MWh)"},
            title="Nord Pool Eesti hinnapiirkonna kalendrikuude keskmised hinnad (€/MWh)",
        )
        fig_monthly.update_traces(marker_color="#2b5c8f")
        fig_monthly.update_layout(xaxis_tickformat="%Y-%m")
        st.plotly_chart(fig_monthly, use_container_width=True)

        # Viimase 6 kuu tabelvaade
        with st.expander("Vaata viimaste kuude keskmiste tabelit"):
            df_table = df_el_monthly.tail(12).sort_values(
                "year_month", ascending=False
            )
            df_table_display = pd.DataFrame({
                "Kuu": df_table["year_month"].dt.strftime("%Y-%m"),
                "Keskmine hind (€/MWh)": df_table["mean"].round(2),
                "Keskmine hind (s/kWh)": df_table["s_kwh"].round(2),
                "Madalaim hind (€/MWh)": df_table["min"].round(2),
                "Kõrgeim hind (€/MWh)": df_table["max"].round(2),
            })
            st.dataframe(df_table_display, hide_index=True, use_container_width=True)
    else:
        st.info("Pikaajalise ajaloo laadimine...")

    st.caption(
        "📍 **Allikas:** Elering Live API / Nord Pool Day-Ahead EE hinnapiirkond."
    )

# TTF Gaas
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
        st.caption(
            "📍 **Allikas:** ICE Endex / Yahoo Finance (`TTF=F`) — Dutch TTF Natural Gas Futures."
        )
    else:
        st.warning("Gaasihindade laadimine ebaõnnestus.")

# Brent Nafta
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
        st.caption(
            "📍 **Allikas:** ICE Europe / Yahoo Finance (`BZ=F`) — Brent Crude Oil Futures."
        )
    else:
        st.warning("Naftahindade laadimine ebaõnnestus.")

# EU ETS EUA
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
        st.caption(
            "📍 **Allikas:** London Stock Exchange / ICE (`CO2.L` / SparkChange Physical Carbon EUA ETC) — tagatud 1:1 Euroopa Liidu heitmekvoodiga (EUA)."
        )
    else:
        st.warning("EU ETS kvoodi andmete laadimine ebaõnnestus.")
