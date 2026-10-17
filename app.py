"""
Ichimoku Cloud Alerts -> WhatsApp  (single-file Streamlit app)

Local run :  pip install streamlit yfinance pandas pywhatkit
             python -m streamlit run app.py
Cloud run :  requirements.txt = streamlit, yfinance, pandas
             (dashboard works; WhatsApp via pywhatkit only works on a PC with Chrome + WhatsApp Web)
"""
import os, json, time, datetime, threading
import requests
import pandas as pd
import yfinance as yf
import streamlit as st

st.set_page_config(page_title="Ichimoku Cloud Alerts", page_icon="☁️", layout="wide")

# ============ CONFIG ============
WHATSAPP_NUMBER = "+916381316654"
CHECK_EVERY_SECONDS = 5 * 60
HEARTBEAT_SECONDS = 30 * 60
NEAR_PCT = 0.01
CONFIG_FILE = "dashboard_config.json"
STATE_FILE = "nifty_ichimoku_alert_state.json"
SIGNAL_FILE = "whatsapp_signal.txt"

TENKAN_PERIOD, KIJUN_PERIOD, SENKOU_B_PERIOD, DISPLACEMENT = 9, 26, 52, 26
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_START = datetime.time(9, 15)
MARKET_END = datetime.time(15, 30)

INDICES = {
    "Nifty 50": "^NSEI",
    "Nifty 200": "^CNX200",
    "Nifty 100": "^CNX100",
    "Nifty Midcap 150": "NIFTYMIDCAP150.NS",
    "Nifty Smallcap 250": "NIFTYSMLCAP250.NS",
    "Nifty Next 50": "^NSMIDCP",
}
DEFAULT_CFG = {"indices": ["Nifty 50"], "timeframes": ["4h"], "zones": ["above", "below"]}
# ================================


# State must survive Streamlit reruns -> keep it in cache_resource
@st.cache_resource(show_spinner=False)
def get_shared():
    return {"bot": {"running": False, "last_scan": None, "rows": [], "test_mode": False, "ignore_hours": False},
            "lock": threading.Lock()}


_shared = get_shared()
bot = _shared["bot"]
send_lock = _shared["lock"]


def _secret(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:
        return str(st.secrets[name])
    except Exception:
        return default


# Cloud-friendly alert channels (set in Streamlit "Secrets"; see instructions)
TELEGRAM_BOT_TOKEN = _secret("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = [x.strip() for x in _secret("TELEGRAM_CHAT_IDS").split(",") if x.strip()]
# NTFY_TOPIC: plain phone push notification (install the free "ntfy" app and subscribe to the same topic)
NTFY_TOPICS = [x.strip() for x in _secret("NTFY_TOPIC").split(",") if x.strip()]
# CALLMEBOT_KEYS format:  "+9190000000:apikey1,+9180000000:apikey2"
CALLMEBOT_KEYS = [x.strip() for x in _secret("CALLMEBOT_KEYS").split(",") if ":" in x]


def now_ist():
    return datetime.datetime.now(IST).replace(tzinfo=None)


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


def classify_zone(price, cloud_top, cloud_bottom):
    if price > cloud_top:
        return "above"
    elif price >= cloud_top * (1 - NEAR_PCT):
        return "near_top"
    elif price < cloud_bottom:
        return "below"
    elif price <= cloud_bottom * (1 + NEAR_PCT):
        return "near_bottom"
    return "inside"


MESSAGES = {
    "near_bottom": "🟠 {name} ({tf})\nCloud BOTTOM line-ku {pct}% arukila vandhuduchu -> cloud-ku keela vara pogudhu!\nLive price: {price}\nCloud bottom: {cloud_bottom}",
    "below": "🔴 {name} ({tf}) - BELOW CLOUD\nLive price cloud-ku keela POYIDUCHU!\nLive price: {price}\nCloud: {cloud_bottom} - {cloud_top}",
    "near_top": "🟡 {name} ({tf})\nCloud TOP line-ku {pct}% arukila vandhuduchu -> cloud-ku mela poga pogudhu!\nLive price: {price}\nCloud top: {cloud_top}",
    "above": "🟢 {name} ({tf}) - ABOVE CLOUD\nLive price cloud-ku mela POYIDUCHU!\nLive price: {price}\nCloud: {cloud_bottom} - {cloud_top}",
    "inside": "⚪ {name} ({tf})\nCloud-ku ULLA irukku, neither zone-um illa (neutral).\nLive price: {price}\nCloud: {cloud_bottom} - {cloud_top}",
}


def send_pywhatkit(message):
    def worker():
        with send_lock:  # pywhatkit is slow, so send one message at a time
            send_time = datetime.datetime.now() + datetime.timedelta(minutes=3)
            try:
                import pywhatkit as kit  # works only on a PC with Chrome + WhatsApp Web
                kit.sendwhatmsg(WHATSAPP_NUMBER, message, send_time.hour, send_time.minute,
                                wait_time=30, tab_close=True)
                print("WhatsApp message scheduled successfully.")
            except Exception as e:
                print(f"Error sending WhatsApp message: {e}")
    threading.Thread(target=worker, daemon=True).start()


def active_channels():
    ch = []
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS:
        ch.append(f"Telegram ({len(TELEGRAM_CHAT_IDS)} chat)")
    if CALLMEBOT_KEYS:
        ch.append(f"WhatsApp-CallMeBot ({len(CALLMEBOT_KEYS)} number)")
    if NTFY_TOPICS:
        ch.append("Phone notification (ntfy)")
    return ch


def send_alert(message):
    """Cloud channels if configured, otherwise fall back to pywhatkit (local PC only)."""
    def worker():
        for cid in TELEGRAM_CHAT_IDS if TELEGRAM_BOT_TOKEN else []:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data={"chat_id": cid, "text": message}, timeout=20)
                print("Telegram", cid, r.status_code)
            except Exception as e:
                print("Telegram error:", e)
        for topic in NTFY_TOPICS:
            try:
                r = requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                  headers={"Title": "Ichimoku Cloud Alert", "Priority": "high"},
                                  timeout=20)
                print("ntfy", topic, r.status_code)
            except Exception as e:
                print("ntfy error:", e)
        for item in CALLMEBOT_KEYS:
            phone, key = item.rsplit(":", 1)
            try:
                r = requests.get("https://api.callmebot.com/whatsapp.php",
                                 params={"phone": phone.strip(), "text": message, "apikey": key.strip()},
                                 timeout=30)
                print("CallMeBot", phone, r.status_code)
            except Exception as e:
                print("CallMeBot error:", e)

    if active_channels():
        threading.Thread(target=worker, daemon=True).start()
    else:
        send_pywhatkit(message)


def in_market_hours():
    t = now_ist()
    # Mon-Fri only (exchange holidays are not covered)
    return t.weekday() < 5 and MARKET_START <= t.time() <= MARKET_END


def scan(force=False):
    """One pass over every selected index x timeframe.
    force=True (Check now): ignore market hours and send if zone is in the selected zones."""
    cfg = load_json(CONFIG_FILE, DEFAULT_CFG)
    state = load_json(STATE_FILE, {})
    market_open = in_market_hours()
    test = bot.get("test_mode", False)
    any_hours = bot.get("ignore_hours", False)
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

            zone = classify_zone(price, top, bottom)
            row.update(zone=zone, top=round(top, 2), bottom=round(bottom, 2))
            key = f"{name}|{tf}"
            s = state.get(key, {})
            changed = zone != s.get("zone")
            last_sent = s.get("last_sent")
            due = (not last_sent) or (now - datetime.datetime.fromisoformat(last_sent)
                                      ).total_seconds() >= HEARTBEAT_SECONDS
            print(f"[{key}] price={price} cloud=({bottom:.2f}-{top:.2f}) zone={zone} changed={changed}")

            if market_open or test or any_hours or force:
                if test or (zone in cfg["zones"] and (force or changed or due)):
                    msg = MESSAGES[zone].format(name=name, tf=tf.upper(), price=price,
                                                cloud_top=round(top, 2), cloud_bottom=round(bottom, 2),
                                                pct=int(NEAR_PCT * 100))
                    if test:
                        msg = "🧪 TEST MODE\n" + msg
                    elif force:
                        msg = "🔎 MANUAL CHECK\n" + msg
                    elif not changed:
                        msg += "\n(periodic update - no change)"
                    if not market_open:
                        msg += "\n(market closed - last close price)"
                    alerts.append(msg)
                    s["last_sent"] = now.isoformat()
                    row["alerted"] = True
                s["zone"] = zone
                state[key] = s

    save_json(STATE_FILE, state)
    if alerts:  # one combined message so browser tabs don't clash
        send_alert("\n\n".join(alerts))
    bot["rows"], bot["last_scan"] = rows, now.strftime("%d %b %H:%M:%S")


def monitor_loop():
    while bot["running"]:
        if check_action_taken_signal():
            print("Signal received: 'action taken'. Stopping bot...")
            bot["running"] = False
            break
        try:
            scan()
        except Exception as e:
            print("scan error:", e)
        for _ in range(CHECK_EVERY_SECONDS):
            if not bot["running"]:
                break
            time.sleep(1)


# ====================== STREAMLIT UI ======================
TFS = {"30m": "30 Minutes", "1h": "1 Hour", "4h": "4 Hours", "1d": "1 Day"}
ZONES = {
    "above": "Above Cloud",
    "near_top": "Near Cloud Top",
    "inside": "Inside Cloud",
    "near_bottom": "Near Cloud Bottom",
    "below": "Below Cloud",
}
IDX = list(INDICES.keys())
ZONE_STYLE = {
    "above": "background-color:#DCFCE7;color:#16A34A;font-weight:600",
    "near_top": "background-color:#FEF3C7;color:#D97706;font-weight:600",
    "inside": "background-color:#F3F4F6;color:#6B7280;font-weight:600",
    "near_bottom": "background-color:#FFEDD5;color:#C2410C;font-weight:600",
    "below": "background-color:#FEE2E2;color:#DC2626;font-weight:600",
}

st.markdown(
    "<div style='background:#131B3B;padding:18px 22px;border-radius:10px;margin-bottom:18px'>"
    "<h2 style='color:#fff;margin:0;font-size:22px'>Ichimoku Cloud Alerts &rarr; WhatsApp</h2></div>",
    unsafe_allow_html=True,
)

saved = load_json(CONFIG_FILE, DEFAULT_CFG)
if "cfg_loaded" not in st.session_state:
    st.session_state.sel_indices = [i for i in saved.get("indices", []) if i in IDX]
    st.session_state.sel_tfs = [t for t in saved.get("timeframes", []) if t in TFS]
    st.session_state.sel_zones = [z for z in saved.get("zones", []) if z in ZONES]
    st.session_state.cfg_loaded = True

with st.container(border=True):
    st.multiselect("Index", IDX, key="sel_indices")
    st.multiselect("Timeframe", list(TFS.keys()), key="sel_tfs", format_func=lambda x: TFS[x])
    st.multiselect("Cloud (alert me when price is)", list(ZONES.keys()), key="sel_zones",
                   format_func=lambda x: ZONES[x])

    cfg = {
        "indices": st.session_state.sel_indices,
        "timeframes": st.session_state.sel_tfs,
        "zones": st.session_state.sel_zones,
    }
    ok = all(cfg.values())

    bot["test_mode"] = st.checkbox(
        "🧪 Test mode: send WhatsApp every 5 min (ignores market hours and zone filter)",
        value=bot.get("test_mode", False))

    bot["ignore_hours"] = st.checkbox(
        "Send auto alerts even when market is closed (Start alerts mode)",
        value=bot.get("ignore_hours", False))

    st.caption("Alert channel: " + (", ".join(active_channels()) or
               "pywhatkit (works only when this app runs on your own PC)"))

    c1, c2, _ = st.columns([1, 1, 6])

    if not bot["running"]:
        if c1.button("Start alerts", type="primary", disabled=not ok, use_container_width=True):
            save_json(CONFIG_FILE, cfg)
            open(SIGNAL_FILE, "w").close()  # clear old 'action taken'
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
              else "closed (alerts Mon-Fri 9:15-15:30 IST) - prices shown are the last close")
    st.info(f"{dot} **{state_txt}** · Market {market} · Last scan: {bot['last_scan'] or '-'}")

    rows = [r for r in bot["rows"]
            if r["index"] in cfg["indices"] and r["tf"] in cfg["timeframes"]]
    rows.sort(key=lambda r: (IDX.index(r["index"]), list(TFS).index(r["tf"])))

    if not rows:
        st.markdown(
            "<div style='border:1px dashed #D1D5DB;border-radius:10px;padding:32px;"
            "text-align:center;color:#6B7280'>Choose index, timeframe and cloud zone, "
            "then press <b>Start alerts</b> or <b>Check now</b>.</div>",
            unsafe_allow_html=True,
        )
        return

    data = []
    for r in rows:
        if r.get("error"):
            data.append({"Index": r["index"], "Timeframe": r["tf"].upper(), "Live price": "",
                         "Cloud bottom": "", "Cloud top": "", "Zone": "", "WhatsApp alert": r["error"]})
            continue
        alert = ("Sent" if r.get("alerted") else "Watching") if (r["zone"] in cfg["zones"] or r.get("alerted")) else "-"
        data.append({
            "Index": r["index"], "Timeframe": r["tf"].upper(),
            "Live price": r["price"], "Cloud bottom": r["bottom"], "Cloud top": r["top"],
            "Zone": ZONES[r["zone"]], "WhatsApp alert": alert,
        })

    df = pd.DataFrame(data)
    label_to_key = {v: k for k, v in ZONES.items()}
    styled = df.style.map(lambda v: ZONE_STYLE.get(label_to_key.get(v), ""), subset=["Zone"])
    num = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else v
    styled = styled.format(num, subset=["Live price", "Cloud bottom", "Cloud top"])
    st.dataframe(styled, use_container_width=True, hide_index=True)


live_panel()
