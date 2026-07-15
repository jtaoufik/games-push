# games-push
Retention push service for the iOS games (Maze Glass, Bloom, Trivio). Runs a
Tue/Thu/Sun 19:00 (Europe/Paris) rotating campaign to opted-in players, plus a
small web UI to preview reach and send on demand.

Env: `FIREBASE_REFRESH_TOKEN` (Firebase CLI OAuth refresh token, cloud-platform scope).
Deploys on Coolify from this repo (Dockerfile). Port 8000.
