from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import pandas as pd
import plotly.express as px
import requests
import streamlit as st

# Lehe seadistus
st.set_page_config(
    page_title="Regiooni energiaturu ülevaade",
    page_icon="⚡",
    layout="wide",
)


@st.cache_data(ttl=180)
def fetch_elering_short_term():
    """Pärib Eleringist täna ja homme kehtivad elektrihinnad."""
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
    """Abifunktsioon ühe kuupõhise vahemiku pärimiseks."""
    url = f"https://dashboard.elering.ee/api/nps/price?start={start_str}&end={end_str}"
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            return res.json().get("data", {}).get("ee", [])
    except Exception:
        pass
    return []


@st.cache_data(ttl=3600 * 12)  # Pikk ajalugu salvestub vahemällu 12 tunniks
def fetch_elering_long_history(years=5):
    """Pärib viimase 5 aasta andmed 1-kuuliste tükkidena paralleelselt."""
    now_utc = datetime.now(timezone.utc)
    chunks = []

    # Loome 30-päevased vahemikud alates tänasest 5 aastat tagasi
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
    # Paralleelpäringud kiireks laadimiseks
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

    # 1. Päevakeskmised (5 aastat)
    df["date"] = df["time_local"].dt.date
    df_daily = (
        df.groupby("date")["price"]
        .agg(mean="mean", min="min", max="max")
        .reset_index()
    )
    df_daily["date"] = pd.to_datetime(df_daily["date"])

    # 2. Kuukeskmised
    df["year"] = df["time_local"].dt.year
    df["month"] = df["time_local"].dt.month
    df["month_label"] = df["time_local"].dt.strftime("%Y-%m")

    df_monthly = (
        df.groupby(["year", "month", "month_label"])["price"]
        .agg(mean="mean", min="min", max="max", count="count")
        .reset_index()
    )

    return df, df_daily, df_monthly


# Päise riba
col_title, col_btn = st.columns([5, 1])
with col_title:
    st.title("Eesti elektrienergia turuhinnad (Nord Pool EE)")
with col_btn:
    if st.button("🔄 Värskenda"):
        st.cache_data.clear()
        st.rerun()

# Andmete laadimine
with st.spinner("Laadin elektrihinna andmeid Eleringist..."):
    df_short = fetch_elering_short_term()
    df_raw, df_daily, df_monthly = fetch_elering_long_history(years=5)

# Intervalli pikkus (15 min või 1h)
interval_seconds = 3600
if len(df_short) > 1:
    interval_seconds = int(
        df_short["timestamp"].iloc[1] - df_short["timestamp"].iloc[0]
    )
    if interval_seconds <= 0:
        interval_seconds = 900
step_label = "15 min" if interval_seconds == 900 else "tund"

# --- 1. MÕÕDIKUTE KAARDID (Hetkeseis + Jooksev kuu) ---
st.subheader("Hetketuru hinnatasemed")
kpi1, kpi2, kpi3 = st.columns(3)

# Hetkehind
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
            label=f"Jooksev hetkehind ({step_label})",
            value=f"{current_el_price:.2f} €/MWh",
            delta=f"{(current_el_price / 10):.2f} s/kWh",
            delta_color="off",
        )
    else:
        st.metric(label="Jooksev hind", value="Pole saadaval")

# Jooksva kuu keskmine
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
            label="Jooksva kuu keskmine (MTD)",
            value=f"{current_month_avg:.2f} €/MWh",
            delta=f"{(current_month_avg / 10):.2f} s/kWh",
            delta_color="off",
        )
    else:
        st.metric(label="Kuu keskmine", value="Pole saadaval")

# Tänase päeva keskmine
with kpi3:
    if not df_short.empty:
        today_mean = df_short["price"].mean()
        st.metric(
            label="Tänane keskmine hind",
            value=f"{today_mean:.2f} €/MWh",
            delta=f"{(today_mean / 10):.2f} s/kWh",
            delta_color="off",
        )
    else:
        st.metric(label="Tänane keskmine", value="Pole saadaval")

st.divider()

# --- 2. PÄEVA OLUKORD (TÄNA JA HOMME) ---
st.subheader("1. Päeva olukord (tänane ja homne)")
if not df_short.empty:
    fig_short = px.bar(
        df_short,
        x="time_local",
        y="price",
        color="price",
        color_continuous_scale="Turbo",
        labels={"time_local": "Kellaaeg", "price": "Hind (€/MWh)"},
        title=f"Nord Pool Eesti tunnihinnad ({step_label} intervalliga)",
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
    min_end = (min_row["time_local"] + timedelta(minutes=step_minutes)).strftime(
        "%H:%M"
    )

    max_row = df_short.loc[df_short["price"].idxmax()]
    max_start = max_row["time_local"].strftime("%d.%m kell %H:%M")
    max_end = (max_row["time_local"] + timedelta(minutes=step_minutes)).strftime(
        "%H:%M"
    )

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

st.divider()

# --- 3. VIIMASE 5 AASTA HINNAGRAAFIK PÄEVADE KAUPA ---
st.subheader("2. Viimase 5 aasta hinnad (päevade kaupa)")
if not df_daily.empty:
    fig_daily = px.line(
        df_daily,
        x="date",
        y="mean",
        labels={"date": "Kuupäev", "mean": "Päeva keskmine hind (€/MWh)"},
        title="Nord Pool Eesti päeva aritmeetilised keskmised hinnad (viimased 5 aastat)",
    )
    fig_daily.update_traces(line_color="#1f77b4", line_width=1.5)
    st.plotly_chart(fig_daily, use_container_width=True)
else:
    st.info("Päevaandmete ajalugu laaditakse Eleringist...")

st.divider()

# --- 4. JOOKSVA AASTA KUUDE ÜLEVAADE JA AASTA KESKMINE ---
current_year = datetime.now().year
st.subheader(
    f"3. Jooksva aasta ({current_year}) kuude ülevaade ja aritmeetiline keskmine"
)

if not df_raw.empty:
    df_curr_year = df_raw[df_raw["time_local"].dt.year == current_year]

    if not df_curr_year.empty:
        # Kuude keskmised
        df_year_monthly = (
            df_curr_year.groupby(
                df_curr_year["time_local"].dt.strftime("%Y-%m")
            )["price"]
            .agg(mean="mean", min="min", max="max")
            .reset_index()
        )
        df_year_monthly.columns = [
            "Periood",
            "Keskmine (€/MWh)",
            "Madalaim (€/MWh)",
            "Kõrgeim (€/MWh)",
        ]
        df_year_monthly["Keskmine (s/kWh)"] = (
            df_year_monthly["Keskmine (€/MWh)"] / 10
        )

        current_month_str = datetime.now().strftime("%Y-%m")
        df_year_monthly["Periood"] = df_year_monthly["Periood"].apply(
            lambda x: f"{x} (jooksev kuu)" if x == current_month_str else f"{x}"
        )

        # Jooksva aasta aritmeetiline keskmine kuvamise hetkel
        ytd_mean = df_curr_year["price"].mean()
        ytd_min = df_curr_year["price"].min()
        ytd_max = df_curr_year["price"].max()

        summary_row = pd.DataFrame([{
            "Periood": f"⭐ AASTA {current_year} KESKMINE (YTD)",
            "Keskmine (€/MWh)": ytd_mean,
            "Madalaim (€/MWh)": ytd_min,
            "Kõrgeim (€/MWh)": ytd_max,
            "Keskmine (s/kWh)": ytd_mean / 10,
        }])

        final_table = pd.concat([df_year_monthly, summary_row], ignore_index=True)

        final_table["Keskmine (€/MWh)"] = final_table["Keskmine (€/MWh)"].apply(
            lambda x: f"{x:.2f}"
        )
        final_table["Keskmine (s/kWh)"] = final_table["Keskmine (s/kWh)"].apply(
            lambda x: f"{x:.2f}"
        )
        final_table["Madalaim (€/MWh)"] = final_table["Madalaim (€/MWh)"].apply(
            lambda x: f"{x:.2f}"
        )
        final_table["Kõrgeim (€/MWh)"] = final_table["Kõrgeim (€/MWh)"].apply(
            lambda x: f"{x:.2f}"
        )

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
    "📍 **Allikas:** Elering Live API / Nord Pool Day-Ahead EE hinnapiirkond. Hinnad on käibemaksuta."
)
