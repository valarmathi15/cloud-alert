"""
Ichimoku Cloud Alerts -> Phone notification (ntfy)  |  single-file Streamlit app

Run locally :  pip install -U streamlit yfinance pandas requests
               python -m streamlit run app.py
Secrets     :  NTFY_TOPIC = "your-topic-name"   (Streamlit Cloud: Manage app -> Settings -> Secrets)
Stop alerts :  press Stop in the dashboard, tap the "Action taken" button on a notification,
               or publish the text  action taken  to your ntfy topic.
Log         :  every notification (time, zone, price, action taken + time) is saved in
               notification_log.json and shown in the "Notification log" tab.
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
LOG_FILE = "notification_log.json"
LOG_MAX = 2000
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
    return {"running": False, "last_scan": None, "rows": [], "since": 0,
            "lock": threading.Lock()}


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


def fmt_ist(ts):
    return datetime.datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M:%S")


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


# ---------- notification log (saved to file, survives setting changes) ----------
def log_notifications(entries):
    if not entries:
        return
    with bot["lock"]:
        log = load_json(LOG_FILE, [])
        log.extend(entries)
        save_json(LOG_FILE, log[-LOG_MAX:])


def mark_action_taken(action_ts):
    """Mark the auto notifications of the current run as 'action taken' at action_ts."""
    with bot["lock"]:
        log = load_json(LOG_FILE, [])
        for e in log:
            if (e.get("kind") == "auto" and not e.get("action_unix")
                    and bot["since"] <= e.get("sent_unix", 0) <= action_ts + 2):
                e["action_unix"] = int(action_ts)
                e["action_at"] = fmt_ist(action_ts)
        save_json(LOG_FILE, log)


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


def zone_status(zone, selected_zones, near_pct=None):
    """Only the zones the user selected, each marked in (✅) or not in (❌)."""
    parts = []
    for z in ZONES:  # fixed order
        if z in selected_zones:
            label = ZONES[z] + (f" ({near_pct}%)" if near_pct and z in ("near_top", "near_bottom") else "")
            parts.append(f"{'✅' if z == zone else '❌'} {label}")
    return " | ".join(parts)


def build_message(name, tf, price, top, bottom, zone, selected_zones, near_pct):
    """Tells the current zone and, for every selected zone, whether the price is in it or not."""
    in_any = zone in selected_zones
    lines = [f"{'✅' if in_any else '❌'} {name} ({tf.upper()})",
             f"Live price: {price:,.2f}",
             f"Cloud: {bottom:,.2f} - {top:,.2f}"]
    for z in ZONES:  # keep a fixed order
        if z not in selected_zones:
            continue
        label = ZONES[z] + (f" ({near_pct}%)" if z in ("near_top", "near_bottom") else "")
        lines.append(f"{'✅' if z == zone else '❌'} {label} - {'IRUKKU' if z == zone else 'ILLA'}")
    return "\n".join(lines)


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
    """Unix time of the latest 'action taken' published to the topic after `since`, else 0."""
    found = 0
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
                    found = max(found, int(m["time"]))
        except Exception as e:
            print("ntfy poll error:", e)
    return found


def stop_bot(reason, action_ts=None):
    bot["running"] = False
    print("Alerts stopped:", reason)
    if action_ts:
        mark_action_taken(action_ts)
    send_alert("✅ Alerts stopped (" + reason + "). Dashboard-la irundhu thirumba Start pannalaam.",
               with_button=False)


# ---------- scan / loop ----------
def scan(force=False):
    """One pass over every selected index x timeframe.
    Every scan sends a notification saying whether the price is IN or NOT IN each selected zone."""
    cfg = load_json(CONFIG_FILE, DEFAULT_CFG)
    near_pct = int(cfg.get("near_pct", 1))
    near = near_pct / 100
    market_open = in_market_hours()
    now = now_ist()
    rows, alerts, log_entries = [], [], []
    sent_ts = int(time.time())

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

            matched = zone in cfg["zones"]
            msg = build_message(name, tf, price, top, bottom, zone, cfg["zones"], near_pct)
            if force:
                msg = "🔎 MANUAL CHECK\n" + msg
            if not market_open:
                msg += "\n(market closed - last close price)"
            alerts.append(msg)
            status = zone_status(zone, cfg["zones"], near_pct)
            row["alerted"], row["matched"], row["status"] = True, matched, status
            log_entries.append({
                "sent_unix": sent_ts, "sent_at": fmt_ist(sent_ts), "index": name,
                "tf": tf, "zone": zone, "price": price, "matched": matched, "status": status,
                "kind": "manual" if force else "auto",
                "action_unix": None, "action_at": None})

    if alerts:  # one combined notification per scan
        send_alert("\n\n".join(alerts))
        log_notifications(log_entries)
    bot["rows"], bot["last_scan"] = rows, now.strftime("%d %b %H:%M:%S")


def monitor_loop():
    while bot["running"]:
        if check_action_taken_signal():
            stop_bot("signal file", time.time())
            break
        if NTFY_TOPICS:
            ts = ntfy_stop_requested(bot["since"])
            if ts:
                stop_bot("action taken", ts)
                break
        try:
            scan()
        except Exception as e:
            print("scan error:", e)
        waited = 0
        while bot["running"] and waited < CHECK_EVERY_SECONDS:
            time.sleep(1)
            waited += 1
            if waited % STOP_POLL_SECONDS == 0 and NTFY_TOPICS:
                ts = ntfy_stop_requested(bot["since"])
                if ts:
                    stop_bot("action taken", ts)
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

# ---- session defaults come from the saved config; checkbox states live in session_state ----
saved = load_json(CONFIG_FILE, DEFAULT_CFG)
saved_segments = saved.get("segments") or sorted(
    {SEGMENT_OF[i] for i in saved.get("indices", []) if i in SEGMENT_OF}) or ["NSE"]


def keep(key, default):
    st.session_state[key] = st.session_state.get(key, default)


try:
    _np = min(10, max(1, int(saved.get("near_pct", 1))))
except Exception:
    _np = 1
for s_ in SEGMENTS:
    keep(f"seg_{s_}", s_ in saved_segments)
for n_ in INDICES:
    keep(f"idx_{n_}", n_ in saved.get("indices", []))
for t_ in TFS:
    keep(f"tf_{t_}", t_ in saved.get("timeframes", []))
for z_ in ZONES:
    keep(f"zn_{z_}", z_ in saved.get("zones", []))
keep("near_pct", _np)


def _toggle_all(prefix, values):
    v = st.session_state[f"{prefix}__all"]
    for x in values:
        st.session_state[f"{prefix}_{x}"] = v


def check_dropdown(col, title, items, prefix):
    """A dropdown (popover) with a 'Select all' box and one checkbox per option."""
    values = [v for v, _ in items]
    chosen = [lbl for v, lbl in items if st.session_state[f"{prefix}_{v}"]]
    if not chosen:
        text = "Select..."
    elif len(chosen) == len(items):
        text = "All"
    elif len(chosen) <= 2:
        text = ", ".join(chosen)
    else:
        text = f"{len(chosen)} selected"
    col.caption(title)
    with col.popover(text, width="stretch"):
        st.session_state[f"{prefix}__all"] = len(chosen) == len(items)
        st.checkbox("Select all", key=f"{prefix}__all", on_change=_toggle_all, args=(prefix, values))
        st.divider()
        for v, lbl in items:
            st.checkbox(lbl, key=f"{prefix}_{v}")
    return [v for v in values if st.session_state[f"{prefix}_{v}"]]


tab_dash, tab_log = st.tabs(["Dashboard", "Notification log"])

with tab_dash:
    with st.container(border=True):
        c_seg, c_idx, c_tf, c_zn, c_np = st.columns([1, 1.6, 1.3, 1.6, 0.9])

        sel_segments = check_dropdown(c_seg, "Segment", [(s, s) for s in SEGMENTS], "seg")
        idx_items = [(n, n) for n in INDICES if SEGMENT_OF[n] in sel_segments]
        sel_indices = check_dropdown(c_idx, "Index", idx_items, "idx") if idx_items else []
        if not idx_items:
            c_idx.caption("Index")
            c_idx.popover("Select segment first", width="stretch", disabled=True)
        sel_tfs = check_dropdown(c_tf, "Timeframe", list(TFS.items()), "tf")
        sel_zones = check_dropdown(c_zn, "Cloud (alert only when price is)", list(ZONES.items()), "zn")

        c_np.caption("Target %")
        with c_np.popover(f"{st.session_state['near_pct']}%", width="stretch"):
            st.caption('Near Cloud Top / Bottom = price within this % of the cloud line')
            st.radio("Target %", list(range(1, 11)), key="near_pct",
                     format_func=lambda x: f"{x}%", label_visibility="collapsed")

        cfg = {
            "segments": sel_segments,
            "indices": sel_indices,
            "timeframes": sel_tfs,
            "zones": sel_zones,
            "near_pct": int(st.session_state["near_pct"]),
        }
        ok = bool(cfg["indices"] and cfg["timeframes"] and cfg["zones"])
        if cfg != saved:
            save_json(CONFIG_FILE, cfg)  # every change is saved

        if active_channels():
            st.caption("Alert channel: " + ", ".join(active_channels()) +
                       "  ·  Stop: Stop button / notification-la \"Action taken\" button / ntfy-la \"action taken\" nu send pannunga")
        else:
            st.warning("NTFY_TOPIC is not set in Secrets - notifications will not be sent.")

        c1, c2, _ = st.columns([1, 1, 6])

        if not bot["running"]:
            if c1.button("Start alerts", type="primary", disabled=not ok, width="stretch"):
                save_json(CONFIG_FILE, cfg)
                open(SIGNAL_FILE, "w").close()          # clear old local 'action taken'
                bot["since"] = int(time.time())          # ignore old 'action taken' messages on ntfy
                bot["running"] = True
                threading.Thread(target=monitor_loop, daemon=True).start()
                st.rerun()
        else:
            if c1.button("Stop alerts", type="primary", width="stretch"):
                bot["running"] = False
                st.rerun()

        if c2.button("Check now", disabled=not ok, width="stretch"):
            save_json(CONFIG_FILE, cfg)
            with st.spinner("Checking..."):
                scan(force=True)
            st.rerun()

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
                             "Cloud bottom": "", "Cloud top": "", "Selected zone": "", "Notification": r["error"]})
                continue
            if r.get("alerted"):
                alert = "Sent - in selected zone" if r.get("matched") else "Sent - not in selected zone"
            else:
                alert = "Watching"
            data.append({
                "Index": r["index"], "Timeframe": r["tf"].upper(),
                "Live price": r["price"], "Cloud bottom": r["bottom"], "Cloud top": r["top"],
                "Selected zone": r.get("status", ""), "Notification": alert,
            })

        df = pd.DataFrame(data)
        num = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else v
        paint = lambda v: (ZONE_STYLE["above"] if "✅" in str(v)
                           else ZONE_STYLE["below"] if "❌" in str(v) else "")
        styled = (df.style.map(paint, subset=["Selected zone"])
                          .format(num, subset=["Live price", "Cloud bottom", "Cloud top"]))
        st.dataframe(styled, width="stretch", hide_index=True)

    live_panel()

with tab_log:
    @st.fragment(run_every=10)
    def log_panel():
        log = load_json(LOG_FILE, [])
        if not log:
            st.info("Innum notification poga-la. Pogum bodhu inga time, zone, price, action taken ellam save aagi varum.")
            return
        data = []
        for e in reversed(log):
            if e.get("kind") == "manual":
                action, at = "- (manual check)", "-"
            elif e.get("action_at"):
                action, at = "Yes", e["action_at"]
            elif bot["running"] and e.get("sent_unix", 0) >= bot["since"]:
                action, at = "Pending", "-"
            else:
                action, at = "No", "-"
            data.append({
                "Sent at (IST)": e.get("sent_at", ""),
                "Index": e.get("index", ""),
                "Timeframe": str(e.get("tf", "")).upper(),
                "Selected zone": e.get("status") or ZONES.get(e.get("zone"), e.get("zone")),
                "In selected zone": "Yes" if e.get("matched", True) else "No",
                "Price": e.get("price"),
                "Type": "Manual check" if e.get("kind") == "manual" else "Auto",
                "Action taken": action,
                "Action time (IST)": at,
            })
        df = pd.DataFrame(data)
        st.caption(f"{len(df)} notifications saved (newest first)")
        st.dataframe(df.style.format({"Price": lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else v}),
                     width="stretch", hide_index=True)
        st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"),
                           file_name="notification_log.csv", mime="text/csv", key="dl_log")

    log_panel()
