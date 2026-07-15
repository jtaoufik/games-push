#!/usr/bin/env python3
"""Games retention-push service (Coolify/Hetzner).

Moved off the Mac launchd cron. Sends a rotating competitive/leaderboard push to
every opted-in player across Maze Glass, Bloom, Trivio, on a schedule, and exposes
a small practical web UI to see reach, preview, and send on demand.

AUTH: reuses the Firebase CLI OAuth (Leon, cloud-platform scope -> Firestore + FCM
v1) via a refresh token set in FIREBASE_REFRESH_TOKEN. The stored access token is
always short-lived so we refresh on every use.
"""
from __future__ import annotations
import json, os, time, threading, datetime, pathlib
from typing import Any

import requests
from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse, JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# firebase-tools' public OAuth client (embedded in the open-source CLI).
CLIENT_ID = "563584335869-fgrhgmd47bqnekij5i8b5pr03ho849e6.apps.googleusercontent.com"
CLIENT_SECRET = "j9iVZfS8kkCEFUPaAeJV0sAi"
REFRESH_TOKEN = os.environ.get("FIREBASE_REFRESH_TOKEN", "")

# Firebase project id per game.
GAMES = {
    "Maze Glass": "maze-glass",
    "Bloom": "bloom-game-b3b33",
    "Trivio": "trivio-quiz-app",
}
TZ = os.environ.get("TZ", "Europe/Paris")
HERE = pathlib.Path(__file__).parent
CAMPAIGNS: list[dict] = json.load(open(HERE / "campaigns.json"))
FALLBACK = "en"
LOG_PATH = pathlib.Path("/data/sendlog.json")   # persisted across restarts if /data is a volume
_LOG_LOCK = threading.Lock()


def short_locale(loc: str) -> str:
    base = (loc or "en").split("-")[0].lower()
    return base if any(base in c for c in CAMPAIGNS) or base in CAMPAIGNS[0] else FALLBACK


def creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    c = Credentials(token=None, refresh_token=REFRESH_TOKEN,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"])
    c.refresh(Request())
    return c


def campaign_for_now() -> tuple[int, dict]:
    idx = (int(time.time()) // 86400) % len(CAMPAIGNS)
    return idx, CAMPAIGNS[idx]


def read_tokens(project: str, c) -> dict[str, list[str]]:
    """users/*.fcmToken grouped by pushLocale short code."""
    from google.cloud import firestore
    db = firestore.Client(project=project, credentials=c)
    by_locale: dict[str, list[str]] = {}
    for doc in db.collection("users").stream():
        d = doc.to_dict() or {}
        tok = d.get("fcmToken")
        if not tok:
            continue
        by_locale.setdefault(short_locale(d.get("pushLocale") or "en"), []).append(tok)
    return by_locale


def do_send(payloads: dict, dry: bool) -> dict:
    c = creds()
    sess = None
    if not dry:
        from google.auth.transport.requests import AuthorizedSession
        sess = AuthorizedSession(c)
    result = {"dry": dry, "at": datetime.datetime.now().isoformat(timespec="seconds"), "games": {}}
    for name, project in GAMES.items():
        try:
            by_locale = read_tokens(project, c)
        except Exception as e:
            result["games"][name] = {"error": str(e)[:200]}
            continue
        total = sum(len(v) for v in by_locale.values())
        sent = fail = 0
        if not dry:
            endpoint = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
            for lang, toks in by_locale.items():
                p = payloads.get(lang) or payloads.get(FALLBACK)
                for t in toks:
                    msg = {"message": {"token": t,
                                       "notification": {"title": p["title"], "body": p["body"]},
                                       "apns": {"payload": {"aps": {"sound": "default"}}}}}
                    r = sess.post(endpoint, json=msg, timeout=30)
                    if r.status_code == 200: sent += 1
                    else: fail += 1
                    time.sleep(0.05)
        result["games"][name] = {"tokens": total, "sent": sent, "failed": fail,
                                 "byLocale": {k: len(v) for k, v in by_locale.items()}}
    if not dry:
        _append_log(result)
    return result


def _append_log(entry: dict):
    with _LOG_LOCK:
        try:
            log = json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else []
        except Exception:
            log = []
        log.insert(0, entry)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text(json.dumps(log[:50]))


def recent_log() -> list:
    try:
        return json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else []
    except Exception:
        return []


def scheduled_job():
    _, camp = campaign_for_now()
    do_send(camp, dry=False)


scheduler = BackgroundScheduler(timezone=TZ)
# Tue / Thu / Sun at 19:00 local.
scheduler.add_job(scheduled_job, CronTrigger(day_of_week="tue,thu,sun", hour=19, minute=0), id="retention")
scheduler.start()

app = FastAPI(title="Games Push")


@app.get("/api/status")
def status():
    idx, camp = campaign_for_now()
    nxt = scheduler.get_job("retention").next_run_time
    return JSONResponse({
        "games": list(GAMES.keys()),
        "campaignIndex": idx, "campaignCount": len(CAMPAIGNS),
        "campaign": camp, "nextRun": nxt.isoformat() if nxt else None,
        "tz": TZ, "log": recent_log()[:10],
        "authConfigured": bool(REFRESH_TOKEN),
    })


@app.post("/api/send")
def send(body: dict = Body(default={})):
    dry = bool(body.get("dry", True))
    idx = body.get("campaignIndex")
    camp = CAMPAIGNS[idx] if isinstance(idx, int) and 0 <= idx < len(CAMPAIGNS) else campaign_for_now()[1]
    return JSONResponse(do_send(camp, dry=dry))


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML


HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Games Push</title><style>
:root{color-scheme:dark}body{margin:0;font:15px/1.5 -apple-system,system-ui,sans-serif;background:#0f1115;color:#e6e8ec}
.wrap{max-width:760px;margin:0 auto;padding:24px 16px 60px}h1{font-size:20px;margin:0 0 4px}.sub{color:#8b93a1;font-size:13px;margin-bottom:20px}
.card{background:#171a21;border:1px solid #232833;border-radius:14px;padding:16px;margin-bottom:14px}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px}.muted{color:#8b93a1;font-size:13px}
.big{font-size:26px;font-weight:700}.games{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:6px}
.g{background:#12151b;border:1px solid #232833;border-radius:10px;padding:10px;text-align:center}.g b{display:block;font-size:22px}
.btn{border:0;border-radius:10px;padding:10px 16px;font-weight:600;cursor:pointer;font-size:14px}
.btn.primary{background:#3b82f6;color:#fff}.btn.ghost{background:#232833;color:#e6e8ec}.btn:disabled{opacity:.5;cursor:default}
.msg{background:#12151b;border-radius:10px;padding:10px 12px;margin-top:8px;font-size:13px}.msg b{color:#cdd3dc}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:#9aa3b2;margin:8px 0 0}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;background:#232833;color:#8b93a1}
h2{font-size:14px;color:#8b93a1;margin:22px 0 8px;text-transform:uppercase;letter-spacing:.04em}
.logrow{border-bottom:1px solid #232833;padding:8px 0;font-size:13px}.ok{color:#4ade80}
</style></head><body><div class=wrap>
<h1>Games Push</h1><div class=sub>Retention notifications for Maze Glass, Bloom, Trivio</div>
<div class=card><div class=row><div><div class=muted>Next scheduled send</div><div class=big id=next>…</div></div>
<span class=pill id=tz></span></div>
<div class=games id=games></div></div>
<div class=card><div class=row><div><div class=muted>Current campaign (rotates every day)</div>
<div id=campmeta class=muted></div></div></div>
<div class=msg><b id=ctitle></b><br><span id=cbody></span></div>
<div style="margin-top:14px;display:flex;gap:10px">
<button class="btn ghost" id=dry>Preview reach (dry run)</button>
<button class="btn primary" id=send>Send now</button></div>
<pre id=out></pre></div>
<h2>Recent sends</h2><div id=log></div>
</div><script>
const $=s=>document.querySelector(s);
async function load(){const r=await fetch('api/status');const d=await r.json();
 $('#next').textContent=d.nextRun?new Date(d.nextRun).toLocaleString():'—';
 $('#tz').textContent=d.tz+(d.authConfigured?'':' · NO AUTH');
 $('#games').innerHTML=d.games.map(g=>`<div class=g><b>${g[0]}</b><span class=muted>${g}</span></div>`).join('');
 $('#campmeta').textContent='#'+(d.campaignIndex+1)+' of '+d.campaignCount;
 const c=d.campaign.en||Object.values(d.campaign)[0];$('#ctitle').textContent=c.title;$('#cbody').textContent=c.body;
 $('#log').innerHTML=(d.log||[]).map(e=>{const g=Object.entries(e.games).map(([n,v])=>`${n}: ${v.sent??0}✓`).join('  ');
   return `<div class=logrow>${new Date(e.at).toLocaleString()} — ${g}</div>`}).join('')||'<div class=muted>No sends yet.</div>';
}
async function run(dry){$('#out').textContent='Working…';$('#dry').disabled=$('#send').disabled=true;
 try{const r=await fetch('api/send',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({dry})});
 const d=await r.json();$('#out').textContent=JSON.stringify(d.games,null,2);}catch(e){$('#out').textContent=e}
 $('#dry').disabled=$('#send').disabled=false;load()}
$('#dry').onclick=()=>run(true);
$('#send').onclick=()=>{if(confirm('Send this campaign to all opted-in players now?'))run(false)};
load();setInterval(load,30000);
</script></body></html>"""
