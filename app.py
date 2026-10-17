"""
Ichimoku Cloud Alerts -> Phone notification (ntfy)  |  single-file Streamlit app

Run locally :  pip install streamlit yfinance pandas requests
               python -m streamlit run app.py
Secrets     :  NTFY_TOPIC = "your-topic-name"   (Streamlit Cloud: Manage app -> Settings -> Secrets)
Stop alerts :  press Stop in the dashboard, tap the "Action taken" button on a notification,
               or publish the text  action taken  to your ntfy topic.
"""
import os, json, time, datetime, threading
import requests
import pandas as pd
import yfinance as yf
import streamlit as st

st.set_page_config(page_title="Ichimoku Cloud Alerts", page_icon="☁️", layout="wide")

# ============ CONFIG ============
CHECK_EVERY_SECONDS = 5 * 60
STOP_POLL_SECONDS = 10          # how often to look for "action taken" on ntfy
CONFIG_FILE = "dashboard_config.json"
SIGNAL_FILE = "whatsapp_signal.txt"   # local "action taken" file (optional, unchanged)

TENKAN_PERIOD, KIJUN_PERIOD, SENKOU_B_PERIOD, DISPLACEMENT = 9, 26, 52, 26
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_START = datetime.time(9, 15)
MARKET_END = datetime.time(15, 30)

# Yahoo Finance tickers. BSE ones (except Sensex) are best-guess: check them on Yahoo once.
INDICES_BY_SEGMENT = {
    "NSE": {
        "Nifty 50": "^NSEI",
        "Nifty 200": "^CNX200",
        "Nifty 100": "^CNX100",
        "Nifty Midcap 150": "NIFTYMIDCAP150.NS",
        "Nifty Smallcap 250": "NIFTYSMLCAP250.NS",
        "Nifty Next 50": "^NSMIDCP",
    },
    "BSE": {
        "Sensex": "^BSESN",
        "BSE 100": "BSE-100.BO",
        "BSE 200": "BSE-200.BO",
        "BSE 500": "BSE-500.BO",
        "BSE Midcap": "BSE-MIDCAP.BO",
        "BSE Smallcap": "BSE-SMLCAP.BO",
    },
}
SEGMENTS = list(INDICES_BY_SEGMENT.keys())
INDICES = {n: t for seg in INDICES_BY_SEGMENT.values() for n, t in seg.items()}
SEGMENT_OF = {n: s for s, d in INDICES_BY_SEGMENT.items() for n in d}

TFS = {"30m": "30 Minutes", "1h": "1 Hour", "4h": "4 Hours", "1d": "1 Day"}
ZONES = {
    "above": "Above Cloud",
    "near_top": "Near Cloud Top",
    "inside": "Inside Cloud",
    "near_bottom": "Near Cloud Bottom",
    "below": "Below Cloud",
}
DEFAULT_CFG = {"segments": ["NSE"], "indices": ["Nifty 50"], "timeframes": ["4h"],
               "zones": ["above", "below"], "near_pct": 1}
# ================================


# Shared state must survive Streamlit reruns -> cache_resource
@st.cache_resource(show_spinner=False)
def get_shared():
    return {"running": False, "last_scan": None, "rows": [], "since": 0}


bot = get_shared()


def _secret(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:
        return str(st.secrets[name])
    except Exception:
        return default


NTFY_TOPICS = [x.strip() for x in _secret("NTFY_TOPIC").split(",") if x.strip()]


def now_ist():
    return datetime.datetime.now(IST).replace(tzinfo=None)


def in_market_hours():
    t = now_ist()
    return t.weekday() < 5 and MARKET_START <= t.time() <= MARKET_END  # holidays not covered


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def check_action_taken_signal() -> bool:
    if os.path.exists(SIGNAL_FILE):
        try:
            with open(SIGNAL_FILE, "r", encoding="utf-8") as f:
                return "action taken" in f.read().strip().lower()
        except Exception:
            pass
    return False


# ---------- Ichimoku ----------
def compute_ichimoku(df):
    high, low = df["High"], df["Low"]
    tenkan = (high.rolling(TENKAN_PERIOD).max() + low.rolling(TENKAN_PERIOD).min()) / 2
    kijun = (high.rolling(KIJUN_PERIOD).max() + low.rolling(KIJUN_PERIOD).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(DISPLACEMENT)
    senkou_b = ((high.rolling(SENKOU_B_PERIOD).max() +
                 low.rolling(SENKOU_B_PERIOD).min()) / 2).shift(DISPLACEMENT)
    df = df.copy()
    df["senkou_a"], df["senkou_b"] = senkou_a, senkou_b
    df["cloud_top"] = df[["senkou_a", "senkou_b"]].max(axis=1)
    df["cloud_bottom"] = df[["senkou_a", "senkou_b"]].min(axis=1)
    return df


def get_data(ticker, tf):
    if tf == "1d":
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
    elif tf == "30m":
        df = yf.download(ticker, period="60d", interval="30m", progress=False)
    else:  # "1h" and "4h" both start from 60m candles
        df = yf.download(ticker, period="60d", interval="60m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["High", "Low", "Close"])
    if tf == "4h":
        frames = []
        for _, day_df in df.groupby(df.index.date):
            frames.append(day_df.resample("4h", origin="start").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna())
        df = pd.concat(frames).sort_index()
    return df


def cloud_levels(df):
    last = compute_ichimoku(df).iloc[-1]
    return float(last["cloud_top"]), float(last["cloud_bottom"])


def live_price(ticker):
    try:
        return round(float(yf.Ticker(ticker).fast_info["last_price"]), 2)
    except Exception:
        return None


def classify_zone(price, cloud_top, cloud_bottom, near):
    if price > cloud_top:
        return "above"
    elif price >= cloud_top * (1 - near):
        return "near_top"
    elif price < cloud_bottom:
        return "below"
    elif price <= cloud_bottom * (1 + near):
        return "near_bottom"
    return "inside"


MESSAGES = {
    "near_bottom": "🟠 {name} ({tf})\nCloud BOTTOM line-ku {pct}% arukila vandhuduchu -> cloud-ku keela vara pogudhu!\nLive price: {price}\nCloud bottom: {cloud_bottom}",
    "below": "🔴 {name} ({tf}) - BELOW CLOUD\nLive price cloud-ku keela POYIDUCHU!\nLive price: {price}\nCloud: {cloud_bottom} - {cloud_top}",
    "near_top": "🟡 {name} ({tf})\nCloud TOP line-ku {pct}% arukila vandhuduchu -> cloud-ku mela poga pogudhu!\nLive price: {price}\nCloud top: {cloud_top}",
    "above": "🟢 {name} ({tf}) - ABOVE CLOUD\nLive price cloud-ku mela POYIDUCHU!\nLive price: {price}\nCloud: {cloud_bottom} - {cloud_top}",
    "inside": "⚪ {name} ({tf})\nCloud-ku ULLA irukku, neither zone-um illa (neutral).\nLive price: {price}\nCloud: {cloud_bottom} - {cloud_top}",
}


# ---------- ntfy ----------
def active_channels():
    return ["Phone notification (ntfy)"] if NTFY_TOPICS else []


def send_alert(message, with_button=True):
    """Push notification through ntfy. The 'Action taken' button posts 'action taken' to the topic."""
    def worker():
        for topic in NTFY_TOPICS:
            headers = {"Title": "Ichimoku Cloud Alert", "Priority": "high"}
            if with_button:
                headers["Actions"] = (f"http, Action taken, https://ntfy.sh/{topic}, "
                                      f"method=POST, body=action taken, clear=true")
            try:
                r = requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                  headers=headers, timeout=20)
                print("ntfy", topic, r.status_code)
            except Exception as e:
                print("ntfy error:", e)

    if NTFY_TOPICS:
        threading.Thread(target=worker, daemon=True).start()
    else:
        print("No NTFY_TOPIC set - notification not sent")


def ntfy_stop_requested(since):
    """True if somebody published 'action taken' to the topic after `since` (unix seconds)."""
    for topic in NTFY_TOPICS:
        try:
            r = requests.get(f"https://ntfy.sh/{topic}/json",
                             params={"poll": "1", "since": str(int(since))}, timeout=15)
            for line in r.text.splitlines():
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if (m.get("event") == "message" and m.get("time", 0) >= int(since)
                        and "action taken" in str(m.get("message", "")).lower()):
                    return True
        except Exception as e:
            print("ntfy poll error:", e)
    return False


def stop_bot(reason):
    bot["running"] = False
    print("Alerts stopped:", reason)
    send_alert("✅ Alerts stopped (" + reason + "). Dashboard-la irundhu thirumba Start pannalaam.",
               with_button=False)


# ---------- scan / loop ----------
def scan(force=False):
    """One pass over every selected index x timeframe.
    Notification goes out only when the price is in one of the selected zones."""
    cfg = load_json(CONFIG_FILE, DEFAULT_CFG)
    near_pct = int(cfg.get("near_pct", 1))
    near = near_pct / 100
    market_open = in_market_hours()
    now = now_ist()
    rows, alerts = [], []

    for name in cfg["indices"]:
        price = live_price(INDICES[name])
        for tf in cfg["timeframes"]:
            row = {"index": name, "tf": tf, "price": price, "zone": None,
                   "top": None, "bottom": None, "error": None}
            rows.append(row)
            if price is None:
                row["error"] = "no live price"
                continue
            try:
                top, bottom = cloud_levels(get_data(INDICES[name], tf))
            except Exception as e:
                row["error"] = f"data error: {e}"
                continue

            zone = classify_zone(price, top, bottom, near)
            row.update(zone=zone, top=round(top, 2), bottom=round(bottom, 2))
            print(f"[{name}|{tf}] price={price} cloud=({bottom:.2f}-{top:.2f}) zone={zone}")

            if zone in cfg["zones"]:  # only the zones the user selected
                msg = MESSAGES[zone].format(name=name, tf=tf.upper(), price=price,
                                            cloud_top=round(top, 2), cloud_bottom=round(bottom, 2),
                                            pct=near_pct)
                if force:
                    msg = "🔎 MANUAL CHECK\n" + msg
                if not market_open:
                    msg += "\n(market closed - last close price)"
                alerts.append(msg)
                row["alerted"] = True

    if alerts:  # one combined notification per scan
        send_alert("\n\n".join(alerts))
    bot["rows"], bot["last_scan"] = rows, now.strftime("%d %b %H:%M:%S")


def monitor_loop():
    while bot["running"]:
        if check_action_taken_signal():
            stop_bot("signal file")
            break
        if NTFY_TOPICS and ntfy_stop_requested(bot["since"]):
            stop_bot("action taken")
            break
        try:
            scan()
        except Exception as e:
            print("scan error:", e)
        waited = 0
        while bot["running"] and waited < CHECK_EVERY_SECONDS:
            time.sleep(1)
            waited += 1
            if waited % STOP_POLL_SECONDS == 0 and NTFY_TOPICS and ntfy_stop_requested(bot["since"]):
                stop_bot("action taken")
                break


# ====================== STREAMLIT UI ======================
ZONE_STYLE = {
    "above": "background-color:#DCFCE7;color:#16A34A;font-weight:600",
    "near_top": "background-color:#FEF3C7;color:#D97706;font-weight:600",
    "inside": "background-color:#F3F4F6;color:#6B7280;font-weight:600",
    "near_bottom": "background-color:#FFEDD5;color:#C2410C;font-weight:600",
    "below": "background-color:#FEE2E2;color:#DC2626;font-weight:600",
}

st.markdown(
    "<div style='background:#131B3B;padding:18px 22px;border-radius:10px;margin-bottom:18px'>"
    "<h2 style='color:#fff;margin:0;font-size:22px'>Ichimoku Cloud Alerts &rarr; Phone Notification</h2></div>",
    unsafe_allow_html=True,
)

# ---- keep every checkbox value alive across reruns (even when its row is hidden) ----
saved = load_json(CONFIG_FILE, DEFAULT_CFG)
saved_segments = saved.get("segments") or sorted({SEGMENT_OF[i] for i in saved.get("indices", []) if i in SEGMENT_OF}) or ["NSE"]


def keep(key, default):
    st.session_state[key] = st.session_state.get(key, default)


for s_ in SEGMENTS:
    keep(f"seg_{s_}", s_ in saved_segments)
for n_ in INDICES:
    keep(f"idx_{n_}", n_ in saved.get("indices", []))
for t_ in TFS:
    keep(f"tf_{t_}", t_ in saved.get("timeframes", []))
for z_ in ZONES:
    keep(f"zn_{z_}", z_ in saved.get("zones", []))
try:
    _np = min(10, max(1, int(saved.get("near_pct", 1))))
except Exception:
    _np = 1
keep("near_pct", _np)


def checkbox_row(items, prefix):
    cols = st.columns(len(items))
    for col, (k, label) in zip(cols, items):
        col.checkbox(label, key=f"{prefix}_{k}")


with st.container(border=True):
    st.markdown("**Segment**")
    checkbox_row([(s, s) for s in SEGMENTS], "seg")
    sel_segments = [s for s in SEGMENTS if st.session_state[f"seg_{s}"]]

    st.markdown("**Index**")
    if not sel_segments:
        st.caption("Select a segment first (NSE / BSE).")
    for seg in sel_segments:
        st.caption(seg)
        checkbox_row([(n, n) for n in INDICES_BY_SEGMENT[seg]], "idx")

    st.markdown("**Timeframe**")
    checkbox_row([(k, v) for k, v in TFS.items()], "tf")

    st.markdown("**Cloud (alert me only when price is)**")
    checkbox_row([(k, v) for k, v in ZONES.items()], "zn")

    st.markdown("**Target % (how close is \"Near Cloud Top / Bottom\")**")
    st.radio("Target %", list(range(1, 11)), key="near_pct", horizontal=True,
             format_func=lambda x: f"{x}%", label_visibility="collapsed")

    cfg = {
        "segments": sel_segments,
        "indices": [n for n in INDICES if SEGMENT_OF[n] in sel_segments and st.session_state[f"idx_{n}"]],
        "timeframes": [t for t in TFS if st.session_state[f"tf_{t}"]],
        "zones": [z for z in ZONES if st.session_state[f"zn_{z}"]],
        "near_pct": int(st.session_state["near_pct"]),
    }
    ok = bool(cfg["indices"] and cfg["timeframes"] and cfg["zones"])

    if active_channels():
        st.caption("Alert channel: " + ", ".join(active_channels()) +
                   "  ·  Stop: Stop button / notification-la \"Action taken\" button / ntfy-la \"action taken\" nu send pannunga")
    else:
        st.warning("NTFY_TOPIC is not set in Secrets - notifications will not be sent.")

    c1, c2, _ = st.columns([1, 1, 6])

    if not bot["running"]:
        if c1.button("Start alerts", type="primary", disabled=not ok, use_container_width=True):
            save_json(CONFIG_FILE, cfg)
            open(SIGNAL_FILE, "w").close()          # clear old local 'action taken'
            bot["since"] = int(time.time())          # ignore old 'action taken' messages on ntfy
            bot["running"] = True
            threading.Thread(target=monitor_loop, daemon=True).start()
            st.rerun()
    else:
        if c1.button("Stop alerts", type="primary", use_container_width=True):
            bot["running"] = False
            st.rerun()

    if c2.button("Check now", disabled=not ok, use_container_width=True):
        save_json(CONFIG_FILE, cfg)
        with st.spinner("Checking..."):
            scan(force=True)
        st.rerun()

if ok and bot["running"]:
    save_json(CONFIG_FILE, cfg)  # keep saved config in sync while running


@st.fragment(run_every=10)
def live_panel():
    dot = "🟢" if bot["running"] else "⚪"
    state_txt = "Alerts running" if bot["running"] else "Alerts stopped"
    market = ("open" if in_market_hours()
              else "closed (Mon-Fri 9:15-15:30 IST) - prices shown are the last close")
    st.info(f"{dot} **{state_txt}** · Market {market} · Last scan: {bot['last_scan'] or '-'}")

    rows = [r for r in bot["rows"]
            if r["index"] in cfg["indices"] and r["tf"] in cfg["timeframes"]]
    rows.sort(key=lambda r: (list(INDICES).index(r["index"]), list(TFS).index(r["tf"])))

    if not rows:
        st.markdown(
            "<div style='border:1px dashed #D1D5DB;border-radius:10px;padding:32px;"
            "text-align:center;color:#6B7280'>Choose segment, index, timeframe and cloud zone, "
            "then press <b>Start alerts</b> or <b>Check now</b>.</div>",
            unsafe_allow_html=True,
        )
        return

    data = []
    for r in rows:
        if r.get("error"):
            data.append({"Index": r["index"], "Timeframe": r["tf"].upper(), "Live price": "",
                         "Cloud bottom": "", "Cloud top": "", "Zone": "", "Notification": r["error"]})
            continue
        if r["zone"] in cfg["zones"]:
            alert = "Sent" if r.get("alerted") else "Watching"
        else:
            alert = "-"
        data.append({
            "Index": r["index"], "Timeframe": r["tf"].upper(),
            "Live price": r["price"], "Cloud bottom": r["bottom"], "Cloud top": r["top"],
            "Zone": ZONES[r["zone"]], "Notification": alert,
        })

    df = pd.DataFrame(data)
    label_to_key = {v: k for k, v in ZONES.items()}
    num = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else v
    styled = (df.style.map(lambda v: ZONE_STYLE.get(label_to_key.get(v), ""), subset=["Zone"])
                      .format(num, subset=["Live price", "Cloud bottom", "Cloud top"]))
    st.dataframe(styled, use_container_width=True, hide_index=True)


live_panel()
