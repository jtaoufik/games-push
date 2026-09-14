# games-push

Retention push service for the games fleet (Maze Glass, Bloom, Trivio, Forge).
Runs a Tue/Thu/Sun 19:00 (Europe/Paris) rotating campaign to opted-in players,
plus a small web UI to preview reach and send on demand.

Every message carries a `data` block with a routing `kind` so a tap lands on the
screen the copy is selling, instead of on whatever screen the app opened on. The
kind is declared per campaign message in `campaigns.json`, not per app: "solve a
maze right now" routes to `play`, "defend your rank" routes to `leaderboard`.
The contract lives in `mobile/push-routing.md`. The key is `kind`, never `type`.

`GET /api/status` returns the exact message each app would send right now
(`payloads`, token redacted), so the payload can be checked without sending.
A dry run (`POST /api/send {"dry":true}`) adds `byPlatform`, the opt-in count
split ios / android per app.

Env: `FIREBASE_REFRESH_TOKEN` (Firebase CLI OAuth refresh token, cloud-platform scope).
Deploys on Coolify from this repo (Dockerfile). Port 8000.
