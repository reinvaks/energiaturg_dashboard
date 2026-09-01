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


@st.cache_data(ttl=3600 * 6)  # Pikk ajalugu uueneb iga 6 tunni järel
def fetch_elering_long_history(years=5):
    """Pärib Eleringist viimase 5 aasta elektrihinnad ja töötleb päeva- ning kuukeskmised."""
    now_utc = datetime.now(timezone.utc)
    all_data = []

    # Pätime andmed aasta kaupa, et tagada kiire ja stabiilne vastus
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
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df = pd.DataFrame(all_data).drop_duplicates(subset=["timestamp"])
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["time_local"] = df["time"].dt.tz_convert("Europe/Tallinn")
    df = df.sort_values("time_local")

    # 1. Päevade keskmised (viimased 5 aastat)
    df["date"] = df["time_local"].dt.date
    df_daily = (
        df.groupby("date")["price"]
        .agg(mean="mean", min="min", max="max")
        .reset_index()
    )
    df_daily["date"] = pd.to_datetime(df_daily["date"])
    df_daily["s_kwh"] = df_daily["mean"] / 10

    # 2. Kuude keskmised
    df["year"] = df["time_local"].dt.year
    df["month"] = df["time_local"].dt.month
    df["month_label"] = df["time_local"].dt.strftime("%Y-%m")

    df_monthly = (
        df.groupby(["year", "month", "month_label"])["price"]
        .agg(mean="mean", min="min", max="max", count="count")
        .reset_index()
    )
    df_monthly["s_kwh"] = df_monthly["mean"] / 10

    return df, df_daily, df_monthly


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

# Perioodi valik teistele toorainetele
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
df_el_raw, df_el_daily, df_el_monthly = fetch_elering_long_history(years=5)
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

# 2. Jooksva kuu keskmine
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
            label="Elektri jooksva kuu keskmine",
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

with tab_el:
    # 1. Päevasisesed hinnad
    st.markdown("#### 1. Jooksva ja homse päeva tunnihinnad")
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

    # 2. Viimase 5 aasta elektrihinnad päevade kaupa
    st.markdown("#### 2. Eesti hinnapiirkonna viimase 5 aasta hinnad (päevade kaupa)")
    if not df_el_daily.empty:
        fig_daily = px.line(
            df_el_daily,
            x="date",
            y="mean",
            labels={"date": "Kuupäev", "mean": "Päeva keskmine hind (€/MWh)"},
            title="Nord Pool Eesti päeva aritmeetilised keskmised hinnad (viimased 5 aastat)",
        )
        fig_daily.update_traces(line_color="#1f77b4", line_width=1.5)
        st.plotly_chart(fig_daily, use_container_width=True)
    else:
        st.info("5 aasta päevaandmete laadimine...")

    st.markdown("---")

    # 3. Jooksva aasta kuude ülevaade ja aasta keskmine tabelis
    current_year = datetime.now().year
    st.markdown(f"#### 3. Jooksva aasta ({current_year}) kuude ülevaade ja keskmised")

    if not df_el_raw.empty:
        # Filtreerime välja ainult jooksva aasta andmed
        df_curr_year = df_el_raw[df_el_raw["time_local"].dt.year == current_year]

        if not df_curr_year.empty:
            # Kuude keskmised jooksval aastal
            df_curr_year_monthly = (
                df_curr_year.groupby(
                    df_curr_year["time_local"].dt.strftime("%Y-%m")
                )["price"]
                .agg(mean="mean", min="min", max="max")
                .reset_index()
            )
            df_curr_year_monthly.columns = [
                "Periood",
                "Keskmine (€/MWh)",
                "Madalaim (€/MWh)",
                "Kõrgeim (€/MWh)",
            ]
            df_curr_year_monthly["Keskmine (s/kWh)"] = (
                df_curr_year_monthly["Keskmine (€/MWh)"] / 10
            )

            # Märgime ära, milline on jooksev kuu
            current_month_str = datetime.now().strftime("%Y-%m")
            df_curr_year_monthly["Periood"] = df_curr_year_monthly[
                "Periood"
            ].apply(
                lambda x: f"{x} (jooksev kuu)"
                if x == current_month_str
                else f"{x}"
            )

            # Jooksva aasta aritmeetiline keskmine kuvamise hetkel
            ytd_mean = df_curr_year["price"].mean()
            ytd_min = df_curr_year["price"].min()
            ytd_max = df_curr_year["price"].max()

            # Koostame koondrea aasta keskmise kohta
            summary_row = pd.DataFrame([{
                "Periood": f"⭐ AASTA {current_year} KESKMINE (YTD)",
                "Keskmine (€/MWh)": ytd_mean,
                "Madalaim (€/MWh)": ytd_min,
                "Kõrgeim (€/MWh)": ytd_max,
                "Keskmine (s/kWh)": ytd_mean / 10,
            }])

            # Liidame tabeli ja koondrea
            final_table = pd.concat(
                [df_curr_year_monthly, summary_row], ignore_index=True
            )

            # Üm張りstame numbrid kuvamiseks
            final_table["Keskmine (€/MWh)"] = final_table[
                "Keskmine (€/MWh)"
            ].apply(lambda x: f"{x:.2f}")
            final_table["Keskmine (s/kWh)"] = final_table[
                "Keskmine (s/kWh)"
            ].apply(lambda x: f"{x:.2f}")
            final_table["Madalaim (€/MWh)"] = final_table[
                "Madalaim (€/MWh)"
            ].apply(lambda x: f"{x:.2f}")
            final_table["Kõrgeim (€/MWh)"] = final_table[
                "Kõrgeim (€/MWh)"
            ].apply(lambda x: f"{x:.2f}")

            # Kuvame tabeli
            st.dataframe(
                final_table[
                    [
                        "Periood",
                        "Keskmine (€/MWh)",
                        "Keskmine (s/kWh)",
                        "Madalaim (€/MWh)",
                        "Kõrgeim (€/MWh)",
                    ]
                ],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info(f"Aasta {current_year} andmed pole veel kättesaadavad.")

    st.caption(
        "📍 **Allikas:** Elering Live API / Nord Pool Day-Ahead EE hinnapiirkond. Hinnad on ilma käibemaksuta."
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
