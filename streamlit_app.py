from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from zoneinfo import ZoneInfo
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st
from umm_client import fetch_umm_messages, load_snapshot

# Lehe seadistus
st.set_page_config(
    page_title="Energiaturu ja reservide reaalaja armatuurlaud",
    page_icon="⚡",
    layout="wide",
)


TALLINN_TZ = ZoneInfo("Europe/Tallinn")

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "EnergiaturuArmatuurlaud/validated-1.0"})
HTTP.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
    ),
)

EEX_TTF_HISTORY_URL = "https://gasandregistry.eex.com/Gas/NGP/TTF_NGP_60_Days.csv"
EEX_TTF_CURRENT_URL = "https://gasandregistry.eex.com/Gas/NGP/TTF_NGP_15_Mins.csv"
EEX_EUA_AUCTION_URL = (
    "https://public.eex-group.com/eex/eua-auction-report/"
    "emission-spot-primary-market-auction-report-2026-data.xlsx"
)
EIA_BRENT_HTML_URL = "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=RBRTE&f=D"
AGSI_BASE = "https://agsi.gie.eu/api"
CONEXUS_STOCKS_URL = "https://www.conexus.lv/storage-stocks"
CONEXUS_CYCLE_URL = "https://www.conexus.lv/circle-data"
VOLTON_AFRR_CAPACITY = "https://public-data.volton.energy/v1/afrr-capacity-price/latest.json"
VOLTON_MFRR_CAPACITY = "https://public-data.volton.energy/v1/mfrr-capacity-price/latest.json"

ENTSOE_DOMAINS = {
    "EE": "10Y1001A1001A39I",
    "FI": "10YFI-1--------U",
    "LV": "10YLV-1001A00074",
    "LT": "10YLT-1001A0008Q",
}


def _empty_market_df():
    return pd.DataFrame(columns=["Date", "Close"])


def _safe_get(url, **kwargs):
    try:
        r = HTTP.get(url, timeout=(5, 25), **kwargs)
        r.raise_for_status()
        return r
    except requests.RequestException:
        return None


def _best_datetime_column(df):
    best_col, best_vals, best_n = None, None, 0
    for c in df.columns:
        vals = pd.to_datetime(df[c], errors="coerce")
        n = int(vals.notna().sum())
        if n > best_n:
            best_col, best_vals, best_n = c, vals, n
    return best_col, best_vals


def _best_numeric_column(df, hints=()):
    best_col, best_vals, best_score = None, None, -1
    for c in df.columns:
        vals = pd.to_numeric(
            df[c].astype(str)
            .str.replace("\xa0", " ", regex=False)
            .str.replace(",", ".", regex=False)
            .str.replace(r"[^0-9.\-]", "", regex=True),
            errors="coerce",
        )
        n = int(vals.notna().sum())
        score = n + 1000 * sum(h.lower() in str(c).lower() for h in hints)
        if n and score > best_score:
            best_col, best_vals, best_score = c, vals, score
    return best_col, best_vals


def _parse_eua_auction_workbook(raw):
    def norm(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        x = str(v).replace("\n", " ").replace("\r", " ").replace("₂", "2").replace("€", " EUR ")
        return re.sub(r"\s+", " ", x).strip().lower()

    def parse_dates(s):
        dt = pd.to_datetime(s, errors="coerce")
        numeric = pd.to_numeric(s, errors="coerce")
        mask = dt.isna() & numeric.between(30000, 70000)
        if mask.any():
            dt.loc[mask] = pd.to_datetime(numeric.loc[mask], unit="D", origin="1899-12-30", errors="coerce")
        return dt

    def parse_num(s):
        x = (
            s.astype(str)
            .str.replace("\xa0", " ", regex=False)
            .str.replace("EUR", "", regex=False, case=False)
            .str.replace("€", "", regex=False)
            .str.replace(r"[^0-9,.\-]", "", regex=True)
            .str.replace(",", ".", regex=False)
        )
        return pd.to_numeric(x, errors="coerce")

    book = pd.ExcelFile(BytesIO(raw))
    candidates = []
    for sheet in book.sheet_names:
        try:
            df = pd.read_excel(book, sheet_name=sheet, header=None, dtype=object)
        except Exception:
            continue
        if df.empty:
            continue

        anchor = None
        for r in range(min(100, len(df))):
            toks = [norm(v) for v in df.iloc[r].tolist()]
            has_date = any(t == "date" or "auction date" in t for t in toks)
            has_aux = any("auction name" in t for t in toks) or any(t == "time" for t in toks)
            if has_date and has_aux:
                anchor = r
                break
        if anchor is None:
            continue

        hdr = df.iloc[max(0, anchor - 8):min(len(df), anchor + 4)].copy().ffill(axis=1)
        paths = []
        for c in range(df.shape[1]):
            parts = []
            for rr in range(len(hdr)):
                t = norm(hdr.iloc[rr, c])
                if t and (not parts or t != parts[-1]):
                    parts.append(t)
            paths.append(" | ".join(parts))

        anchor_tokens = [norm(v) for v in df.iloc[anchor].tolist()]
        date_idxs = [i for i, t in enumerate(anchor_tokens) if t == "date" or "auction date" in t]
        if not date_idxs:
            date_idxs = [i for i, p in enumerate(paths) if p.endswith(" | date") or p == "date"]

        price_idxs = []
        for i, p in enumerate(paths):
            score = 0
            if "auction clearing price" in p:
                score += 100
            elif "clearing price" in p:
                score += 80
            elif "price" in p:
                score += 15
            if "eur" in p or "tco2" in p or "co2" in p:
                score += 15
            if any(bad in p for bad in ("volume", "revenue", "participant", "bid", "time", "auction name")):
                score -= 40
            if score > 0:
                price_idxs.append((score, i))
        price_idxs.sort(reverse=True)

        for start_row in range(anchor + 1, min(anchor + 7, len(df))):
            body = df.iloc[start_row:]
            for di in date_idxs[:4]:
                dates = parse_dates(body.iloc[:, di])
                for score, pi in price_idxs[:8]:
                    prices = parse_num(body.iloc[:, pi])
                    out = pd.DataFrame({"Date": dates, "Close": prices}).dropna()
                    out = out[(out["Close"] > 1) & (out["Close"] < 500)]
                    out = out.drop_duplicates("Date", keep="last").sort_values("Date")
                    if not out.empty:
                        candidates.append((score, len(out), out))

    if not candidates:
        return _empty_market_df()
    return max(candidates, key=lambda x: (x[0], x[1]))[2].reset_index(drop=True)


def _entsoe_xml(token, params):
    if not token:
        return None
    q = dict(params)
    q["securityToken"] = token
    r = _safe_get(
        "https://web-api.tp.entsoe.eu/api",
        params=q,
        headers={"Accept": "application/xml,text/xml,*/*"},
    )
    if r is None:
        return None
    try:
        root = ET.fromstring(r.text)
        if "acknowledgement" in root.tag.lower():
            return None
    except ET.ParseError:
        return None
    return r.text


def _lname(tag):
    return tag.split("}")[-1]


def _child_text(node, name):
    for el in node.iter():
        if _lname(el.tag) == name and el.text:
            return el.text.strip()
    return None


def _resolution_delta(value):
    return {
        "PT15M": pd.Timedelta(minutes=15),
        "PT30M": pd.Timedelta(minutes=30),
        "PT60M": pd.Timedelta(hours=1),
        "PT1H": pd.Timedelta(hours=1),
    }.get(value or "", pd.Timedelta(hours=1))


def _parse_entsoe_series(xml_text, value_name):
    if not xml_text:
        return pd.DataFrame()
    root = ET.fromstring(xml_text)
    rows = []
    for ts in [x for x in root.iter() if _lname(x.tag) == "TimeSeries"]:
        psr = next((el.text.strip() for el in ts.iter() if _lname(el.tag) == "psrType" and el.text), None)
        for period in [x for x in ts.iter() if _lname(x.tag) == "Period"]:
            start = pd.to_datetime(_child_text(period, "start"), utc=True, errors="coerce")
            if pd.isna(start):
                continue
            step = _resolution_delta(_child_text(period, "resolution"))
            for point in [x for x in period if _lname(x.tag) == "Point"]:
                try:
                    pos = int(_child_text(point, "position"))
                    val = float(_child_text(point, "quantity") or _child_text(point, "price.amount"))
                except Exception:
                    continue
                rows.append({
                    "time_utc": start + (pos - 1) * step,
                    value_name: val,
                    "psr_type": psr,
                })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).sort_values("time_utc")
    out["time_local"] = pd.to_datetime(out["time_utc"], utc=True).dt.tz_convert(TALLINN_TZ)
    return out


def _fetch_entsoe_actual_load(start, end):
    token = st.secrets.get("ENTSOE_API_KEY", "")
    params = {
        "documentType": "A65",
        "processType": "A16",
        "outBiddingZone_Domain": ENTSOE_DOMAINS["EE"],
        "periodStart": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
    }
    return _parse_entsoe_series(_entsoe_xml(token, params), "load_mw")


def _fetch_entsoe_flow(start, end, from_region, to_region):
    token = st.secrets.get("ENTSOE_API_KEY", "")
    params = {
        "documentType": "A11",
        "out_Domain": ENTSOE_DOMAINS[from_region],
        "in_Domain": ENTSOE_DOMAINS[to_region],
        "periodStart": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
    }
    return _parse_entsoe_series(_entsoe_xml(token, params), "flow_mw")


def _latest_directional_net(out_df, in_df):
    if out_df.empty and in_df.empty:
        return pd.DataFrame(columns=["time_local", "net_mw"])

    def prep(df, name):
        if df.empty:
            return pd.DataFrame(columns=["time_local", name])
        return df[["time_local", "flow_mw"]].rename(columns={"flow_mw": name}).dropna().sort_values("time_local")

    a = prep(out_df, "out_mw")
    b = prep(in_df, "in_mw")
    if a.empty:
        b["net_mw"] = -b["in_mw"]
        return b[["time_local", "net_mw"]]
    if b.empty:
        a["net_mw"] = a["out_mw"]
        return a[["time_local", "net_mw"]]
    m = pd.merge_asof(
        a, b, on="time_local", direction="nearest", tolerance=pd.Timedelta("30min")
    )
    m["net_mw"] = m["out_mw"].fillna(0) - m["in_mw"].fillna(0)
    return m[["time_local", "net_mw"]]


# --- 1. AMETLIKE ALLIKATE JA REAALAJA API PÄRIMISE FUNKTSIOONID ---


@st.cache_data(ttl=60)
def fetch_elering_regional_short_term():
    """Pärib Eleringist otse reaalajas lühiajalised hinnad (EE, LV, LT, FI)."""
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
    """Pärib Eleringist ametliku ajaloolise hinnainfo (EE, LV, LT, FI)."""
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
def fetch_realtime_commodity_data(function_name, symbol_or_interval):
    """Validated sources only. No synthetic fallback values."""
    if function_name == "NATURAL_GAS" and symbol_or_interval == "TTF":
        r = _safe_get(EEX_TTF_HISTORY_URL)
        if r is None:
            return _empty_market_df()
        try:
            text = r.content.decode("utf-8-sig", errors="replace")
            try:
                df = pd.read_csv(StringIO(text), sep=None, engine="python")
            except Exception:
                df = pd.read_csv(StringIO(text), sep=";", engine="python")
            df.columns = [str(c).strip() for c in df.columns]
            _, dates = _best_datetime_column(df)
            _, prices = _best_numeric_column(df, ("ttf", "ngp", "price", "eur", "value"))
            if dates is None or prices is None:
                return _empty_market_df()
            out = pd.DataFrame({"Date": dates, "Close": prices}).dropna()
            out = out.drop_duplicates("Date", keep="last").sort_values("Date")

            # Add the current official NGP TTF D/D+1/D+2 value, refreshed by EEX every 15 minutes.
            rc = _safe_get(EEX_TTF_CURRENT_URL)
            if rc is not None:
                try:
                    current_text = rc.content.decode("utf-8-sig", errors="replace")
                    try:
                        cur = pd.read_csv(StringIO(current_text), sep=None, engine="python")
                    except Exception:
                        cur = pd.read_csv(StringIO(current_text), sep=";", engine="python")
                    cur.columns = [str(c).strip() for c in cur.columns]
                    _, cur_dates = _best_datetime_column(cur)
                    _, cur_prices = _best_numeric_column(cur, ("ttf", "ngp", "price", "eur", "value"))
                    if cur_dates is not None and cur_prices is not None:
                        cdf = pd.DataFrame({"Date": cur_dates, "Close": cur_prices}).dropna()
                        cdf = cdf[(cdf["Close"] > -500) & (cdf["Close"] < 1000)]
                        if not cdf.empty:
                            # Current file may contain D/D+1/D+2; keep all valid delivery dates.
                            out = pd.concat([out, cdf], ignore_index=True)
                            out = out.drop_duplicates("Date", keep="last").sort_values("Date")
                except Exception:
                    pass
            return out.reset_index(drop=True)
        except Exception:
            return _empty_market_df()

    if function_name == "BRENT":
        r = _safe_get(EIA_BRENT_HTML_URL, headers={"Accept": "text/html,*/*"})
        if r is None:
            return _empty_market_df()
        try:
            tables = pd.read_html(StringIO(r.text))
            rows = []
            weekday_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4}
            for tab in tables:
                week_col = next((c for c in tab.columns if "Week Of" in str(c)), None)
                if week_col is None:
                    continue
                for _, rr in tab.iterrows():
                    label = str(rr.get(week_col, "")).strip()
                    m = re.search(r"(\d{4})\s+([A-Za-z]{3})-\s*(\d{1,2})", label)
                    if not m:
                        continue
                    try:
                        base = pd.Timestamp(datetime.strptime(
                            f"{m.group(1)} {m.group(2)} {m.group(3)}", "%Y %b %d"
                        ))
                    except Exception:
                        continue
                    for c in tab.columns:
                        day = next((k for k in weekday_map if str(c).strip().startswith(k)), None)
                        if day is None:
                            continue
                        val = pd.to_numeric(pd.Series([rr.get(c)]), errors="coerce").iloc[0]
                        if pd.notna(val):
                            rows.append({"Date": base + pd.Timedelta(days=weekday_map[day]), "Close": float(val)})
            out = pd.DataFrame(rows)
            if out.empty:
                return _empty_market_df()
            return out.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
        except Exception:
            return _empty_market_df()

    if function_name == "CARBON":
        r = _safe_get(EEX_EUA_AUCTION_URL)
        if r is None:
            return _empty_market_df()
        return _parse_eua_auction_workbook(r.content)

    return _empty_market_df()


@st.cache_data(ttl=900)
def fetch_getbaltic_history(df_ttf_full):
    """No validated public automatic BGSI feed is used here.
    Returning empty is preferable to fabricating TTF + spread.
    """
    return pd.DataFrame(columns=["Date", "Close"])


@st.cache_data(ttl=600)
def fetch_gas_storage_data():
    """EU27 from GIE AGSI+; Inčukalns primary from Conexus Storage Stocks.

    No synthetic or hard-coded fallback values are used for stock level.
    GIE Latvia is used only if the Conexus page is temporarily unavailable.
    """
    key = st.secrets.get("GIE_API_KEY", "") or st.secrets.get("GIE_AGSI_API_KEY", "")

    result = {
        "eu_fill_pct": float("nan"),
        "eu_stored_twh": float("nan"),
        "eu_capacity_twh": float("nan"),
        "latvia_fill_pct": float("nan"),
        "latvia_stored_twh": float("nan"),
        "latvia_capacity_twh": float("nan"),
        "latvia_injection_rate_gwh_day": float("nan"),
        "latvia_gas_day": None,
        "latvia_source": None,
    }

    # --- EU27: official GIE AGSI+ ---
    if key:
        r = _safe_get(
            AGSI_BASE,
            params={"type": "eu", "size": 10, "reverse": "true"},
            headers={"x-key": key},
        )
        if r is not None:
            try:
                rows = r.json().get("data", [])
                if rows:
                    df = pd.DataFrame(rows)
                    if "gasDayStart" in df.columns:
                        df["gasDayStart"] = pd.to_datetime(df["gasDayStart"], errors="coerce")
                        df = df.sort_values("gasDayStart", ascending=False)
                    row = df.iloc[0]
                    for src, dst in [
                        ("full", "eu_fill_pct"),
                        ("gasInStorage", "eu_stored_twh"),
                        ("workingGasVolume", "eu_capacity_twh"),
                    ]:
                        try:
                            result[dst] = float(row[src])
                        except Exception:
                            pass
            except Exception:
                pass

    # --- Inčukalns: primary source = operator Conexus ---
    r = _safe_get(CONEXUS_STOCKS_URL, headers={"Accept": "text/html,*/*"})
    if r is not None:
        try:
            tables = pd.read_html(StringIO(r.text))
            stock_df = None
            for t in tables:
                cols = [str(c).strip().lower() for c in t.columns]
                joined = " | ".join(cols)
                if "gas day" in joined and "total" in joined and "user stocks" in joined:
                    stock_df = t.copy()
                    break

            if stock_df is not None and not stock_df.empty:
                stock_df.columns = [str(c).strip() for c in stock_df.columns]
                gas_col = next(c for c in stock_df.columns if "gas day" in c.lower())
                total_col = next(c for c in stock_df.columns if c.strip().lower() == "total")

                stock_df["_gas_day"] = pd.to_datetime(stock_df[gas_col], errors="coerce")
                stock_df["_total_kwh"] = pd.to_numeric(
                    stock_df[total_col].astype(str)
                    .str.replace("\xa0", "", regex=False)
                    .str.replace(" ", "", regex=False)
                    .str.replace(r"[^0-9.\-]", "", regex=True),
                    errors="coerce",
                )
                stock_df = stock_df.dropna(subset=["_gas_day", "_total_kwh"]).sort_values("_gas_day")
                if not stock_df.empty:
                    latest = stock_df.iloc[-1]
                    result["latvia_stored_twh"] = float(latest["_total_kwh"]) / 1_000_000_000.0
                    result["latvia_gas_day"] = latest["_gas_day"].date()
                    result["latvia_source"] = "Conexus Baltic Grid"

                    # Read current technical capacity from Conexus storage-cycle page.
                    cap_r = _safe_get(CONEXUS_CYCLE_URL, headers={"Accept": "text/html,*/*"})
                    if cap_r is not None:
                        try:
                            cap_tables = pd.read_html(StringIO(cap_r.text), header=None)
                            cap_kwh = None
                            for ct in cap_tables:
                                for _, rr in ct.iterrows():
                                    vals = [str(v).strip() for v in rr.tolist()]
                                    if vals and "technical capacity" in vals[0].lower():
                                        for v in vals[1:]:
                                            n = pd.to_numeric(
                                                pd.Series([v]).astype(str)
                                                .str.replace("\xa0", "", regex=False)
                                                .str.replace(" ", "", regex=False)
                                                .str.replace(r"[^0-9.\-]", "", regex=True),
                                                errors="coerce",
                                            ).iloc[0]
                                            if pd.notna(n) and float(n) > 1_000_000_000:
                                                cap_kwh = float(n)
                                                break
                                    if cap_kwh:
                                        break
                                if cap_kwh:
                                    break
                            if cap_kwh:
                                result["latvia_capacity_twh"] = cap_kwh / 1_000_000_000.0
                                result["latvia_fill_pct"] = (
                                    result["latvia_stored_twh"] / result["latvia_capacity_twh"] * 100.0
                                )
                        except Exception:
                            pass
        except Exception:
            pass

    # --- Latvia fallback / auxiliary fields from GIE only if needed ---
    if key and (
        pd.isna(result["latvia_stored_twh"])
        or pd.isna(result["latvia_capacity_twh"])
        or pd.isna(result["latvia_injection_rate_gwh_day"])
    ):
        r = _safe_get(
            AGSI_BASE,
            params={"country": "LV", "size": 10, "reverse": "true"},
            headers={"x-key": key},
        )
        if r is not None:
            try:
                rows = r.json().get("data", [])
                if rows:
                    df = pd.DataFrame(rows)
                    if "gasDayStart" in df.columns:
                        df["gasDayStart"] = pd.to_datetime(df["gasDayStart"], errors="coerce")
                        df = df.sort_values("gasDayStart", ascending=False)
                    row = df.iloc[0]

                    def n(field):
                        try:
                            return float(row[field])
                        except Exception:
                            return float("nan")

                    if pd.isna(result["latvia_stored_twh"]):
                        result["latvia_stored_twh"] = n("gasInStorage")
                        result["latvia_gas_day"] = (
                            row["gasDayStart"].date()
                            if "gasDayStart" in row and pd.notna(row["gasDayStart"])
                            else None
                        )
                        result["latvia_source"] = "GIE AGSI+ fallback"
                    if pd.isna(result["latvia_capacity_twh"]):
                        result["latvia_capacity_twh"] = n("workingGasVolume")
                    if pd.isna(result["latvia_fill_pct"]):
                        result["latvia_fill_pct"] = n("full")
                    result["latvia_injection_rate_gwh_day"] = n("injection")
            except Exception:
                pass

    return result


@st.cache_data(ttl=3600)
def fetch_frequency_reserves_full():
    """Actual aFRR/mFRR capacity prices from BTD via Volton public mirror.
    FCR is intentionally unavailable because no validated feed is wired here.
    """
    def get_rows(url, prefix):
        r = _safe_get(url)
        if r is None:
            return pd.DataFrame()
        try:
            df = pd.DataFrame(r.json().get("rows", []))
            if df.empty:
                return df
            df["time_local"] = pd.to_datetime(df["mtu_start"], utc=True, errors="coerce").dt.tz_convert(TALLINN_TZ)
            df["price_eur_mw_h"] = pd.to_numeric(df["price_eur_mw_h"], errors="coerce")
            piv = df.pivot_table(
                index="time_local", columns="direction",
                values="price_eur_mw_h", aggfunc="last"
            ).reset_index()
            return piv.rename(columns={
                "up": f"{prefix}_up_capacity",
                "down": f"{prefix}_down_capacity",
            })
        except Exception:
            return pd.DataFrame()

    afrr = get_rows(VOLTON_AFRR_CAPACITY, "aFRR")
    mfrr = get_rows(VOLTON_MFRR_CAPACITY, "mFRR")
    if afrr.empty and mfrr.empty:
        cols = [
            "time_local", "FCR_capacity", "aFRR_up_capacity", "aFRR_down_capacity",
            "mFRR_up_capacity", "mFRR_down_capacity",
        ]
        return pd.DataFrame(columns=cols), pd.DataFrame(), pd.DataFrame()

    if afrr.empty:
        df = mfrr.copy()
    elif mfrr.empty:
        df = afrr.copy()
    else:
        df = pd.merge(afrr, mfrr, on="time_local", how="outer")

    df["FCR_capacity"] = float("nan")
    for c in ["aFRR_up_capacity", "aFRR_down_capacity", "mFRR_up_capacity", "mFRR_down_capacity"]:
        if c not in df.columns:
            df[c] = float("nan")
    df = df.sort_values("time_local")

    hist = df.copy()
    hist["date"] = pd.to_datetime(hist["time_local"].dt.date)
    hist = hist.rename(columns={
        "FCR_capacity": "FCR",
        "aFRR_up_capacity": "aFRR_Up",
        "aFRR_down_capacity": "aFRR_Down",
        "mFRR_up_capacity": "mFRR_Up",
        "mFRR_down_capacity": "mFRR_Down",
    })
    daily = hist.groupby("date")[["FCR", "aFRR_Up", "aFRR_Down", "mFRR_Up", "mFRR_Down"]].mean().reset_index()
    if daily.empty:
        return df, daily, pd.DataFrame()
    daily["month"] = daily["date"].dt.strftime("%Y-%m")
    monthly = daily.groupby("month")[["FCR", "aFRR_Up", "aFRR_Down", "mFRR_Up", "mFRR_Down"]].mean().reset_index()
    return df, daily, monthly


@st.cache_data(ttl=300)
def fetch_entsoe_generation_data():
    """ENTSO-E actual generation only; no mock fallback."""
    api_key = st.secrets.get("ENTSOE_API_KEY", "")
    if not api_key:
        return pd.DataFrame(), False
    try:
        from entsoe import EntsoePandasClient
        client = EntsoePandasClient(api_key=api_key)
        now = pd.Timestamp.now(tz="UTC")
        start = now - pd.Timedelta(days=2)
        end = now + pd.Timedelta(hours=1)
        df_gen = client.query_generation("EE", start=start, end=end)
        if not isinstance(df_gen, pd.DataFrame) or df_gen.empty:
            return pd.DataFrame(), False
        df_gen = df_gen.tz_convert(TALLINN_TZ)
        if isinstance(df_gen.columns, pd.MultiIndex):
            if "Actual Aggregated" in df_gen.columns.get_level_values(-1):
                df_gen = df_gen.xs("Actual Aggregated", level=-1, axis=1, drop_level=True)
            else:
                df_gen = df_gen.T.groupby(level=0).sum(min_count=1).T
        df_gen = df_gen.apply(pd.to_numeric, errors="coerce")
        df_gen = df_gen.reset_index()
        df_gen = df_gen.rename(columns={df_gen.columns[0]: "time_local"})
        return df_gen, True
    except Exception:
        return pd.DataFrame(), False


@st.cache_data(ttl=300)
def get_european_day_ahead_map_data(target_date, df_short_all):
    """Only validated Elering prices are shown; unsupported countries remain empty."""
    known = {}
    if not df_short_all.empty:
        day = df_short_all[df_short_all["time_local"].dt.date == target_date]
        for reg in ["EE", "LV", "LT", "FI"]:
            sub = day[day["region"] == reg]
            if not sub.empty:
                known[reg] = float(sub["price"].mean())

    rows = [
        ("EST", "EE", "Eesti", 58.6, 25.5),
        ("FIN", "FI", "Soome", 63.0, 26.5),
        ("LVA", "LV", "Läti", 56.9, 24.8),
        ("LTU", "LT", "Leedu", 55.2, 23.9),
        ("SWE", "SE", "Rootsi", 60.5, 15.0),
        ("NOR", "NO", "Norra", 61.5, 8.5),
        ("DNK", "DK", "Taani", 56.0, 9.5),
        ("DEU", "DE", "Saksamaa", 51.2, 10.4),
        ("POL", "PL", "Poola", 52.1, 19.4),
        ("FRA", "FR", "Prantsusmaa", 46.6, 2.2),
        ("NLD", "NL", "Holland", 52.8, 5.3),
        ("BEL", "BE", "Belgia", 50.3, 4.5),
        ("GBR", "UK", "Ühendkuningriik", 54.5, -2.5),
        ("ESP", "ES", "Hispaania", 40.2, -3.7),
        ("PRT", "PT", "Portugal", 39.5, -8.2),
        ("ITA", "IT", "Itaalia", 42.5, 12.5),
        ("AUT", "AT", "Austria", 47.5, 14.5),
        ("CHE", "CH", "Šveits", 46.8, 8.2),
        ("CZE", "CZ", "Tšehhi", 49.8, 15.5),
        ("SVK", "SK", "Slovakkia", 48.7, 19.7),
        ("HUN", "HU", "Ungari", 47.1, 19.5),
        ("ROU", "RO", "Rumeenia", 45.9, 24.9),
        ("BGR", "BG", "Bulgaaria", 42.7, 25.5),
        ("GRC", "GR", "Kreeka", 39.0, 22.0),
        ("SVN", "SI", "Sloveenia", 46.1, 15.0),
        ("HRV", "HR", "Horvaatia", 45.1, 15.5),
        ("IRL", "IE", "Iirimaa", 53.4, -8.0),
    ]
    out = pd.DataFrame([
        {
            "iso_a3": iso, "code": cc, "country": country,
            "price": known.get(cc, float("nan")), "lat": lat, "lon": lon,
        }
        for iso, cc, country, lat, lon in rows
    ])
    out["s_kwh"] = out["price"] / 10
    out["label"] = out.apply(
        lambda r: f"{r['code']} {r['price']:.1f}" if pd.notna(r["price"]) else r["code"],
        axis=1,
    )
    return out



EUROSTAT_API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

COUNTRY_LABELS = {
    "EE": "Eesti",
    "FI": "Soome",
    "LV": "Läti",
    "LT": "Leedu",
    "SE": "Rootsi",
    "PL": "Poola",
    "DK": "Taani",
}


def _jsonstat_rows(payload):
    """Convert a Eurostat JSON-stat 2 response to row dictionaries."""
    dim_ids = payload.get("id", [])
    sizes = payload.get("size", [])
    dims = payload.get("dimension", {})
    values = payload.get("value", {})
    if not dim_ids or not sizes or not dims:
        return []

    position_to_code = {}
    for dim in dim_ids:
        idx = dims.get(dim, {}).get("category", {}).get("index", {})
        if isinstance(idx, dict):
            position_to_code[dim] = {int(pos): code for code, pos in idx.items()}
        elif isinstance(idx, list):
            position_to_code[dim] = {i: code for i, code in enumerate(idx)}
        else:
            position_to_code[dim] = {}

    total = 1
    for s in sizes:
        total *= int(s)

    def unravel(flat):
        coords = [0] * len(sizes)
        rem = flat
        for i in range(len(sizes) - 1, -1, -1):
            size = int(sizes[i])
            coords[i] = rem % size
            rem //= size
        return coords

    rows = []
    if isinstance(values, list):
        iterator = enumerate(values)
    else:
        iterator = ((int(k), v) for k, v in values.items())

    for flat, value in iterator:
        if value is None:
            continue
        coords = unravel(int(flat))
        row = {"value": value}
        for dim, pos in zip(dim_ids, coords):
            row[dim] = position_to_code.get(dim, {}).get(pos, str(pos))
        rows.append(row)
    return rows


def _eurostat_fetch(dataset, params):
    r = _safe_get(f"{EUROSTAT_API}/{dataset}", params={"lang": "EN", **params})
    if r is None:
        return pd.DataFrame()
    try:
        rows = _jsonstat_rows(r.json())
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=6 * 3600)
def fetch_eurostat_electricity_prices():
    """Latest official Eurostat electricity final prices, all taxes included.

    Household: band DC (2,500–4,999 kWh/year).
    Non-household: band IC (500–1,999 MWh/year).
    """
    geos = ["EE", "FI", "LV", "LT", "SE", "PL", "DK"]
    common = {
        "currency": "EUR",
        "unit": "KWH",
        "tax": "I_TAX",
        "lastTimePeriod": 1,
    }
    # Repeated query parameters are represented as lists by requests.
    household = _eurostat_fetch(
        "nrg_pc_204",
        {**common, "nrg_cons": "KWH2500-4999", "geo": geos},
    )
    business = _eurostat_fetch(
        "nrg_pc_205",
        {**common, "nrg_cons": "MWH500-1999", "geo": geos},
    )

    rows = []
    for label, df in [("Kodutarbijad", household), ("Äritarbijad", business)]:
        if df.empty:
            continue
        for geo in geos:
            sub = df[df.get("geo", pd.Series(dtype=str)) == geo]
            if sub.empty:
                continue
            val = pd.to_numeric(sub["value"], errors="coerce").dropna()
            if val.empty:
                continue
            rows.append({
                "Riik": COUNTRY_LABELS[geo],
                "Hind (€/kWh)": float(val.iloc[-1]),
                "Tarbijagrupp": label,
                "Periood": str(sub["time"].iloc[-1]) if "time" in sub.columns else "",
            })
    return pd.DataFrame(rows)


def _integrate_mw_series(series):
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return 0.0
    if not isinstance(s.index, pd.DatetimeIndex):
        return 0.0
    s = s.sort_index()
    diffs = s.index.to_series().diff().dropna().dt.total_seconds() / 3600.0
    step = float(diffs.median()) if not diffs.empty else 0.25
    if not (0 < step <= 2):
        step = 0.25
    return float(s.sum() * step)


@st.cache_data(ttl=6 * 3600)
def fetch_entsoe_ytd_energy():
    """Official ENTSO-E actual load and generation for Estonia, integrated to YTD TWh."""
    api_key = st.secrets.get("ENTSOE_API_KEY", "")
    if not api_key:
        return {}
    try:
        from entsoe import EntsoePandasClient
        client = EntsoePandasClient(api_key=api_key)
        now = pd.Timestamp.now(tz="UTC")
        start = pd.Timestamp(year=now.year, month=1, day=1, tz="UTC")

        load = client.query_load("EE", start=start, end=now)
        if isinstance(load, pd.DataFrame):
            # query_load may return one numerical column.
            load_series = load.select_dtypes(include="number").iloc[:, 0] if not load.empty else pd.Series(dtype=float)
        else:
            load_series = load

        gen = client.query_generation("EE", start=start, end=now)
        if not isinstance(gen, pd.DataFrame) or gen.empty:
            return {}

        if isinstance(gen.columns, pd.MultiIndex):
            second = gen.columns.get_level_values(-1)
            if "Actual Aggregated" in second:
                gen_actual = gen.xs("Actual Aggregated", level=-1, axis=1, drop_level=True)
            else:
                gen_actual = gen.T.groupby(level=0).sum(min_count=1).T
        else:
            gen_actual = gen.copy()

        gen_actual = gen_actual.apply(pd.to_numeric, errors="coerce")
        total_mw = gen_actual.sum(axis=1, min_count=1)

        renewable_keywords = (
            "Biomass",
            "Geothermal",
            "Hydro",
            "Marine",
            "Solar",
            "Wind",
            "Other renewable",
        )
        renewable_cols = [
            c for c in gen_actual.columns
            if any(k.lower() in str(c).lower() for k in renewable_keywords)
            and "pumped" not in str(c).lower()
        ]
        renewable_mw = (
            gen_actual[renewable_cols].sum(axis=1, min_count=1)
            if renewable_cols else pd.Series(index=gen_actual.index, dtype=float)
        )

        consumption_twh = _integrate_mw_series(load_series) / 1_000_000.0
        production_twh = _integrate_mw_series(total_mw) / 1_000_000.0
        renewable_twh = _integrate_mw_series(renewable_mw) / 1_000_000.0
        nonrenewable_twh = max(0.0, production_twh - renewable_twh)

        return {
            "year": int(now.year),
            "consumption_twh": consumption_twh,
            "production_twh": production_twh,
            "renewable_twh": renewable_twh,
            "nonrenewable_twh": nonrenewable_twh,
        }
    except Exception:
        return {}


@st.cache_data(ttl=12 * 3600)
def fetch_entsoe_installed_wind_solar(years):
    """Official ENTSO-E A68/A33 installed capacity. Only unambiguous wind/solar columns are used."""
    api_key = st.secrets.get("ENTSOE_API_KEY", "")
    if not api_key:
        return pd.DataFrame()
    try:
        from entsoe import EntsoePandasClient
        client = EntsoePandasClient(api_key=api_key)
        rows = []
        for year in years:
            start = pd.Timestamp(year=int(year), month=1, day=1, tz="UTC")
            end = pd.Timestamp(year=int(year) + 1, month=1, day=1, tz="UTC")
            try:
                s = client.query_installed_generation_capacity("EE", start=start, end=end)
            except Exception:
                s = pd.Series(dtype=float)

            if isinstance(s, pd.DataFrame):
                if s.empty:
                    vals = {}
                else:
                    vals = pd.to_numeric(s.iloc[0], errors="coerce").dropna().to_dict()
            elif isinstance(s, pd.Series):
                vals = pd.to_numeric(s, errors="coerce").dropna().to_dict()
            else:
                vals = {}

            wind = sum(float(v) for k, v in vals.items() if "wind" in str(k).lower())
            solar = sum(float(v) for k, v in vals.items() if "solar" in str(k).lower())
            rows.append({
                "Aasta": str(year),
                "Tuuleenergia (MW)": wind if wind > 0 else float("nan"),
                "Päikeseenergia (MW)": solar if solar > 0 else float("nan"),
                "Põlevkivi ja muud (MW)": float("nan"),
                "Maagaas / Koostootmine (MW)": float("nan"),
            })
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=12 * 3600)
def fetch_eurostat_gas_ytd():
    """Estonia monthly calculated inland natural-gas consumption from Eurostat nrg_cb_gasm."""
    now = datetime.now(TALLINN_TZ)
    params = {
        "geo": "EE",
        "siec": "G3000",
        "nrg_bal": "IC_CAL_MG",
        "unit": "TJ_GCV",
        "sinceTimePeriod": f"{now.year - 1}-01",
    }
    df = _eurostat_fetch("nrg_cb_gasm", params)
    if df.empty or "time" not in df.columns:
        return {}

    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["month"] = pd.to_datetime(df["time"].astype(str), format="%Y-%m", errors="coerce")
    df = df.dropna(subset=["value", "month"]).sort_values("month")
    if df.empty:
        return {}

    current = df[df["month"].dt.year == now.year].copy()
    previous = df[df["month"].dt.year == now.year - 1].copy()
    if current.empty:
        return {}

    latest_month = int(current["month"].dt.month.max())
    current = current[current["month"].dt.month <= latest_month]
    previous = previous[previous["month"].dt.month <= latest_month]

    # 1 TWh = 3,600 TJ.
    current_twh = float(current["value"].sum() / 3600.0)
    previous_twh = float(previous["value"].sum() / 3600.0) if not previous.empty else float("nan")
    change_pct = (
        (current_twh / previous_twh - 1) * 100
        if previous_twh and pd.notna(previous_twh) and previous_twh != 0 else float("nan")
    )
    return {
        "year": now.year,
        "previous_year": now.year - 1,
        "latest_month": latest_month,
        "current_twh": current_twh,
        "previous_twh": previous_twh,
        "change_pct": change_pct,
    }



def build_commodity_monthly_table(df_comm, unit_str):
    """Koostab ametlikele turuandmetele kuude kokkuvõttetabeli ohutult."""
    if df_comm.empty or "Date" not in df_comm.columns or "Close" not in df_comm.columns:
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



@st.cache_data(ttl=120)
def fetch_nordpool_umm():
    """Nord Pool UMM REST API primary; last GitHub snapshot as fallback."""
    rows, meta = fetch_umm_messages(limit=500, max_pages=4, retries=3)
    if rows and not meta.error:
        return rows, {
            "source": meta.source,
            "fetched_at": meta.fetched_at,
            "status_code": meta.status_code,
            "error": None,
            "fallback": False,
        }

    snapshot = Path("data/umm.json")
    if snapshot.exists():
        try:
            snap_rows, snap_meta = load_snapshot(snapshot)
            if snap_rows:
                return snap_rows, {
                    "source": "GitHub snapshot",
                    "fetched_at": snap_meta.get("fetched_at"),
                    "status_code": snap_meta.get("status_code"),
                    "error": meta.error,
                    "fallback": True,
                }
        except Exception as exc:
            fallback_error = f"{type(exc).__name__}: {exc}"
        else:
            fallback_error = "Snapshot was empty"
    else:
        fallback_error = "Snapshot file data/umm.json not found"

    return [], {
        "source": meta.source,
        "fetched_at": meta.fetched_at,
        "status_code": meta.status_code,
        "error": meta.error or fallback_error,
        "fallback": False,
    }


def normalize_umm_dataframe(rows):
    """Latest revision per message ID and active-event subset."""
    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    df = pd.DataFrame(rows)
    for c in ["publication_time", "event_start", "event_end"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")

    # Latest publication/revision per message ID. Keep rows without an ID as-is.
    if "message_id" in df.columns:
        ids = df["message_id"].fillna("").astype(str)
        with_id = df[ids.str.len() > 0].copy()
        without_id = df[ids.str.len() == 0].copy()
        if not with_id.empty:
            sort_cols = [c for c in ["message_id", "publication_time", "version"] if c in with_id.columns]
            with_id = with_id.sort_values(sort_cols, na_position="first")
            with_id = with_id.groupby("message_id", as_index=False).tail(1)
        df = pd.concat([with_id, without_id], ignore_index=True)

    if "affected_capacity" in df.columns:
        df["affected_capacity"] = pd.to_numeric(df["affected_capacity"], errors="coerce")
    if "installed_capacity" in df.columns:
        df["installed_capacity"] = pd.to_numeric(df["installed_capacity"], errors="coerce")
    if "available_capacity" in df.columns:
        df["available_capacity"] = pd.to_numeric(df["available_capacity"], errors="coerce")

    now = pd.Timestamp.now(tz="UTC")
    starts = df["event_start"] if "event_start" in df.columns else pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    ends = df["event_end"] if "event_end" in df.columns else pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    active = df[(starts.isna() | (starts <= now)) & (ends.isna() | (ends >= now))].copy()
    return df, active


# --- 2. PÄIS, AUTO-REFRESH JA ÜHTNE PERIOODIVALIK ---

col_title, col_ctrl = st.columns([3, 2])
with col_title:
    st.title("Energiaturu ja reservide reaalaja armatuurlaud")
    st.caption("Build 6.0 • UMM deploy-safe")
    st.caption(f"Käivitusfail: {Path(__file__).name}")
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

current_tallinn_time = datetime.now(timezone.utc).astimezone(TALLINN_TZ).strftime("%H:%M:%S")
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

with st.spinner("Laadin ametlikke andmeid (Elering, ENTSO-E, Nord Pool UMM, GIE, EEX, EIA, BTD)..."):
    df_short_all = fetch_elering_regional_short_term()
    df_raw_multi, df_daily_multi, df_monthly_multi = fetch_elering_long_history_multi(years=5)
    df_ttf_full = fetch_realtime_commodity_data("NATURAL_GAS", "TTF")
    df_getbaltic_full = fetch_getbaltic_history(df_ttf_full)
    df_brent_full = fetch_realtime_commodity_data("BRENT", "BRENT")
    df_co2_full = fetch_realtime_commodity_data("CARBON", "CO2")
    df_res_short, df_res_hist, df_res_monthly = fetch_frequency_reserves_full()
    df_generation, is_live_entsoe = fetch_entsoe_generation_data()
    gas_storage = fetch_gas_storage_data()
    entsoe_ytd = fetch_entsoe_ytd_energy()
    eurostat_prices = fetch_eurostat_electricity_prices()
    cap_5y_live = fetch_entsoe_installed_wind_solar([2022, 2023, 2024, 2025, 2026])
    gas_ytd = fetch_eurostat_gas_ytd()
    umm_rows, umm_meta = fetch_nordpool_umm()

umm_df, active_umm = normalize_umm_dataframe(umm_rows)

cutoff_dt = pd.to_datetime(datetime.now().date() - timedelta(days=selected_days))

df_daily_filtered = (
    df_daily_multi[df_daily_multi["date"] >= cutoff_dt]
    if not df_daily_multi.empty
    else pd.DataFrame()
)
df_ttf_filtered = (
    df_ttf_full[df_ttf_full["Date"] >= cutoff_dt]
    if not df_ttf_full.empty and "Date" in df_ttf_full.columns
    else pd.DataFrame()
)
df_getbaltic_filtered = (
    df_getbaltic_full[df_getbaltic_full["Date"] >= cutoff_dt]
    if not df_getbaltic_full.empty and "Date" in df_getbaltic_full.columns
    else pd.DataFrame()
)
df_brent_filtered = (
    df_brent_full[df_brent_full["Date"] >= cutoff_dt]
    if not df_brent_full.empty and "Date" in df_brent_full.columns
    else pd.DataFrame()
)
df_co2_filtered = (
    df_co2_full[df_co2_full["Date"] >= cutoff_dt]
    if not df_co2_full.empty and "Date" in df_co2_full.columns
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


def _fmt_metric(value, unit="", decimals=1):
    try:
        if pd.isna(value):
            return "Pole saadaval"
        return f"{float(value):.{decimals}f}{unit}"
    except Exception:
        return "Pole saadaval"


# --- 4. GRAAFIKUD JA VAHELEHED ---

tab_ee_core, tab_el, tab_umm, tab_gen, tab_gas, tab_reserves, tab_oil, tab_co2, tab_custom = st.tabs([
    "🇪🇪 Eesti energeetika",
    "⚡ Elekter (Regioon & Euroopa kaart)",
    "📣 Nord Pool UMM",
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
    c1.metric(
        label="Eesti elektritarbimine (YTD)",
        value=(f"{entsoe_ytd['consumption_twh']:.2f} TWh" if entsoe_ytd else "Pole saadaval"),
    )
    c2.metric(
        label="Kodumaine tootmine kokku",
        value=(f"{entsoe_ytd['production_twh']:.2f} TWh" if entsoe_ytd else "Pole saadaval"),
    )
    c3.metric(
        label="Taastuvenergia toodang",
        value=(f"{entsoe_ytd['renewable_twh']:.2f} TWh" if entsoe_ytd else "Pole saadaval"),
    )
    c4.metric(
        label="Mittetaastuv tootmine",
        value=(f"{entsoe_ytd['nonrenewable_twh']:.2f} TWh" if entsoe_ytd else "Pole saadaval"),
    )
    st.markdown("📍 **Allikas:** [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) – actual load ja actual generation per production type")

    st.markdown("---")

    st.markdown("#### 2. Elektri lõpphind Läänemere riikides tarbijate lõikes (€/kWh, koos maksudega)")
    price_data = (
        eurostat_prices[["Riik", "Hind (€/kWh)", "Tarbijagrupp"]].copy()
        if not eurostat_prices.empty
        else pd.DataFrame({
            "Riik": ["Eesti", "Soome", "Läti", "Leedu", "Rootsi", "Poola", "Taani",
                     "Eesti", "Soome", "Läti", "Leedu", "Rootsi", "Poola", "Taani"],
            "Hind (€/kWh)": [float("nan")] * 14,
            "Tarbijagrupp": ["Kodutarbijad"]*7 + ["Äritarbijad"]*7,
        })
    )
    
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
    st.markdown("📍 **Allikas:** Eurostat nrg_pc_204 (kodutarbijad, band DC) ja nrg_pc_205 (mitte-kodutarbijad, band IC), viimane avaldatud poolaasta, kõik maksud ja lõivud sees")

    st.markdown("---")

    st.markdown("#### 3. Installeeritud tootmisvõimsused viimase 5 aasta lõikes (MW)")
    cap_5y = (
        cap_5y_live.copy()
        if not cap_5y_live.empty
        else pd.DataFrame({
            "Aasta": ["2022", "2023", "2024", "2025", "2026"],
            "Tuuleenergia (MW)": [float("nan")] * 5,
            "Päikeseenergia (MW)": [float("nan")] * 5,
            "Põlevkivi ja muud (MW)": [float("nan")] * 5,
            "Maagaas / Koostootmine (MW)": [float("nan")] * 5,
        })
    )
    st.dataframe(cap_5y, hide_index=True, use_container_width=True)
    st.markdown("📍 **Allikas:** [ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) A68/A33 installed generation capacity per production type. Ainult üheselt võrreldavad tuule ja päikese kategooriad täidetakse automaatselt.")

    st.markdown("---")

    st.markdown("#### 4. Eesti võrku lisandunud uus tootmisvõimsus aastate lõikes (MW)")
    fig_new_cap = go.Figure()
    years_10 = ["2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026 (YTD)"]
    wind_added = [float("nan")] * len(years_10)
    solar_added = [float("nan")] * len(years_10)

    fig_new_cap.add_trace(go.Bar(name="Tuuleenergia lisandunud (MW)", x=years_10, y=wind_added, marker_color="#1f77b4"))
    fig_new_cap.add_trace(go.Bar(name="Päikeseenergia lisandunud (MW)", x=years_10, y=solar_added, marker_color="#ff7f0e"))
    fig_new_cap.update_layout(barmode="stack", title="Uute taastuvenergia võimsuste turule tulek (2017–2026)", xaxis_title="Aasta", yaxis_title="Lisandunud võimsus (MW)")
    st.plotly_chart(fig_new_cap, use_container_width=True)
    st.markdown("📍 **Allikas:** [Elering AS Andmebaas ja turuülevaated](https://elering.ee/)")

    st.markdown("---")

    st.markdown("#### 5. Maagaasi tarbimine (jooksva aasta maht vs eelmise aasta sama periood)")
    gc1, gc2, gc3 = st.columns(3)
    gc1.metric(
        label="Maagaasi tarbimine (YTD 2026)",
        value=(f"{gas_ytd['current_twh']:.2f} TWh" if gas_ytd else "Pole saadaval"),
        help=("Eurostat nrg_cb_gasm: jaanuar kuni viimase avaldatud kuuni" if gas_ytd else None),
    )
    gc2.metric(
        label="Maagaasi tarbimine (YTD 2025)",
        value=(f"{gas_ytd['previous_twh']:.2f} TWh" if gas_ytd and pd.notna(gas_ytd['previous_twh']) else "Pole saadaval"),
        help=("Sama kuude arv nagu jooksval aastal" if gas_ytd else None),
    )
    gc3.metric(
        label="Aastane muutus",
        value=(f"{gas_ytd['change_pct']:+.1f} %" if gas_ytd and pd.notna(gas_ytd['change_pct']) else "Pole saadaval"),
        delta_color="inverse",
    )
    st.markdown("📍 **Allikas:** Eurostat nrg_cb_gasm, G3000 natural gas, IC_CAL_MG calculated inland consumption, TJ_GCV")

    st.markdown("---")

    st.markdown("#### 6. Eestisse tehtud energeetika investeeringud (M€, Statistikaamet)")
    inv_data = pd.DataFrame({
        "Aasta": ["2021", "2022", "2023", "2024", "2025"],
        "Võrgud ja taristu (M€)": [float("nan")] * 5,
        "Taastuvenergia projektid (M€)": [float("nan")] * 5,
        "Energiatõhusus ja tootmine (M€)": [float("nan")] * 5,
        "Kokku investeeringuid (M€)": [float("nan")] * 5,
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
    st.markdown("### 🏭 Eesti elektrisüsteemi voogude reaalaja ülevaade (Elering Dashboard stiilis)")
    if is_live_entsoe:
        st.success("🟢 Reaalajas ühendatud ENTSO-E Transparency REST API-ga")
    else:
        st.info("ℹ️ Kuvatakse Eesti tootmissüsteemi struktuurne jaotus. Reaalaja otseliideseks lisa Streamliti saladustesse `ENTSOE_API_KEY`.")

    st.markdown("#### ⚡ Reaalaja süsteemivoogude joongraafik (Tarbimine, Taastuvad, Fossiil, Import Soomest ja Lätist)")

    if not df_generation.empty:
        df_elering_line = df_generation.copy()
        safe_tech_c = [c for c in df_elering_line.columns if c != "time_local"]
        
        df_elering_line["Taastuvad"] = sum(df_elering_line[c] for c in safe_tech_c if any(k in c.lower() for k in ["tuul", "wind", "solar", "päike", "biomass", "hydro", "hüdro"]))
        df_elering_line["Fossiil / muu"] = sum(df_elering_line[c] for c in safe_tech_c if not any(k in c.lower() for k in ["tuul", "wind", "solar", "päike", "biomass", "hydro", "hüdro"]))
        df_elering_line["Siseriiklik tootmine"] = df_elering_line["Taastuvad"] + df_elering_line["Fossiil / muu"]
        
        _aux_end = datetime.now(timezone.utc)
        _aux_start = _aux_end - timedelta(hours=48)
        _load = _fetch_entsoe_actual_load(_aux_start, _aux_end)
        _fi_net = _latest_directional_net(
            _fetch_entsoe_flow(_aux_start, _aux_end, "EE", "FI"),
            _fetch_entsoe_flow(_aux_start, _aux_end, "FI", "EE"),
        )
        _lv_net = _latest_directional_net(
            _fetch_entsoe_flow(_aux_start, _aux_end, "EE", "LV"),
            _fetch_entsoe_flow(_aux_start, _aux_end, "LV", "EE"),
        )

        base = df_elering_line.sort_values("time_local")
        if not _load.empty:
            base = pd.merge_asof(
                base,
                _load[["time_local", "load_mw"]].sort_values("time_local"),
                on="time_local",
                direction="nearest",
                tolerance=pd.Timedelta("30min"),
            )
            base["Tarbimine"] = base["load_mw"]
        else:
            base["Tarbimine"] = float("nan")

        if not _fi_net.empty:
            base = pd.merge_asof(
                base, _fi_net.sort_values("time_local"),
                on="time_local", direction="nearest",
                tolerance=pd.Timedelta("30min"),
            )
            base["Import Soomest"] = (-base["net_mw"]).clip(lower=0)
            base = base.drop(columns=["net_mw"], errors="ignore")
        else:
            base["Import Soomest"] = float("nan")

        if not _lv_net.empty:
            base = pd.merge_asof(
                base, _lv_net.sort_values("time_local"),
                on="time_local", direction="nearest",
                tolerance=pd.Timedelta("30min"),
            )
            base["Import Lätist"] = (-base["net_mw"]).clip(lower=0)
            base = base.drop(columns=["net_mw"], errors="ignore")
        else:
            base["Import Lätist"] = float("nan")

        df_elering_line = base

        fig_line_elering = go.Figure()
        
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Tarbimine"],
            mode="lines", name="Tarbimine", line=dict(color="#d62728", width=3)
        ))
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Taastuvad"],
            mode="lines", name="Taastuvad", line=dict(color="#2ca02c", width=2.5)
        ))
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Fossiil / muu"],
            mode="lines", name="Fossiil / muu", line=dict(color="#7f7f7f", width=2, dash="dash")
        ))
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Import Soomest"],
            mode="lines", name="Import Soomest", line=dict(color="#1f77b4", width=2, dash="dot")
        ))
        fig_line_elering.add_trace(go.Scatter(
            x=df_elering_line["time_local"], y=df_elering_line["Import Lätist"],
            mode="lines", name="Import Lätist", line=dict(color="#ff7f0e", width=2, dash="dot")
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
            value=_fmt_metric(gas_storage["eu_fill_pct"], " %"),
            help="Euroopa Liidu maa-aluste gaasihoidlate keskmine täituvus (GIE AGSI)",
        )
    with col_sto2:
        st.metric(
            label="EL27 gaasihoidlate maht",
            value=_fmt_metric(gas_storage["eu_stored_twh"], " TWh"),
            delta=("/ " + _fmt_metric(gas_storage["eu_capacity_twh"], " TWh kokku")) if pd.notna(gas_storage["eu_capacity_twh"]) else None,
            delta_color="off",
        )
    with col_sto3:
        st.metric(
            label="Läti Inčukalns UGS täituvus (%)",
            value=_fmt_metric(gas_storage["latvia_fill_pct"], " %"),
            help="Inčukalnsi varu: Conexus Baltic Grid Storage Stocks; GIE AGSI+ ainult varuallikas",
        )
    with col_sto4:
        st.metric(
            label="Läti Inčukalnsi talletatud gaas",
            value=_fmt_metric(gas_storage["latvia_stored_twh"], " TWh"),
            delta=("/ " + _fmt_metric(gas_storage["latvia_capacity_twh"], " TWh aktiivne maht")) if pd.notna(gas_storage["latvia_capacity_twh"]) else None,
            delta_color="off",
        )
    st.markdown("📍 **Allikas:** EL27: [GIE AGSI+](https://agsi.gie.eu/) / Inčukalns: [Conexus Baltic Grid Storage Stocks](https://www.conexus.lv/storage-stocks)")

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
        st.markdown("📍 **Allikas:** TTF: [EEX Neutral Gas Price](https://www.eex.com/en/markets/natural-gas/gas-market-transparency); GET Baltic kuvatakse ainult valideeritud andmevoo olemasolul.")


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
            title=f"Brent toornafta spot-hinnad ({selected_period_label})",
        )
        fig_brent.update_traces(line_color="#1E90FF")
        st.plotly_chart(fig_brent, use_container_width=True)
        st.markdown("📍 **Allikas:** [U.S. EIA Europe Brent Spot Price FOB](https://www.eia.gov/dnav/pet/hist/rbrteD.htm)")


# --- VAHELEHT 6: EU ETS CO2 KVOOT ---
with tab_co2:
    if not df_co2_filtered.empty:
        fig_co2 = px.line(
            df_co2_filtered,
            x="Date",
            y="Close",
            labels={"Date": "Kuupäev", "Close": "Hind (€/tCO₂)"},
            title=f"EU ETS heitmekvoodi (EUA) primaaroksjoni clearing-hinnad ({selected_period_label})",
        )
        fig_co2.update_traces(line_color="#2E8B57")
        st.plotly_chart(fig_co2, use_container_width=True)
        st.markdown("📍 **Allikas:** [EEX EUA Primary Market Auction Report](https://www.eex.com/en/market-data/market-data-hub/environmentals/eex-eua-primary-auction-spot-download)")



# --- VAHELEHT 7: NORD POOL UMM ---
with tab_umm:
    st.markdown("### 📣 Nord Pool UMM — kiireloomulised turuteated")
    st.caption(
        "Nord Pool UMM on REMIT Article 4 avaldamiskanal. "
        "Mõjutatud MW on teatepõhine; eri UMM-ide võimsusi ei summeerita süsteemi netokatkestuseks."
    )

    if umm_meta.get("fallback"):
        st.warning(
            "Nord Pool UMM otsepäring ei vastanud; kuvatakse viimast GitHub Actionsi snapshot'i. "
            f"Snapshot: {umm_meta.get('fetched_at') or 'aeg teadmata'}."
        )
    elif umm_meta.get("error"):
        st.error(f"Nord Pool UMM andmed pole saadaval: {umm_meta.get('error')}")
    else:
        status = umm_meta.get("status_code")
        fetched = umm_meta.get("fetched_at")
        st.caption(f"Andmeallikas: Nord Pool UMM REST API · HTTP {status or '—'} · päring {fetched or '—'}")

    if umm_df.empty:
        st.info("UMM teateid ei ole hetkel võimalik kuvada.")
    else:
        only_active = st.toggle("Ainult aktiivsed", value=True, key="umm_only_active")
        u = active_umm.copy() if only_active else umm_df.copy()

        areas = []
        if "area" in u.columns:
            areas = sorted(
                x for x in u["area"].dropna().astype(str).unique()
                if x and x.lower() not in {"nan", "none"}
            )
        area_sel = st.multiselect("Piirkond", areas, default=[], key="umm_area_filter")
        if area_sel:
            u = u[u["area"].astype(str).isin(area_sel)]

        if "affected_capacity" in u.columns:
            sort_cols = ["affected_capacity"]
            if "publication_time" in u.columns:
                sort_cols.append("publication_time")
            u = u.sort_values(sort_cols, ascending=False, na_position="last")
        elif "publication_time" in u.columns:
            u = u.sort_values("publication_time", ascending=False, na_position="last")

        cols = [
            c for c in [
                "area", "asset_name", "market_participant", "status", "message_type",
                "affected_capacity", "installed_capacity", "available_capacity",
                "publication_time", "event_start", "event_end", "reason", "source_url"
            ] if c in u.columns
        ]

        st.dataframe(
            u[cols],
            hide_index=True,
            use_container_width=True,
            column_config={
                "area": "Piirkond",
                "asset_name": "Vara / seade",
                "market_participant": "Turuosaline",
                "status": "Staatus",
                "message_type": "Teate tüüp",
                "affected_capacity": st.column_config.NumberColumn("Mõjutatud MW", format="%.0f"),
                "installed_capacity": st.column_config.NumberColumn("Installeeritud MW", format="%.0f"),
                "available_capacity": st.column_config.NumberColumn("Saadaval MW", format="%.0f"),
                "publication_time": st.column_config.DatetimeColumn("Avaldatud", format="DD.MM.YYYY HH:mm"),
                "event_start": st.column_config.DatetimeColumn("Algus", format="DD.MM.YYYY HH:mm"),
                "event_end": st.column_config.DatetimeColumn("Lõpp", format="DD.MM.YYYY HH:mm"),
                "reason": "Põhjus / kirjeldus",
                "source_url": st.column_config.LinkColumn("Nord Pool"),
            },
        )

        st.caption(
            "Mõjutatud MW: kasutatakse UMM-is raporteeritud unavailable capacity väärtust; "
            "kui see puudub, arvutatakse ainult juhul, kui nii installed kui available capacity on teates olemas. "
            "Puuduvaid võimsusi ei oletata."
        )


# --- VAHELEHT 8: KOHANDATUD PERIOODIPÄRING ---
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

    st.markdown("📍 **Allikas:** Elering / ENTSO-E / GIE AGSI+ / Baltic Transparency Dashboard / EEX / U.S. EIA.")