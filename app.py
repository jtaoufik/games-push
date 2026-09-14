#!/usr/bin/env python3
"""Games retention-push service (Coolify/Hetzner).

Moved off the Mac launchd cron. Sends a rotating, PER-APP retention push to every
opted-in player across Maze Glass, Bloom, Trivio and Forge, on a schedule, and
exposes a small web UI that shows each app's own message and can send on demand.

Every message carries a `data` block with a routing `kind` so a tap lands on the
screen the copy is selling. See mobile/push-routing.md for the contract.

AUTH: reuses the Firebase CLI OAuth (Leon, cloud-platform scope -> Firestore + FCM
v1) via a refresh token set in FIREBASE_REFRESH_TOKEN. The stored access token is
always short-lived so we refresh on every use.
"""
from __future__ import annotations
import json, os, time, threading, datetime, pathlib

from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse, JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# firebase-tools' public OAuth client (embedded in the open-source CLI).
CLIENT_ID = "563584335869-fgrhgmd47bqnekij5i8b5pr03ho849e6.apps.googleusercontent.com"
CLIENT_SECRET = "j9iVZfS8kkCEFUPaAeJV0sAi"
REFRESH_TOKEN = os.environ.get("FIREBASE_REFRESH_TOKEN", "")

# Firebase project id per app.
GAMES = {
    "Maze Glass": "maze-glass",
    "Bloom": "bloom-game-b3b33",
    "Trivio": "trivio-quiz-app",
    "Forge": "forge-screen-time",
}

# ---- Routing (contract: mobile/push-routing.md) --------------------------
# Every message carries a `data` dict whose `kind` tells the client where to
# land. The key is `kind`, never `type`: Introva already ships that name in its
# backend and in its Android client, so it is fixed fleet wide.
#
# The kind is declared PER CAMPAIGN MESSAGE, not per app. The five rotating
# messages do not all sell the same thing: "solve a maze right now" belongs on
# the game, "defend your rank" belongs on the leaderboard. Landing competitive
# copy on a play button is the same defect as landing it on a menu.
#
# `kind` is a closed set per app. Trivio is play-only on purpose: it has no
# leaderboard screen and stores no high score. Forge has no leaderboard either,
# its retention copy always points at the workout screen where time is earned.
KINDS = {
    "Maze Glass": {"play", "leaderboard"},
    "Bloom": {"play", "leaderboard"},
    "Trivio": {"play"},
    "Forge": {"retention"},
}
DEFAULT_KIND = "play"
APP_DEFAULT_KIND = {"Forge": "retention"}
TZ = os.environ.get("TZ", "Europe/Paris")
HERE = pathlib.Path(__file__).parent
# Per-app campaign banks: { "Maze Glass": [ {en:{title,body}, fr:{...}, ...}, ... ], ... }
CAMPAIGNS: dict[str, list[dict]] = json.load(open(HERE / "campaigns.json"))
LOCALES = ["en", "fr", "es", "ja", "pt"]
FALLBACK = "en"
LOG_PATH = pathlib.Path("/data/sendlog.json")   # persisted if /data is a volume
_LOG_LOCK = threading.Lock()


def short_locale(loc: str) -> str:
    base = (loc or "en").split("-")[0].lower()
    return base if base in LOCALES else FALLBACK


def num_campaigns() -> int:
    return min(len(v) for v in CAMPAIGNS.values())


def current_index() -> int:
    return (int(time.time()) // 86400) % num_campaigns()


def message_for(app: str, idx: int) -> dict:
    lst = CAMPAIGNS[app]
    return lst[idx % len(lst)]


def kind_for(app: str, entry: dict) -> str:
    """Routing kind of one campaign entry, clamped to that app's closed set.

    A missing or unknown kind falls back to the app's default. Never raises:
    a broadcast must not die because someone typo'd a campaign entry.
    """
    fallback = APP_DEFAULT_KIND.get(app, DEFAULT_KIND)
    k = entry.get("kind")
    return k if isinstance(k, str) and k in KINDS.get(app, set()) else fallback


def build_data(app: str, entry: dict) -> dict[str, str]:
    """The data block. FCM v1 rejects a message whose data holds any non-string
    value (400 INVALID_ARGUMENT), so every value is stringified here."""
    data = {"kind": kind_for(app, entry)}
    ref = entry.get("refId")
    if ref is not None:
        data["refId"] = ref
    return {str(k): str(v) for k, v in data.items()}


def build_message(token: str, app: str, entry: dict, copy: dict) -> dict:
    """One complete FCM v1 message.

    `data` rides ALONGSIDE `notification`, it does not replace it: dropping the
    notification block would kill the banner while the app is killed on some
    OEMs, and the data keys survive a tap on a system drawn notification because
    Firebase copies them into the launcher intent extras.
    """
    data = build_data(app, entry)
    bad = sorted(k for k, v in data.items() if not isinstance(v, str))
    if bad:
        raise ValueError(f"FCM v1 data values must be strings, got: {bad}")
    return {"message": {
        "token": token,
        "notification": {"title": copy["title"], "body": copy["body"]},
        "apns": {"payload": {"aps": {"sound": "default"}}},
        "data": data,
    }}


def preview_message(app: str, idx: int) -> dict:
    """The exact message this app would send at `idx`, with the token redacted.
    Lets the payload be proven without sending anything to a real device."""
    entry = message_for(app, idx)
    return build_message("<TOKEN>", app, entry, entry[FALLBACK])


def creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    c = Credentials(token=None, refresh_token=REFRESH_TOKEN,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"])
    c.refresh(Request())
    return c


def read_tokens(project: str, c) -> tuple[dict[str, list[str]], dict[str, int]]:
    """users/*.fcmToken grouped by pushLocale short code, plus a per-platform
    count. The platform split is what tells us an app is a live campaign target
    that cannot receive: Maze Glass Android reads 0 because firebase-messaging
    was removed from its build on 05/09/2026."""
    from google.cloud import firestore
    db = firestore.Client(project=project, credentials=c)
    by_locale: dict[str, list[str]] = {}
    by_platform: dict[str, int] = {}
    for doc in db.collection("users").stream():
        d = doc.to_dict() or {}
        tok = d.get("fcmToken")
        if not tok:
            continue
        by_locale.setdefault(short_locale(d.get("pushLocale") or "en"), []).append(tok)
        plat = d.get("pushPlatform") or "unknown"
        by_platform[str(plat)] = by_platform.get(str(plat), 0) + 1
    return by_locale, by_platform


def do_send(idx: int, dry: bool, only_app: str | None = None) -> dict:
    c = creds()
    sess = None
    if not dry:
        from google.auth.transport.requests import AuthorizedSession
        sess = AuthorizedSession(c)
    result = {"dry": dry, "at": datetime.datetime.now().isoformat(timespec="seconds"),
              "campaignIndex": idx, "games": {}}
    for name, project in GAMES.items():
        if only_app and name != only_app:
            continue
        entry = message_for(name, idx)
        try:
            by_locale, by_platform = read_tokens(project, c)
        except Exception as e:
            result["games"][name] = {"error": str(e)[:200]}
            continue
        total = sum(len(v) for v in by_locale.values())
        sent = fail = 0
        if not dry:
            endpoint = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
            for lang, toks in by_locale.items():
                p = entry.get(lang) or entry[FALLBACK]
                for t in toks:
                    msg = build_message(t, name, entry, p)
                    r = sess.post(endpoint, json=msg, timeout=30)
                    if r.status_code == 200: sent += 1
                    else: fail += 1
                    time.sleep(0.05)
        result["games"][name] = {"tokens": total, "sent": sent, "failed": fail,
                                 "title": entry[FALLBACK]["title"],
                                 "kind": kind_for(name, entry),
                                 "byLocale": {k: len(v) for k, v in by_locale.items()},
                                 "byPlatform": by_platform}
        if dry:
            # the exact bytes a real send would post, token redacted
            result["games"][name]["payload"] = build_message(
                "<TOKEN>", name, entry, entry[FALLBACK])
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
    do_send(current_index(), dry=False)


scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(scheduled_job, CronTrigger(day_of_week="tue,thu,sun", hour=19, minute=0), id="retention")
scheduler.start()

app = FastAPI(title="Games Push")


@app.get("/api/status")
def status():
    idx = current_index()
    nxt = scheduler.get_job("retention").next_run_time
    return JSONResponse({
        "games": list(GAMES.keys()),
        "campaignIndex": idx, "campaignCount": num_campaigns(),
        "messages": {name: message_for(name, idx) for name in GAMES},
        "kinds": {name: kind_for(name, message_for(name, idx)) for name in GAMES},
        "payloads": {name: preview_message(name, idx) for name in GAMES},
        "nextRun": nxt.isoformat() if nxt else None,
        "tz": TZ, "log": recent_log()[:10], "authConfigured": bool(REFRESH_TOKEN),
    })


@app.post("/api/send")
def send(body: dict = Body(default={})):
    dry = bool(body.get("dry", True))
    i = body.get("campaignIndex")
    idx = i if isinstance(i, int) and 0 <= i < num_campaigns() else current_index()
    only = body.get("app")
    return JSONResponse(do_send(idx, dry=dry, only_app=only if only in GAMES else None))


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML


HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Games Push</title><style>
:root{color-scheme:dark}body{margin:0;font:15px/1.5 -apple-system,system-ui,sans-serif;background:#0f1115;color:#e6e8ec}
.wrap{max-width:780px;margin:0 auto;padding:24px 16px 60px}h1{font-size:20px;margin:0 0 4px}.sub{color:#8b93a1;font-size:13px;margin-bottom:20px}
.card{background:#171a21;border:1px solid #232833;border-radius:14px;padding:16px;margin-bottom:14px}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.muted{color:#8b93a1;font-size:13px}
.big{font-size:22px;font-weight:700}
.btn{border:0;border-radius:10px;padding:9px 15px;font-weight:600;cursor:pointer;font-size:13px}
.btn.primary{background:#3b82f6;color:#fff}.btn.ghost{background:#232833;color:#e6e8ec}.btn.sm{padding:6px 12px;font-size:12px}.btn:disabled{opacity:.5;cursor:default}
.app{border-top:1px solid #232833;padding:14px 0}.app:first-of-type{border-top:0}
.appname{font-weight:700;display:flex;align-items:center;gap:8px}.count{font-size:12px;color:#8b93a1;background:#12151b;border:1px solid #232833;padding:2px 8px;border-radius:99px}
.mtitle{color:#cdd3dc;font-weight:600;margin-top:6px}.mbody{color:#aab2c0;font-size:13px}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;background:#232833;color:#8b93a1}
.kind{font-size:11px;padding:2px 8px;border-radius:99px;background:#16324d;color:#7fb3ec;font-weight:600}
.plat{font-size:11px;color:#6d7686}
h2{font-size:13px;color:#8b93a1;margin:22px 0 8px;text-transform:uppercase;letter-spacing:.04em}
.logrow{border-bottom:1px solid #232833;padding:8px 0;font-size:13px;color:#aab2c0}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:#9aa3b2;margin:8px 0 0}
.actions{display:flex;gap:8px;align-items:center;margin-top:8px}
</style></head><body><div class=wrap>
<h1>Games Push</h1><div class=sub>Retention notifications. Each app gets its own message and its own routing kind.</div>
<div class=card><div class=row><div><div class=muted>Next scheduled send</div><div class=big id=next>…</div></div>
<div class=row><span class=pill id=camp></span><span class=pill id=tz></span>
<button class="btn ghost sm" id=dryall>Dry run all</button><button class="btn primary sm" id=sendall>Send all now</button></div></div>
<pre id=allout></pre></div>

<div class=card id=apps></div>

<h2>Recent sends</h2><div id=log></div>
</div><script>
const $=s=>document.querySelector(s);let ST=null;
async function load(){const r=await fetch('api/status');const d=await r.json();ST=d;
 $('#next').textContent=d.nextRun?new Date(d.nextRun).toLocaleString():'—';
 $('#camp').textContent='Campaign '+(d.campaignIndex+1)+' / '+d.campaignCount;
 $('#tz').textContent=d.tz+(d.authConfigured?'':' · NO AUTH');
 $('#apps').innerHTML=d.games.map(g=>{const m=(d.messages[g].en)||Object.values(d.messages[g])[0];
   return `<div class=app><div class=row><div class=appname>${g} <span class=kind>data.kind = ${d.kinds?d.kinds[g]:'?'}</span></div>
   <span class=count id="c_${g.replace(/\\s/g,'')}">…</span></div>
   <div class=mtitle>${m.title}</div><div class=mbody>${m.body}</div>
   <div class=plat id="p_${g.replace(/\\s/g,'')}"></div>
   <div class=actions><button class="btn ghost sm" onclick="one('${g}',true)">Preview reach</button>
   <button class="btn primary sm" onclick="one('${g}',false)">Send to ${g}</button>
   <span class=muted id="o_${g.replace(/\\s/g,'')}"></span></div></div>`}).join('');
 $('#log').innerHTML=(d.log||[]).map(e=>{const g=Object.entries(e.games).map(([n,v])=>`${n} ${v.sent??0}✓`).join(' · ');
   return `<div class=logrow>${new Date(e.at).toLocaleString()} — ${g}</div>`}).join('')||'<div class=muted>No sends yet.</div>';
 // fill live counts via a dry run in the background (once)
 if(!load._counted){load._counted=true;dryAll(true)}
}
async function post(b){const r=await fetch('api/send',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)});return r.json()}
async function one(app,dry){const el=$('#o_'+app.replace(/\\s/g,''));el.textContent=' working…';
 try{const d=await post({dry,app});const v=d.games[app]||{};el.textContent=dry?` reach: ${v.tokens??0}`:` sent ${v.sent??0} / failed ${v.failed??0}`;
  const c=$('#c_'+app.replace(/\\s/g,''));if(c&&v.tokens!=null)c.textContent=v.tokens+' opted in';plat(app,v);if(!dry)load()}catch(e){el.textContent=' error'}}
function plat(g,v){const p=$('#p_'+g.replace(/\\s/g,''));if(!p||!v.byPlatform)return;
 const e=Object.entries(v.byPlatform);p.textContent=e.length?('tokens: '+e.map(([k,n])=>k+' '+n).join(' · ')):'tokens: none'}
async function dryAll(quiet){if(!quiet){$('#allout').textContent='Working…';$('#dryall').disabled=$('#sendall').disabled=true}
 try{const d=await post({dry:true});for(const [g,v] of Object.entries(d.games)){const c=$('#c_'+g.replace(/\\s/g,''));if(c)c.textContent=(v.tokens??0)+' opted in';plat(g,v)}
  if(!quiet)$('#allout').textContent='Reach: '+Object.entries(d.games).map(([g,v])=>`${g} ${v.tokens??0}`).join(' · ')}catch(e){if(!quiet)$('#allout').textContent=e}
 $('#dryall').disabled=$('#sendall').disabled=false}
$('#dryall').onclick=()=>dryAll(false);
$('#sendall').onclick=async()=>{if(!confirm('Send each app its own campaign to all opted-in players now?'))return;
 $('#allout').textContent='Sending…';$('#sendall').disabled=true;const d=await post({dry:false});
 $('#allout').textContent='Sent: '+Object.entries(d.games).map(([g,v])=>`${g} ${v.sent??0}✓`).join(' · ');$('#sendall').disabled=false;load()};
load();setInterval(load,30000);
</script></body></html>"""
