"""
Ichimoku Cloud Alerts -> WhatsApp  (Streamlit dashboard)
Uses the same logic as server.py (keep both files in the same folder).

Run:  pip install streamlit flask yfinance pandas pywhatkit
      streamlit run app.py
"""
import threading
import pandas as pd
import streamlit as st

# Reuse everything from your existing backend.
# Imported modules stay cached across Streamlit reruns, so `bot` keeps its state.
import server as srv

st.set_page_config(page_title="Ichimoku Cloud Alerts", page_icon="☁️", layout="wide")

IDX = list(srv.INDICES.keys())
TFS = {"30m": "30 Minutes", "1h": "1 Hour", "4h": "4 Hours", "1d": "1 Day"}
ZONES = {
    "above": "Above Cloud",
    "near_top": "Near Cloud Top",
    "inside": "Inside Cloud",
    "near_bottom": "Near Cloud Bottom",
    "below": "Below Cloud",
}
ZONE_STYLE = {
    "above": "background-color:#DCFCE7;color:#16A34A;font-weight:600",
    "near_top": "background-color:#FEF3C7;color:#D97706;font-weight:600",
    "inside": "background-color:#F3F4F6;color:#6B7280;font-weight:600",
    "near_bottom": "background-color:#FFEDD5;color:#C2410C;font-weight:600",
    "below": "background-color:#FEE2E2;color:#DC2626;font-weight:600",
}

# ---------------- Header ----------------
st.markdown(
    "<div style='background:#131B3B;padding:18px 22px;border-radius:10px;margin-bottom:18px'>"
    "<h2 style='color:#fff;margin:0;font-size:22px'>Ichimoku Cloud Alerts &rarr; WhatsApp</h2></div>",
    unsafe_allow_html=True,
)

# ---------------- Config (loaded once from saved file) ----------------
saved = srv.load_json(srv.CONFIG_FILE, srv.DEFAULT_CFG)
if "cfg_loaded" not in st.session_state:
    st.session_state.sel_indices = [i for i in saved.get("indices", []) if i in IDX]
    st.session_state.sel_tfs = [t for t in saved.get("timeframes", []) if t in TFS]
    st.session_state.sel_zones = [z for z in saved.get("zones", []) if z in ZONES]
    st.session_state.cfg_loaded = True

with st.container(border=True):
    st.multiselect("Index", IDX, key="sel_indices")
    st.multiselect("Timeframe", list(TFS.keys()), key="sel_tfs",
                   format_func=lambda x: TFS[x])
    st.multiselect("Cloud (alert me when price is)", list(ZONES.keys()), key="sel_zones",
                   format_func=lambda x: ZONES[x])

    cfg = {
        "indices": st.session_state.sel_indices,
        "timeframes": st.session_state.sel_tfs,
        "zones": st.session_state.sel_zones,
    }
    ok = all(cfg.values())

    c1, c2, c3, _ = st.columns([1, 1, 1, 5])
    running = srv.bot["running"]

    if not running:
        if c1.button("Start alerts", type="primary", disabled=not ok, use_container_width=True):
            srv.save_json(srv.CONFIG_FILE, cfg)
            open(srv.SIGNAL_FILE, "w").close()  # clear old 'action taken'
            srv.bot["running"] = True
            threading.Thread(target=srv.monitor_loop, daemon=True).start()
            st.rerun()
    else:
        if c1.button("Stop alerts", type="primary", use_container_width=True):
            srv.bot["running"] = False
            st.rerun()

    if c2.button("Check now", disabled=not ok, use_container_width=True):
        srv.save_json(srv.CONFIG_FILE, cfg)
        with st.spinner("Checking..."):
            srv.scan()
        st.rerun()

# If alerts are running, keep the saved config in sync with the selection
if ok and srv.bot["running"]:
    srv.save_json(srv.CONFIG_FILE, cfg)


# ---------------- Live panel (auto-refreshes every 10 s) ----------------
@st.fragment(run_every=10)
def live_panel():
    bot = srv.bot
    dot = "🟢" if bot["running"] else "⚪"
    state_txt = "Alerts running" if bot["running"] else "Alerts stopped"
    market = "open" if srv.in_market_hours() else "closed (alerts only 9:15-15:30)"
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
        if r["zone"] in cfg["zones"]:
            alert = "Sent" if r.get("alerted") else "Watching"
        else:
            alert = "-"
        data.append({
            "Index": r["index"], "Timeframe": r["tf"].upper(),
            "Live price": r["price"], "Cloud bottom": r["bottom"], "Cloud top": r["top"],
            "Zone": ZONES[r["zone"]], "WhatsApp alert": alert,
        })

    df = pd.DataFrame(data)
    zone_by_label = {v: k for k, v in ZONES.items()}
    styled = df.style.map(lambda v: ZONE_STYLE.get(zone_by_label.get(v), ""), subset=["Zone"])
    st.dataframe(styled, use_container_width=True, hide_index=True)


live_panel()
