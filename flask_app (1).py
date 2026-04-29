import os
import io
import sqlite3
import hashlib
import secrets
import datetime
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, session, abort,
    send_from_directory, send_file, render_template_string, flash
)
from werkzeug.utils import secure_filename

from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

import arabic_reshaper
from bidi.algorithm import get_display


# =========================================================
#                       CONFIG
# =========================================================
APP_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(APP_DIR, "cubeq.db")
UPLOAD_DIR  = os.path.join(APP_DIR, "uploads")
FONT_PATH = "/home/Taxesaja/mysite/amiri-regular.ttf"
ADMIN_USER  = "mden"
ADMIN_PASS  = "19951995"
ALLOWED_IMG = {"png", "jpg", "jpeg", "gif", "webp"}

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(16))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

# arabic font for pdf — must register Amiri or arabic glyphs will be black squares
PDF_FONT = "Helvetica"
try:
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50_000:
        pdfmetrics.registerFont(TTFont("Amiri", FONT_PATH))
        PDF_FONT = "Amiri"
    else:
        print(f"[cubeq] WARN: Amiri font missing or too small at {FONT_PATH}; PDF arabic will not render.")
except Exception as _e:
    print(f"[cubeq] WARN: failed to register Amiri font: {_e}")


# =========================================================
#                    DB / HELPERS
# =========================================================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def ar(text) -> str:
    try:
        return get_display(arabic_reshaper.reshape(str(text)))
    except Exception:
        return str(text)


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS houses(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          address TEXT,
          engineer_percent REAL DEFAULT 0,
          status TEXT DEFAULT 'active',
          notes TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT UNIQUE NOT NULL,
          password TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('admin','worker','owner')),
          name TEXT,
          house_id INTEGER,
          job TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(house_id) REFERENCES houses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS purchases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          house_id INTEGER NOT NULL,
          category TEXT,
          item TEXT NOT NULL,
          qty REAL DEFAULT 1,
          price REAL NOT NULL,
          vendor TEXT,
          notes TEXT,
          worker_id INTEGER,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(house_id) REFERENCES houses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          house_id INTEGER NOT NULL,
          worker_id INTEGER,
          amount REAL NOT NULL,
          notes TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(house_id) REFERENCES houses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS progress(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          house_id INTEGER NOT NULL,
          percent REAL NOT NULL,
          notes TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(house_id) REFERENCES houses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS money_requests(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          house_id INTEGER NOT NULL,
          worker_id INTEGER NOT NULL,
          amount REAL NOT NULL,
          reason TEXT,
          status TEXT DEFAULT 'pending',
          admin_note TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          decided_at TEXT,
          FOREIGN KEY(house_id) REFERENCES houses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          house_id INTEGER NOT NULL,
          worker_id INTEGER NOT NULL,
          from_role TEXT NOT NULL,
          text TEXT,
          image_path TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(house_id) REFERENCES houses(id) ON DELETE CASCADE
        );
        """)
        cur = c.execute("SELECT id FROM users WHERE username=?", (ADMIN_USER,))
        if not cur.fetchone():
            c.execute(
                "INSERT INTO users(username,password,role,name) VALUES (?,?,?,?)",
                (ADMIN_USER, hash_pw(ADMIN_PASS), "admin", "Admin")
            )
        # ---- migrations: add columns if missing ----
        for table in ("purchases", "payments"):
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if "receipt_image" not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN receipt_image TEXT")


init_db()


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with db() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def login_required(roles=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("login"))
            if roles and u["role"] not in roles:
                abort(403)
            return fn(u, *a, **kw)
        return wrapper
    return deco


def get_house(house_id):
    with db() as c:
        return c.execute("SELECT * FROM houses WHERE id=?", (house_id,)).fetchone()


def fmt_money(v):
    try:
        return f"{float(v):,.0f}"
    except Exception:
        return str(v)


def allowed_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_IMG


def save_upload(file_storage):
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        return None
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    fname = f"{secrets.token_hex(8)}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, fname))
    return fname


def now_str():
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


# =========================================================
#                       TEMPLATES
# =========================================================
BASE_CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#eef2f7;
  --primary:#4f46e5; --primary-2:#4338ca;
  --accent:#f59e0b;
  --surface:#fff; --surface-2:#f8fafc;
  --text:#0f172a; --text-2:#64748b; --text-3:#94a3b8;
  --border:#e2e8f0; --border-2:#f1f5f9;
  --success:#16a34a; --danger:#ef4444; --warning:#f59e0b; --info:#0ea5e9;
  --radius:18px; --radius-sm:12px;
  --shadow:0 8px 24px rgba(15,23,42,.06);
  --shadow-lg:0 20px 50px rgba(15,23,42,.14);
  --hero:linear-gradient(135deg,#4f46e5 0%,#7c3aed 60%,#9333ea 100%);
  --primary-rgb:79,70,229;
}
body[data-role="admin"]{--primary:#4f46e5;--primary-2:#4338ca;--accent:#f59e0b;--primary-rgb:79,70,229;
  --hero:linear-gradient(135deg,#4f46e5 0%,#7c3aed 60%,#9333ea 100%)}
body[data-role="worker"]{--primary:#0d9488;--primary-2:#0f766e;--accent:#f97316;--primary-rgb:13,148,136;
  --hero:linear-gradient(135deg,#0d9488 0%,#0891b2 50%,#0284c7 100%)}
body[data-role="owner"]{--primary:#059669;--primary-2:#047857;--accent:#d97706;--primary-rgb:5,150,105;
  --hero:linear-gradient(135deg,#0f766e 0%,#059669 50%,#10b981 100%)}

html,body{margin:0;padding:0;color:var(--text);min-height:100vh}
body{
  direction:rtl;
  font-family:'Cairo','Tahoma','Segoe UI',sans-serif;
  background:var(--bg);
  background-image:radial-gradient(at 90% -10%,rgba(var(--primary-rgb),.12),transparent 50%),
                   radial-gradient(at -10% 100%,rgba(var(--primary-rgb),.08),transparent 50%);
  background-attachment:fixed;
  padding-bottom:96px;
}
a{color:var(--primary);text-decoration:none}
a:hover{opacity:.85}

/* ===== App Bar ===== */
.appbar{
  position:sticky;top:0;z-index:50;
  background:var(--hero);color:#fff;
  padding:.95rem 1.1rem;border-radius:0 0 26px 26px;
  box-shadow:var(--shadow);
  display:flex;justify-content:space-between;align-items:center;
}
.appbar .brand{display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:1.15rem;letter-spacing:.5px}
.appbar .brand .logo{
  width:38px;height:38px;border-radius:11px;background:rgba(255,255,255,.18);
  display:grid;place-items:center;backdrop-filter:blur(10px)
}
.appbar .who{display:flex;align-items:center;gap:.6rem;font-size:.9rem;font-weight:600}
.appbar .role-pill{background:rgba(255,255,255,.18);padding:.25rem .65rem;border-radius:99px;font-size:.78rem}
.appbar .avatar{
  width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,.22);
  display:grid;place-items:center;font-weight:800;color:#fff;font-size:1rem
}

.container{max-width:980px;margin:1rem auto;padding:0 1rem}

/* ===== Cards ===== */
.card{background:var(--surface);border-radius:var(--radius);padding:1.15rem;margin-bottom:1rem;
  box-shadow:var(--shadow);border:1px solid var(--border-2)}
.card h2,.card h3{margin:0 0 .8rem 0;color:var(--text)}
.card .head{display:flex;justify-content:space-between;align-items:center;margin-bottom:.9rem;gap:.5rem;flex-wrap:wrap}

/* ===== Hero ===== */
.hero{
  background:var(--hero);color:#fff;border-radius:var(--radius);
  padding:1.5rem 1.3rem;margin-bottom:1rem;box-shadow:var(--shadow-lg);
  position:relative;overflow:hidden;
}
.hero h1{margin:0 0 .3rem 0;font-size:1.65rem;font-weight:800}
.hero .sub{opacity:.9;font-size:.95rem}
.hero::after{content:"";position:absolute;left:-40px;top:-40px;width:200px;height:200px;
  background:rgba(255,255,255,.08);border-radius:50%}
.hero::before{content:"";position:absolute;left:30%;bottom:-60px;width:140px;height:140px;
  background:rgba(255,255,255,.06);border-radius:50%}
.hero .actions{margin-top:1rem;display:flex;gap:.5rem;flex-wrap:wrap;position:relative;z-index:1}

/* ===== KPI ===== */
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem}
.kpi .box{background:linear-gradient(180deg,#fff,var(--surface-2));border:1px solid var(--border-2);
  border-radius:14px;padding:.9rem 1rem}
.kpi .box .l{font-size:.74rem;color:var(--text-2);font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.kpi .box .v{font-size:1.5rem;font-weight:800;color:var(--text);margin-top:.25rem;line-height:1.1}
.kpi .box.accent{background:linear-gradient(135deg,var(--primary),var(--primary-2));color:#fff;border:0}
.kpi .box.accent .l,.kpi .box.accent .v{color:#fff}

/* ===== Buttons ===== */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:.4rem;
  background:var(--primary);color:#fff;padding:.65rem 1.05rem;border-radius:12px;border:0;cursor:pointer;
  font-size:.95rem;font-family:inherit;font-weight:700;transition:transform .08s,box-shadow .15s,filter .15s;
  box-shadow:0 4px 14px rgba(var(--primary-rgb),.32);
  text-decoration:none;
}
.btn:hover{transform:translateY(-1px);filter:brightness(1.05);color:#fff;text-decoration:none}
.btn:active{transform:translateY(0)}
.btn-ghost{background:transparent;color:var(--primary);box-shadow:none;border:1.5px solid var(--border)}
.btn-ghost:hover{background:var(--surface-2);color:var(--primary)}
.btn-light{background:rgba(255,255,255,.22);color:#fff;box-shadow:none;backdrop-filter:blur(10px)}
.btn-light:hover{background:rgba(255,255,255,.32);color:#fff}
.btn-danger{background:var(--danger);box-shadow:0 4px 14px rgba(239,68,68,.3)}
.btn-success{background:var(--success);box-shadow:0 4px 14px rgba(22,163,74,.3)}
.btn-warn{background:var(--warning);color:#0f172a;box-shadow:0 4px 14px rgba(245,158,11,.3)}
.btn-sm{padding:.4rem .8rem;font-size:.85rem;border-radius:10px}
.btn-block{width:100%}

/* ===== Forms ===== */
input,select,textarea{
  width:100%;padding:.75rem .9rem;border:1.5px solid var(--border);
  border-radius:12px;font-family:inherit;font-size:.95rem;background:#fff;
  transition:border-color .15s,box-shadow .15s;color:var(--text);
}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--primary);box-shadow:0 0 0 4px rgba(var(--primary-rgb),.14)}
label{display:block;margin:.75rem 0 .3rem;font-weight:700;font-size:.85rem;color:var(--text-2)}

/* ===== Tables ===== */
.table-wrap,.card{position:relative}
.card > table,.table-wrap{overflow-x:auto;display:block;border-radius:12px}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:.92rem;min-width:520px}
th{padding:.7rem .65rem;background:var(--surface-2);font-weight:700;text-align:right;color:var(--text-2);
  font-size:.74rem;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
td{padding:.7rem .65rem;border-bottom:1px solid var(--border-2);text-align:right;vertical-align:top}
tbody tr:hover td{background:var(--surface-2)}

/* ===== Flash ===== */
.flash{background:#dbeafe;color:#1e40af;padding:.85rem 1rem;border-radius:12px;margin-bottom:1rem;
  border-right:4px solid #3b82f6;font-weight:700}
.flash.err{background:#fee2e2;color:#991b1b;border-color:#ef4444}

/* ===== Badges ===== */
.badge{display:inline-flex;align-items:center;gap:.25rem;padding:.25rem .65rem;border-radius:99px;
  font-size:.78rem;background:var(--border);color:var(--text-2);font-weight:700}
.badge.green{background:#dcfce7;color:#166534}
.badge.red{background:#fee2e2;color:#991b1b}
.badge.amber{background:#fef3c7;color:#92400e}
.badge.blue{background:#dbeafe;color:#1e40af}

/* ===== Progress ===== */
.progress{background:var(--border-2);border-radius:99px;height:14px;overflow:hidden;position:relative}
.progress > div{background:linear-gradient(90deg,var(--primary),var(--accent));height:100%;border-radius:99px;
  transition:width .5s;box-shadow:0 0 12px rgba(var(--primary-rgb),.4)}
  /* ===== House cards ===== */
.house-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1rem}
.house-card{
  background:var(--surface);border-radius:var(--radius);padding:1.15rem;
  box-shadow:var(--shadow);border:1px solid var(--border-2);
  transition:transform .15s,box-shadow .15s;display:block;color:inherit;
  position:relative;overflow:hidden;
}
.house-card:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg);text-decoration:none;color:inherit}
.house-card .ribbon{position:absolute;right:0;top:0;bottom:0;width:5px;background:var(--hero)}
.house-card h4{margin:.1rem 0 .25rem;font-size:1.15rem;color:var(--text)}
.house-card .addr{color:var(--text-2);font-size:.85rem;margin-bottom:.8rem}
.house-card .stats{display:flex;justify-content:space-between;font-size:.8rem;color:var(--text-2);margin-top:.6rem;font-weight:600}

/* ===== Receipts ===== */
.receipt{
  background:var(--surface);border-radius:14px;padding:1rem;
  box-shadow:var(--shadow);border:1px solid var(--border-2);
  display:flex;gap:.85rem;align-items:flex-start;margin-bottom:.7rem;
  position:relative;overflow:hidden;
}
.receipt::before{content:"";position:absolute;right:0;top:0;bottom:0;width:5px;background:var(--accent)}
.receipt.payment::before{background:var(--info)}
.receipt.purchase::before{background:var(--success)}
.receipt .rcicon{width:46px;height:46px;border-radius:12px;background:var(--surface-2);display:grid;
  place-items:center;flex-shrink:0;color:var(--primary)}
.receipt.payment .rcicon{background:#e0f2fe;color:var(--info)}
.receipt.purchase .rcicon{background:#dcfce7;color:var(--success)}
.receipt .rcbody{flex:1;min-width:0}
.receipt .rctitle{font-weight:800;color:var(--text);margin-bottom:.15rem}
.receipt .rcmeta{color:var(--text-2);font-size:.82rem;display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
.receipt .rcmeta .dot{color:var(--text-3)}
.receipt .rcamt{font-weight:800;font-size:1.1rem;color:var(--text);white-space:nowrap;align-self:center}

/* ===== Chat ===== */
.chat{
  background:var(--surface-2);border-radius:14px;padding:.8rem;
  border:1px solid var(--border-2);display:flex;flex-direction:column;gap:.5rem;
  max-height:60vh;overflow:auto;
}
.msg{padding:.6rem .9rem;border-radius:18px;max-width:80%;box-shadow:0 1px 3px rgba(0,0,0,.05);
  word-wrap:break-word;line-height:1.4}
.msg.me{align-self:flex-start;background:var(--primary);color:#fff;border-bottom-right-radius:4px}
.msg.them{align-self:flex-end;background:#fff;border:1px solid var(--border-2);border-bottom-left-radius:4px}
.msg.me .who{color:rgba(255,255,255,.85)}
.msg.me a{color:#fff}
.msg .who{font-size:.7rem;color:var(--text-3);margin-bottom:.25rem;font-weight:700}
.msg img{max-width:240px;border-radius:10px;margin-top:.4rem;display:block}

/* ===== Bottom nav ===== */
.bottomnav{
  position:fixed;left:0;right:0;bottom:0;z-index:50;
  background:var(--surface);border-top:1px solid var(--border);
  display:grid;grid-auto-flow:column;grid-auto-columns:1fr;
  padding:.45rem .3rem;padding-bottom:max(.45rem,env(safe-area-inset-bottom));
  box-shadow:0 -8px 24px rgba(15,23,42,.08);
}
.bottomnav a{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:.2rem;padding:.45rem .3rem;color:var(--text-2);font-size:.72rem;
  border-radius:12px;font-weight:700;
}
.bottomnav a.active{color:var(--primary);background:rgba(var(--primary-rgb),.1)}
.bottomnav a svg{width:22px;height:22px;stroke-width:2;fill:none;stroke:currentColor;stroke-linecap:round;stroke-linejoin:round}

/* ===== Login ===== */
.auth-wrap{min-height:88vh;display:grid;place-items:center;padding:1rem}
.auth-card{background:var(--surface);border-radius:24px;padding:1.7rem;
  box-shadow:var(--shadow-lg);max-width:400px;width:100%;border:1px solid var(--border-2)}
.auth-logo{width:78px;height:78px;border-radius:22px;background:var(--hero);color:#fff;
  display:grid;place-items:center;margin:0 auto 1rem;box-shadow:0 12px 30px rgba(var(--primary-rgb),.4)}
.auth-logo svg{width:38px;height:38px;stroke:#fff;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}

/* ===== Helpers ===== */
.row{display:flex;flex-wrap:wrap;gap:1rem}
.col{flex:1 1 280px;min-width:0}
.muted{color:var(--text-2);font-size:.85rem}
.right{text-align:left}
.center{text-align:center}
.empty{text-align:center;padding:1.5rem;color:var(--text-2)}
.empty .ico{font-size:2.5rem;margin-bottom:.5rem;opacity:.5}
.iconbox{width:40px;height:40px;border-radius:10px;background:rgba(var(--primary-rgb),.1);
  color:var(--primary);display:inline-grid;place-items:center}
hr{border:0;border-top:1px solid var(--border-2);margin:1rem 0}
.gap-sm{gap:.4rem}
.flex{display:flex;align-items:center;gap:.5rem}
.flex-between{display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap}

@media (min-width:780px){
  .bottomnav{display:none}
  body{padding-bottom:1rem}
}
@media (max-width:520px){
  .hero h1{font-size:1.35rem}
  .kpi .box .v{font-size:1.2rem}
  .appbar{padding:.85rem 1rem}
  .container{padding:0 .8rem;margin-top:.7rem}
}
</style>
"""

ICONS = {
    "home":   '<svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8"/><path d="M5 9v11a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9"/></svg>',
    "plus":   '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>',
    "chat":   '<svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-12 7l-5 2 2-5a8 8 0 1 1 15-4z"/></svg>',
    "logout": '<svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>',
    "receipt":'<svg viewBox="0 0 24 24"><path d="M4 3h16v18l-3-2-3 2-3-2-3 2-4-2z"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>',
    "money":  '<svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M6 10v4M18 10v4"/></svg>',
    "request":'<svg viewBox="0 0 24 24"><path d="M12 2v20M5 9l7-7 7 7M5 15l7 7 7-7"/></svg>',
    "house":  '<svg viewBox="0 0 24 24"><path d="M3 12l9-9 9 9"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/></svg>',
    "user":   '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
    "package":'<svg viewBox="0 0 24 24"><path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v9"/></svg>',
    "cube":   '<svg viewBox="0 0 24 24"><path d="M21 16V8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5"/><path d="M12 13v9"/></svg>',
    "back":   '<svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>',
    "edit":   '<svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
    "trash":  '<svg viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>',
    "send":   '<svg viewBox="0 0 24 24"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>',
    "check":  '<svg viewBox="0 0 24 24"><path d="M5 12l5 5L20 7"/></svg>',
    "x":      '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    "pdf":    '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h2M9 17h6M9 9h1"/></svg>',
    "tools":  '<svg viewBox="0 0 24 24"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-2.4z"/></svg>',
    "info":   '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v.01M11 12h1v4h1"/></svg>',
}

ROLE_LABELS = {"admin": "الإدارة", "worker": "الكادر", "owner": "صاحب البيت"}


def nav_link(url, icon, label, active=False):
    cls = "active" if active else ""
    return f'<a href="{url}" class="{cls}">{ICONS[icon]}<span>{label}</span></a>'


def render_bottom_nav(user, active):
    if not user:
        return ""
    role = user["role"]
    items = []
    if role == "admin":
        items.append(nav_link("/admin", "home", "الرئيسية", active == "home"))
        items.append(nav_link("/admin/house/new", "plus", "إضافة بيت", active == "new"))
        items.append(nav_link("/logout", "logout", "خروج"))
    elif role == "worker":
        items.append(nav_link("/worker", "home", "الرئيسية", active == "home"))
        items.append(nav_link("/worker/chat", "chat", "الشات", active == "chat"))
        items.append(nav_link("/logout", "logout", "خروج"))
    else:
        items.append(nav_link("/owner", "home", "بيتي", active == "home"))
        items.append(nav_link("/owner/receipts", "receipt", "الفواتير", active == "receipts"))
        items.append(nav_link("/logout", "logout", "خروج"))
    return f'<nav class="bottomnav">{"".join(items)}</nav>'


BASE = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#4f46e5">
<title>{{ title or 'cubeq' }}</title>
""" + BASE_CSS + """
</head>
<body{% if user %} data-role="{{ user['role'] }}"{% endif %}>
{% if user %}
<div class="appbar">
  <div class="brand">
    <span class="logo">""" + ICONS["cube"] + """</span>
    <span>cubeq</span>
  </div>
  <div class="who">
    <span class="role-pill">{{ role_label }}</span>
    <span class="avatar">{{ (user['name'] or user['username'])[0]|upper }}</span>
  </div>
</div>
{% endif %}
<div class="container">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for cat,msg in messages %}
      <div class="flash {{ 'err' if cat=='err' else '' }}">{{ msg }}</div>
    {% endfor %}
  {% endwith %}
  {{ body|safe }}
</div>
{{ bottomnav|safe }}
<style>.appbar .logo svg{width:22px;height:22px;stroke:#fff;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}</style>
</body></html>
"""


def page(body_html, title="cubeq", user=None, active="home"):
    return render_template_string(
        BASE,
        body=body_html,
        title=title,
        user=user,
        role_label=ROLE_LABELS.get(user["role"]) if user else "",
        bottomnav=render_bottom_nav(user, active),
    )


# =========================================================
#                       AUTH
# =========================================================
LOGIN_PICK_TPL = """
<style>
.pick-wrap{min-height:88vh;display:flex;flex-direction:column;justify-content:center;align-items:center;padding:1.2rem}
.pick-head{text-align:center;margin-bottom:1.5rem}
.pick-head .logo{width:84px;height:84px;border-radius:24px;background:linear-gradient(135deg,#4f46e5,#9333ea);
  display:grid;place-items:center;margin:0 auto 1rem;box-shadow:0 18px 40px rgba(79,70,229,.35)}
.pick-head .logo svg{width:42px;height:42px;stroke:#fff;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}
.pick-head h1{margin:0;font-size:1.8rem;font-weight:800}
.pick-head p{color:var(--text-2);margin:.4rem 0 0}
.pick-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;max-width:760px;width:100%}
.pick-card{
  background:var(--surface);border-radius:22px;padding:1.4rem 1.2rem;
  box-shadow:0 12px 30px rgba(15,23,42,.08);border:1px solid var(--border-2);
  text-align:center;transition:transform .15s,box-shadow .2s;color:inherit;display:block;
  position:relative;overflow:hidden;
}
.pick-card:hover{transform:translateY(-5px);box-shadow:0 24px 50px rgba(15,23,42,.18);text-decoration:none;color:inherit}
.pick-card .strip{position:absolute;top:0;left:0;right:0;height:6px}
.pick-card .ico{width:62px;height:62px;border-radius:18px;display:grid;place-items:center;margin:.4rem auto 1rem;color:#fff}
.pick-card .ico svg{width:32px;height:32px;stroke:#fff;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}
.pick-card h3{margin:0;font-size:1.2rem;font-weight:800}
.pick-card p{color:var(--text-2);font-size:.85rem;margin:.4rem 0 .8rem}
.pick-card .go{font-weight:700;font-size:.9rem}
.pick-admin .strip,.pick-admin .ico{background:linear-gradient(135deg,#4f46e5,#9333ea)}
.pick-admin .go{color:#4f46e5}
.pick-worker .strip,.pick-worker .ico{background:linear-gradient(135deg,#0d9488,#0284c7)}
.pick-worker .go{color:#0d9488}
.pick-owner .strip,.pick-owner .ico{background:linear-gradient(135deg,#059669,#10b981)}
.pick-owner .go{color:#059669}
</style>
<div class="pick-wrap">
  <div class="pick-head">
    <div class="logo">""" + ICONS["cube"] + """</div>
    <h1>cubeq</h1>
    <p>اختر نوع حسابك للمتابعة</p>
  </div>
  <div class="pick-grid">
    <a class="pick-card pick-admin" href="{{ url_for('login', role='admin') }}">
      <div class="strip"></div>
      <div class="ico">""" + ICONS["user"] + """</div>
      <h3>الإدارة</h3>
      <p>إدارة كل البيوت والكوادر والمدفوعات</p>
      <div class="go">دخول ←</div>
    </a>
    <a class="pick-card pick-worker" href="{{ url_for('login', role='worker') }}">
      <div class="strip"></div>
      <div class="ico">""" + ICONS["tools"] + """</div>
      <h3>الكوادر</h3>
      <p>طلب فلوس، شات، متابعة دفعاتك</p>
      <div class="go">دخول ←</div>
    </a>
    <a class="pick-card pick-owner" href="{{ url_for('login', role='owner') }}">
      <div class="strip"></div>
      <div class="ico">""" + ICONS["house"] + """</div>
      <h3>صاحب البيت</h3>
      <p>تابع بيتك، فواتيرك، وأين تذهب أموالك</p>
      <div class="go">دخول ←</div>
    </a>
  </div>
</div>
"""


LOGIN_FORM_TPL = """
<style>
body[data-pick="admin"]{--primary:#4f46e5;--primary-2:#4338ca;--accent:#f59e0b;--primary-rgb:79,70,229;
  --hero:linear-gradient(135deg,#4f46e5,#7c3aed 60%,#9333ea)}
body[data-pick="worker"]{--primary:#0d9488;--primary-2:#0f766e;--accent:#f97316;--primary-rgb:13,148,136;
  --hero:linear-gradient(135deg,#0d9488,#0891b2 50%,#0284c7)}
body[data-pick="owner"]{--primary:#059669;--primary-2:#047857;--accent:#d97706;--primary-rgb:5,150,105;
  --hero:linear-gradient(135deg,#0f766e,#059669 50%,#10b981)}
.login-bg{
  position:fixed;inset:0;z-index:-1;
  background:var(--hero);
  background-attachment:fixed;
}
.login-bg::before,.login-bg::after{content:"";position:absolute;border-radius:50%;background:rgba(255,255,255,.12)}
.login-bg::before{width:340px;height:340px;top:-100px;right:-100px}
.login-bg::after{width:260px;height:260px;bottom:-80px;left:-80px}
.login-card{
  background:#fff;border-radius:28px;padding:1.8rem 1.5rem;
  box-shadow:0 30px 60px rgba(0,0,0,.25);max-width:420px;width:100%;
  position:relative;
}
.login-card .role-badge{
  position:absolute;top:-22px;left:50%;transform:translateX(-50%);
  background:var(--hero);color:#fff;padding:.5rem 1.2rem;border-radius:99px;
  font-weight:800;font-size:.9rem;box-shadow:0 6px 16px rgba(0,0,0,.18);
  display:inline-flex;align-items:center;gap:.4rem
}
.login-card .role-badge svg{width:18px;height:18px;stroke:#fff;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}
.login-card .ttl{text-align:center;margin:.7rem 0 .2rem;font-weight:800;font-size:1.4rem}
.login-card .sub{text-align:center;color:var(--text-2);margin:0 0 1.2rem;font-size:.9rem}
.login-card .back{display:inline-flex;align-items:center;gap:.3rem;color:#fff;
  background:rgba(255,255,255,.18);padding:.4rem .8rem;border-radius:99px;
  font-weight:700;font-size:.85rem;backdrop-filter:blur(8px)}
.login-card .back:hover{background:rgba(255,255,255,.28);color:#fff}
.login-top{display:flex;justify-content:flex-start;width:100%;max-width:420px;margin-bottom:1rem}
</style>
<div class="login-bg"></div>
<div style="min-height:100vh;display:flex;flex-direction:column;justify-content:center;align-items:center;padding:1.2rem">
  <div class="login-top"><a class="back" href="{{ url_for('login') }}">← تغيير الحساب</a></div>
  <div class="login-card">
    <div class="role-badge">{{ icon|safe }} {{ label }}</div>
    <h2 class="ttl">مرحباً بعودتك</h2>
    <p class="sub">دخول إلى حساب {{ label }}</p>
    <form method="post">
      <input type="hidden" name="role" value="{{ role }}">
      <label>اسم المستخدم</label>
      <input name="username" required autofocus autocomplete="username">
      <label>كلمة المرور</label>
      <input name="password" type="password" required autocomplete="current-password">
      <div style="margin-top:1.2rem"><button class="btn btn-block">دخول</button></div>
    </form>
  </div>
</div>
"""


_LOGIN_ROLES = {
    "admin":  ("الإدارة", ICONS["user"]),
    "worker": ("الكادر", ICONS["tools"]),
    "owner":  ("صاحب البيت", ICONS["house"]),
}


@app.route("/login", methods=["GET", "POST"])
def login():
    role = request.values.get("role", "")
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        with db() as c:
            row = c.execute(
                "SELECT * FROM users WHERE username=? AND password=?",
                (u, hash_pw(p))
            ).fetchone()
        if row:
            session["uid"] = row["id"]
            return redirect(url_for("home"))
        flash("بيانات الدخول غير صحيحة", "err")
    if role in _LOGIN_ROLES:
        label, icon = _LOGIN_ROLES[role]
        body = render_template_string(LOGIN_FORM_TPL, role=role, label=label, icon=icon)
        # render WITHOUT user, but inject body data-pick attribute via JS
        html = render_template_string(
            BASE, body=body, title=f"دخول — {label}",
            user=None, role_label="", bottomnav="",
        )
        # Insert data-pick attribute on body so themed CSS variables apply
        html = html.replace("<body>", f'<body data-pick="{role}">', 1)
        return html
    body = render_template_string(LOGIN_PICK_TPL)
    return page(body, title="دخول — cubeq")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    if u["role"] == "admin":
        return redirect(url_for("admin_home"))
    if u["role"] == "worker":
        return redirect(url_for("worker_home"))
    if u["role"] == "owner":
        return redirect(url_for("owner_home"))
    abort(403)


# =========================================================
#                       ADMIN
# =========================================================
ADMIN_HOME_TPL = """
<div class="hero">
  <h1>أهلاً، {{ user['name'] or user['username'] }}</h1>
  <div class="sub">إدارة كل بيوتك ومشاريعك من مكان واحد.</div>
  <div class="actions">
    <a class="btn btn-light" href="{{ url_for('admin_house_new') }}">+ إضافة بيت جديد</a>
  </div>
</div>

<div class="card">
  <div class="kpi">
    <div class="box accent"><div class="l">عدد البيوت</div><div class="v">{{ houses|length }}</div></div>
    <div class="box"><div class="l">المشاريع النشطة</div><div class="v">{{ active_count }}</div></div>
    <div class="box"><div class="l">إجمالي الصرف</div><div class="v">{{ fm(total_spend) }}</div></div>
    <div class="box"><div class="l">طلبات معلقة</div><div class="v">{{ pending_count }}</div></div>
  </div>
</div>

{% if houses %}
<div class="card">
  <h3>بيوتك</h3>
  <div class="house-grid">
    {% for h in houses %}
      <a class="house-card" href="{{ url_for('admin_house', hid=h['id']) }}">
        <div class="ribbon"></div>
        <div class="flex-between">
          <span class="iconbox">""" + ICONS["house"] + """</span>
          {% if h['status']=='active' %}<span class="badge green">نشط</span>
          {% else %}<span class="badge red">مغلق</span>{% endif %}
        </div>
        <h4>{{ h['name'] }}</h4>
        <div class="addr">{{ h['address'] or 'بدون عنوان' }}</div>
        <div class="progress"><div style="width:{{ h['percent'] }}%"></div></div>
        <div class="stats">
          <span>الإنجاز {{ h['percent'] }}%</span>
          <span>صرف {{ fm(h['spend']) }}</span>
        </div>
      </a>
    {% endfor %}
  </div>
</div>
{% else %}
<div class="card">
  <div class="empty">
    <div class="ico">""" + ICONS["house"] + """</div>
    <p>لا توجد بيوت بعد. أضف أول بيت للبدء.</p>
    <a class="btn" href="{{ url_for('admin_house_new') }}">+ إضافة بيت</a>
  </div>
</div>
{% endif %}
<style>.empty .ico svg{width:60px;height:60px;stroke:var(--text-3);stroke-width:1.6;fill:none}
.iconbox svg{width:22px;height:22px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}</style>
"""


@app.route("/admin")
@login_required(roles=["admin"])
def admin_home(u):
    with db() as c:
        houses = c.execute("SELECT * FROM houses ORDER BY id DESC").fetchall()
        out = []
        total_spend = 0.0
        for h in houses:
            p = c.execute(
                "SELECT percent FROM progress WHERE house_id=? ORDER BY id DESC LIMIT 1",
                (h["id"],)
            ).fetchone()
            sp = c.execute(
                "SELECT COALESCE(SUM(price),0) AS s FROM purchases WHERE house_id=?",
                (h["id"],)
            ).fetchone()["s"]
            pa = c.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE house_id=?",
                (h["id"],)
            ).fetchone()["s"]
            d = dict(h)
            d["percent"] = round(p["percent"] if p else 0, 1)
            d["spend"] = sp + pa
            total_spend += d["spend"]
            out.append(d)
        active_count = sum(1 for h in out if h["status"] == "active")
        pending_count = c.execute(
            "SELECT COUNT(*) AS c FROM money_requests WHERE status='pending'"
        ).fetchone()["c"]
    body = render_template_string(
        ADMIN_HOME_TPL, houses=out, user=u, fm=fmt_money,
        active_count=active_count, total_spend=total_spend,
        pending_count=pending_count,
    )
    return page(body, "الإدارة - cubeq", user=u, active="home")


HOUSE_NEW_TPL = """
<div class="card">
  <h2>إضافة بيت</h2>
  <form method="post">
    <label>اسم البيت</label>
    <input name="name" required>
    <label>العنوان</label>
    <input name="address">
    <label>نسبة المهندس %</label>
    <input name="engineer_percent" type="number" step="0.01" value="0">
    <label>ملاحظات</label>
    <textarea name="notes" rows="2"></textarea>
    <div style="margin-top:.8rem">
      <button class="btn">حفظ</button>
      <a href="{{ url_for('admin_home') }}">إلغاء</a>
    </div>
  </form>
</div>
"""


@app.route("/admin/house/new", methods=["GET", "POST"])
@login_required(roles=["admin"])
def admin_house_new(u):
    if request.method == "POST":
        f = request.form
        with db() as c:
            cur = c.execute(
                "INSERT INTO houses(name,address,engineer_percent,notes) VALUES (?,?,?,?)",
                (f.get("name", "").strip(),
                 f.get("address", "").strip(),
                 float(f.get("engineer_percent") or 0),
                 f.get("notes", "").strip())
            )
            hid = cur.lastrowid
            c.execute(
                "INSERT INTO progress(house_id,percent,notes) VALUES (?,?,?)",
                (hid, 0, "بداية")
            )
        flash("تم إضافة البيت")
        return redirect(url_for("admin_house", hid=hid))
    body = render_template_string(HOUSE_NEW_TPL)
    return page(body, "إضافة بيت", user=u)


FOLDER_CSS = """
<style>
.folders-section{margin-top:1rem}
.folders-section .head{display:flex;justify-content:space-between;align-items:center;margin-bottom:.6rem;padding:0 .2rem}
.folders-section .head h3{margin:0;display:flex;align-items:center;gap:.5rem}
.folders-section .head h3 .ico{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:rgba(var(--primary-rgb),.12);color:var(--primary)}
.folders-section .head h3 .ico svg{width:18px;height:18px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}
.folders-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.7rem}
.folder{
  background:var(--surface);border-radius:18px;padding:1rem;
  box-shadow:0 6px 14px rgba(15,23,42,.06);border:1px solid var(--border-2);
  display:flex;flex-direction:column;gap:.4rem;color:inherit;text-decoration:none;
  transition:transform .15s,box-shadow .2s;position:relative;overflow:hidden
}
.folder:hover{transform:translateY(-3px);box-shadow:0 14px 28px rgba(15,23,42,.12);text-decoration:none;color:inherit}
.folder .tab{position:absolute;top:0;right:14px;width:42px;height:10px;background:var(--accent);border-radius:0 0 8px 8px}
.folder .ico{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--primary),var(--primary-2));color:#fff;margin-top:.4rem}
.folder .ico svg{width:22px;height:22px;stroke:#fff;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}
.folder.worker .ico{background:linear-gradient(135deg,#0d9488,#0891b2)}
.folder.cat .ico{background:linear-gradient(135deg,#f59e0b,#ef4444)}
.folder .name{font-weight:800;font-size:1rem;line-height:1.3}
.folder .sub{color:var(--text-2);font-size:.78rem}
.folder .amt{font-weight:800;color:var(--primary);font-size:1.05rem}
.folder .row{display:flex;justify-content:space-between;align-items:center;font-size:.8rem;color:var(--text-2)}
.actbar{display:flex;gap:.5rem;flex-wrap:wrap;margin:.4rem 0}
.empty-mini{padding:1rem;text-align:center;color:var(--text-2);font-size:.9rem;
  background:rgba(0,0,0,.02);border-radius:14px;border:1px dashed var(--border)}
.requests-card .req{display:grid;grid-template-columns:1fr auto;gap:.4rem;padding:.7rem;border-bottom:1px solid var(--border-2)}
.requests-card .req:last-child{border-bottom:none}
.requests-card .req .who{font-weight:700}
.requests-card .req .meta{color:var(--text-2);font-size:.82rem}
.requests-card .req .act{display:flex;gap:.3rem;align-items:center;flex-wrap:wrap}
</style>
"""


HOUSE_TPL = FOLDER_CSS + """
<div class="hero">
  <div class="flex-between" style="align-items:flex-start">
    <div>
      <h1>{{ h['name'] }}
        {% if h['status']=='closed' %}<span class="badge" style="background:rgba(255,255,255,.22);color:#fff">مغلق</span>{% endif %}
      </h1>
      <div class="sub">{{ h['address'] or 'بدون عنوان' }} · نسبة المهندس {{ h['engineer_percent'] }}%</div>
    </div>
    <a class="btn btn-light btn-sm" href="{{ url_for('admin_home') }}">رجوع</a>
  </div>
  <div class="actions">
    {% if h['status']=='active' %}
      <a class="btn btn-light btn-sm" href="{{ url_for('admin_house_edit', hid=h['id']) }}">تعديل</a>
      <a class="btn btn-warn btn-sm" href="{{ url_for('admin_house_close', hid=h['id']) }}"
         onclick="return confirm('سيتم إغلاق البيت وإصدار الفاتورة. متابعة؟')">إنهاء وفاتورة نهائية</a>
    {% endif %}
    <a class="btn btn-light btn-sm" href="{{ url_for('house_pdf', hid=h['id']) }}">تحميل PDF</a>
  </div>
</div>

<div class="card">
  <div class="kpi">
    <div class="box accent"><div class="l">إجمالي الصرف</div><div class="v">{{ fm(total_purchases + total_payments) }}</div></div>
    <div class="box"><div class="l">دفعات الكوادر</div><div class="v">{{ fm(total_payments) }}</div></div>
    <div class="box"><div class="l">المشتريات</div><div class="v">{{ fm(total_purchases) }}</div></div>
    <div class="box"><div class="l">قيمة المهندس</div><div class="v">{{ fm(engineer_amount) }}</div></div>
  </div>
  <hr>
  <label>نسبة الإنجاز الحالية: {{ current_percent }}%</label>
  <div class="progress"><div style="width:{{ current_percent }}%"></div></div>
  {% if h['status']=='active' %}
  <form method="post" action="{{ url_for('admin_house_progress', hid=h['id']) }}" style="margin-top:.6rem;display:flex;gap:.5rem;flex-wrap:wrap">
    <input name="percent" type="number" step="0.1" min="0" max="100" placeholder="نسبة جديدة %" required style="max-width:180px">
    <input name="notes" placeholder="ملاحظة (اختياري)" style="flex:1;min-width:200px">
    <button class="btn">تحديث</button>
  </form>
  {% endif %}
</div>

{% if h['status']=='active' %}
<div class="actbar">
  <a class="btn btn-sm" href="{{ url_for('admin_house_user_new', hid=h['id']) }}">+ كادر / صاحب بيت</a>
  <a class="btn btn-sm" href="{{ url_for('admin_purchase_new', hid=h['id']) }}">+ مشترى</a>
  <a class="btn btn-sm" href="{{ url_for('admin_payment_new', hid=h['id']) }}">+ دفعة لكادر</a>
</div>
{% endif %}

<!-- ===== ملفات الكوادر ===== -->
<div class="folders-section">
  <div class="head">
    <h3><span class="ico">""" + ICONS["tools"] + """</span> ملفات الكوادر</h3>
    <span class="muted" style="font-size:.85rem">{{ workers|length }} كادر</span>
  </div>
  {% if workers %}
  <div class="folders-grid">
    {% for w in workers %}
    <a class="folder worker" href="{{ url_for('admin_worker_file', hid=h['id'], uid=w['id']) }}">
      <div class="tab"></div>
      <div class="ico">""" + ICONS["user"] + """</div>
      <div class="name">{{ w['name'] or w['username'] }}</div>
      <div class="sub">{{ w['job'] or 'بدون مهنة' }}</div>
      <div class="row"><span>المستلم</span><span class="amt">{{ fm(w['paid']) }}</span></div>
      <div class="row"><span>مشترياته</span><span>{{ fm(w['bought']) }}</span></div>
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty-mini">لا يوجد كوادر بعد. اضغط "+ كادر / صاحب بيت" لإضافتهم.</div>
  {% endif %}
</div>

<!-- ===== ملفات الأغراض / الفئات ===== -->
<div class="folders-section">
  <div class="head">
    <h3><span class="ico">""" + ICONS["package"] + """</span> ملفات الأغراض</h3>
    <span class="muted" style="font-size:.85rem">{{ categories|length }} ملف</span>
  </div>
  {% if categories %}
  <div class="folders-grid">
    {% for c in categories %}
    <a class="folder cat" href="{{ url_for('admin_category_file', hid=h['id'], name=c['name']) }}">
      <div class="tab"></div>
      <div class="ico">""" + ICONS["package"] + """</div>
      <div class="name">{{ c['name'] }}</div>
      <div class="sub">{{ c['count'] }} عملية</div>
      <div class="row"><span>الإجمالي</span><span class="amt">{{ fm(c['total']) }}</span></div>
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty-mini">لا توجد مشتريات بعد. اضغط "+ مشترى" لإضافة أول فئة.</div>
  {% endif %}
</div>

<!-- ===== صاحب البيت ===== -->
{% if owners %}
<div class="card" style="margin-top:1rem">
  <h3>صاحب البيت</h3>
  {% for x in owners %}
    <div class="flex-between" style="padding:.5rem 0;border-bottom:1px solid var(--border-2)">
      <div>
        <div style="font-weight:700">{{ x['name'] or x['username'] }}</div>
        <div class="muted" style="font-size:.82rem">{{ x['username'] }}</div>
      </div>
      {% if h['status']=='active' %}
      <a class="btn btn-sm btn-danger" href="{{ url_for('admin_user_delete', hid=h['id'], uid=x['id']) }}" onclick="return confirm('حذف؟')">حذف</a>
      {% endif %}
    </div>
  {% endfor %}
</div>
{% endif %}

<!-- ===== طلبات معلقة ===== -->
{% if requests %}
<div class="card requests-card" style="margin-top:1rem">
  <h3>طلبات الكوادر</h3>
  {% for r in requests %}
    <div class="req">
      <div>
        <div class="who">{{ r['worker_name'] }} — {{ fm(r['amount']) }}</div>
        <div class="meta">{{ r['created_at'][:16] }} · {{ r['reason'] or '—' }}</div>
        {% if r['admin_note'] %}<div class="meta">ملاحظة: {{ r['admin_note'] }}</div>{% endif %}
      </div>
      <div class="act">
        {% if r['status']=='pending' %}
          <span class="badge amber">قيد الانتظار</span>
          {% if h['status']=='active' %}
          <form method="post" action="{{ url_for('admin_request_decide', hid=h['id'], rid=r['id']) }}" style="display:flex;gap:.3rem;flex-wrap:wrap">
            <input name="note" placeholder="ملاحظة" style="min-width:100px">
            <button class="btn btn-sm btn-success" name="decision" value="approve">موافقة</button>
            <button class="btn btn-sm btn-danger" name="decision" value="reject">رفض</button>
          </form>
          {% endif %}
        {% elif r['status']=='approved' %}<span class="badge green">موافق</span>
        {% else %}<span class="badge red">مرفوض</span>{% endif %}
      </div>
    </div>
  {% endfor %}
</div>
{% endif %}
"""


WORKER_FILE_TPL = FOLDER_CSS + """
<div class="hero">
  <div class="flex-between" style="align-items:flex-start">
    <div>
      <h1>{{ w['name'] or w['username'] }}</h1>
      <div class="sub">{{ w['job'] or 'كادر' }} · بيت {{ h['name'] }}</div>
    </div>
    <a class="btn btn-light btn-sm" href="{{ back_url }}">رجوع</a>
  </div>
  <div class="actions">
    {% if can_chat %}<a class="btn btn-light btn-sm" href="{{ url_for('admin_chat', hid=h['id'], wid=w['id']) }}">شات</a>{% endif %}
  </div>
</div>

<div class="card">
  <div class="kpi">
    <div class="box accent"><div class="l">إجمالي المستلم</div><div class="v">{{ fm(paid_total) }}</div></div>
    <div class="box"><div class="l">مشترياته</div><div class="v">{{ fm(bought_total) }}</div></div>
  </div>
</div>

<!-- ===== الدفعات المستلمة ===== -->
<div class="card" style="margin-top:1rem">
  <div class="head" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.6rem">
    <h3 style="margin:0;display:flex;align-items:center;gap:.5rem">
      <span style="color:#0d9488">""" + ICONS["money"] + """</span>
      الدفعات المستلمة من الإدارة
    </h3>
    <span class="badge blue">{{ payments|length }}</span>
  </div>
  {% if payments %}
    {% for p in payments %}
      <div class="receipt payment">
        <span class="rcicon">""" + ICONS["money"] + """</span>
        <div class="rcbody">
          <div class="rctitle">{{ p['notes'] or 'دفعة لكادر' }}</div>
          <div class="rcmeta">
            <span>{{ (p['created_at'] or '')[:16] }}</span>
            {% if p['receipt_image'] %}
              <span class="dot">•</span>
              <a href="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" target="_blank">عرض الإيصال</a>
            {% endif %}
          </div>
          {% if p['receipt_image'] %}
            <a href="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" target="_blank">
              <img src="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" style="max-width:160px;max-height:120px;border-radius:10px;margin-top:.4rem;border:1px solid var(--border)">
            </a>
          {% endif %}
        </div>
        <div class="rcamt">{{ fm(p['amount']) }}</div>
      </div>
    {% endfor %}
  {% else %}
    <div class="empty-mini">لم يستلم هذا الكادر أي دفعة بعد.</div>
  {% endif %}
</div>

<!-- ===== المشتريات التي مرت من خلاله ===== -->
<div class="card" style="margin-top:1rem">
  <div class="head" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.6rem">
    <h3 style="margin:0;display:flex;align-items:center;gap:.5rem">
      <span style="color:#f59e0b">""" + ICONS["package"] + """</span>
      المشتريات التي تمت بواسطته
    </h3>
    <span class="badge green">{{ purchases|length }}</span>
  </div>
  {% if purchases %}
    {% for p in purchases %}
      <div class="receipt purchase">
        <span class="rcicon">""" + ICONS["package"] + """</span>
        <div class="rcbody">
          <div class="rctitle">{{ p['item'] }}</div>
          <div class="rcmeta">
            <span>{{ (p['created_at'] or '')[:16] }}</span>
            <span class="dot">•</span>
            <span class="badge green">{{ p['category'] or '—' }}</span>
            {% if p['vendor'] %}<span class="dot">•</span><span>{{ p['vendor'] }}</span>{% endif %}
            {% if p['receipt_image'] %}
              <span class="dot">•</span>
              <a href="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" target="_blank">عرض الفاتورة</a>
            {% endif %}
          </div>
          {% if p['receipt_image'] %}
            <a href="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" target="_blank">
              <img src="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" style="max-width:160px;max-height:120px;border-radius:10px;margin-top:.4rem;border:1px solid var(--border)">
            </a>
          {% endif %}
        </div>
        <div class="rcamt">{{ fm(p['price']) }}</div>
      </div>
    {% endfor %}
  {% else %}
    <div class="empty-mini">لم يشترِ هذا الكادر أي شيء بعد.</div>
  {% endif %}
</div>
<style>.rcicon svg{width:22px;height:22px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}</style>
"""


CATEGORY_FILE_TPL = FOLDER_CSS + """
<div class="hero">
  <div class="flex-between" style="align-items:flex-start">
    <div>
      <h1>{{ name }}</h1>
      <div class="sub">ملف فئة — بيت {{ h['name'] }}</div>
    </div>
    <a class="btn btn-light btn-sm" href="{{ back_url }}">رجوع</a>
  </div>
</div>

<div class="card">
  <div class="kpi">
    <div class="box accent"><div class="l">إجمالي صرف هذه الفئة</div><div class="v">{{ fm(total) }}</div></div>
    <div class="box"><div class="l">عدد العمليات</div><div class="v">{{ purchases|length }}</div></div>
  </div>
</div>
<div class="card" style="margin-top:1rem">
  <h3 style="display:flex;align-items:center;gap:.5rem">
    <span style="color:#f59e0b">""" + ICONS["package"] + """</span>
    كل المشتريات
  </h3>
  {% for p in purchases %}
    <div class="receipt purchase">
      <span class="rcicon">""" + ICONS["package"] + """</span>
      <div class="rcbody">
        <div class="rctitle">{{ p['item'] }}</div>
        <div class="rcmeta">
          <span>{{ (p['created_at'] or '')[:16] }}</span>
          {% if p['worker_name'] %}<span class="dot">•</span><span>الكادر: {{ p['worker_name'] }}</span>{% endif %}
          {% if p['vendor'] %}<span class="dot">•</span><span>{{ p['vendor'] }}</span>{% endif %}
          {% if p['notes'] %}<span class="dot">•</span><span>{{ p['notes'] }}</span>{% endif %}
          {% if p['receipt_image'] %}
            <span class="dot">•</span>
            <a href="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" target="_blank">عرض الفاتورة</a>
          {% endif %}
        </div>
        {% if p['receipt_image'] %}
          <a href="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" target="_blank">
            <img src="{{ url_for('uploaded_file', filename=p['receipt_image']) }}" style="max-width:160px;max-height:120px;border-radius:10px;margin-top:.4rem;border:1px solid var(--border)">
          </a>
        {% endif %}
      </div>
      <div class="rcamt">{{ fm(p['price']) }}</div>
    </div>
  {% endfor %}
</div>
<style>.rcicon svg{width:22px;height:22px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}</style>
"""


def _gather_house_context(hid):
    with db() as c:
        h = c.execute("SELECT * FROM houses WHERE id=?", (hid,)).fetchone()
        if not h:
            return None
        users = c.execute(
            "SELECT * FROM users WHERE house_id=? ORDER BY role,id", (hid,)
        ).fetchall()
        purchases = c.execute("""
            SELECT p.*, u.name AS worker_name
            FROM purchases p LEFT JOIN users u ON u.id=p.worker_id
            WHERE p.house_id=? ORDER BY p.id DESC
        """, (hid,)).fetchall()
        payments = c.execute("""
            SELECT p.*, u.name AS worker_name
            FROM payments p LEFT JOIN users u ON u.id=p.worker_id
            WHERE p.house_id=? ORDER BY p.id DESC
        """, (hid,)).fetchall()
        requests_ = c.execute("""
            SELECT r.*, u.name AS worker_name
            FROM money_requests r LEFT JOIN users u ON u.id=r.worker_id
            WHERE r.house_id=? ORDER BY r.id DESC
        """, (hid,)).fetchall()
        prog = c.execute(
            "SELECT percent FROM progress WHERE house_id=? ORDER BY id DESC LIMIT 1",
            (hid,)
        ).fetchone()
        cur_pct = round(prog["percent"] if prog else 0, 1)

        total_purchases = sum((p["price"] or 0) for p in purchases)
        total_payments  = sum((p["amount"] or 0) for p in payments)
        engineer_amount = (total_purchases + total_payments) * (h["engineer_percent"] or 0) / 100.0
        pending_requests = sum(1 for r in requests_ if r["status"] == "pending")

        # workers folders
        workers = []
        owners = []
        for w in users:
            if w["role"] == "worker":
                paid = sum(p["amount"] for p in payments if p["worker_id"] == w["id"])
                bought = sum((p["price"] or 0) for p in purchases if p["worker_id"] == w["id"])
                workers.append(dict(
                    id=w["id"], name=w["name"] or w["username"],
                    username=w["username"], job=w["job"],
                    paid=paid, bought=bought,
                ))
            elif w["role"] == "owner":
                owners.append(w)

        # categories folders
        cat_map = {}
        for p in purchases:
            name = (p["category"] or "أخرى").strip() or "أخرى"
            d = cat_map.setdefault(name, {"name": name, "total": 0.0, "count": 0})
            d["total"] += (p["price"] or 0)
            d["count"] += 1
        categories = sorted(cat_map.values(), key=lambda x: x["total"], reverse=True)

        # legacy worker_summary (kept for any template still using it)
        worker_summary = [dict(name=w["name"], job=w["job"], paid=w["paid"], bought=w["bought"])
                          for w in workers]

    return dict(
        h=h, users=users, owners=owners, workers=workers, categories=categories,
        purchases=purchases, payments=payments,
        requests=requests_, current_percent=cur_pct,
        total_purchases=total_purchases, total_payments=total_payments,
        engineer_amount=engineer_amount, pending_requests=pending_requests,
        worker_summary=worker_summary,
    )


@app.route("/admin/house/<int:hid>")
@login_required(roles=["admin"])
def admin_house(u, hid):
    ctx = _gather_house_context(hid)
    if not ctx:
        abort(404)
    body = render_template_string(HOUSE_TPL, fm=fmt_money, **ctx)
    return page(body, ctx["h"]["name"], user=u)


@app.route("/admin/house/<int:hid>/worker/<int:uid>")
@login_required(roles=["admin"])
def admin_worker_file(u, hid, uid):
    h = get_house(hid)
    if not h: abort(404)
    with db() as c:
        w = c.execute("SELECT * FROM users WHERE id=? AND house_id=? AND role='worker'",
                      (uid, hid)).fetchone()
        if not w: abort(404)
        payments = c.execute(
            "SELECT * FROM payments WHERE house_id=? AND worker_id=? ORDER BY id DESC",
            (hid, uid)
        ).fetchall()
        purchases = c.execute(
            "SELECT * FROM purchases WHERE house_id=? AND worker_id=? ORDER BY id DESC",
            (hid, uid)
        ).fetchall()
    paid_total = sum(p["amount"] for p in payments)
    bought_total = sum((p["price"] or 0) for p in purchases)
    body = render_template_string(
        WORKER_FILE_TPL, h=h, w=w, payments=payments, purchases=purchases,
        paid_total=paid_total, bought_total=bought_total,
        back_url=url_for("admin_house", hid=hid), can_chat=True, fm=fmt_money,
    )
    return page(body, w["name"] or w["username"], user=u)


@app.route("/admin/house/<int:hid>/category/<path:name>")
@login_required(roles=["admin"])
def admin_category_file(u, hid, name):
    h = get_house(hid)
    if not h: abort(404)
    with db() as c:
        # match exact category, or "أخرى" fallback for null/empty
        if name == "أخرى":
            purchases = c.execute("""
                SELECT p.*, u.name AS worker_name FROM purchases p
                LEFT JOIN users u ON u.id=p.worker_id
                WHERE p.house_id=? AND (p.category IS NULL OR p.category='' OR p.category='أخرى')
                ORDER BY p.id DESC""", (hid,)).fetchall()
        else:
            purchases = c.execute("""
                SELECT p.*, u.name AS worker_name FROM purchases p
                LEFT JOIN users u ON u.id=p.worker_id
                WHERE p.house_id=? AND p.category=? ORDER BY p.id DESC""",
                (hid, name)).fetchall()
    if not purchases: abort(404)
    total = sum((p["price"] or 0) for p in purchases)
    body = render_template_string(
        CATEGORY_FILE_TPL, h=h, name=name, purchases=purchases, total=total,
        back_url=url_for("admin_house", hid=hid), fm=fmt_money,
    )
    return page(body, name, user=u)


HOUSE_EDIT_TPL = """
<div class="card">
  <h2>تعديل البيت</h2>
  <form method="post">
    <label>الاسم</label><input name="name" value="{{ h['name'] }}" required>
    <label>العنوان</label><input name="address" value="{{ h['address'] or '' }}">
    <label>نسبة المهندس %</label><input name="engineer_percent" type="number" step="0.01" value="{{ h['engineer_percent'] }}">
    <label>ملاحظات</label><textarea name="notes" rows="2">{{ h['notes'] or '' }}</textarea>
    <div style="margin-top:.8rem"><button class="btn">حفظ</button>
      <a href="{{ url_for('admin_house', hid=h['id']) }}">إلغاء</a></div>
  </form>
</div>
"""


@app.route("/admin/house/<int:hid>/edit", methods=["GET", "POST"])
@login_required(roles=["admin"])
def admin_house_edit(u, hid):
    h = get_house(hid)
    if not h:
        abort(404)
    if request.method == "POST":
        f = request.form
        with db() as c:
            c.execute("""UPDATE houses SET name=?,address=?,engineer_percent=?,notes=? WHERE id=?""",
                      (f.get("name"), f.get("address"),
                       float(f.get("engineer_percent") or 0),
                       f.get("notes"), hid))
        flash("تم التعديل")
        return redirect(url_for("admin_house", hid=hid))
    body = render_template_string(HOUSE_EDIT_TPL, h=h)
    return page(body, "تعديل بيت", user=u)


USER_NEW_TPL = """
<div class="card">
  <h2>إضافة مستخدم لبيت {{ h['name'] }}</h2>
  <form method="post">
    <label>الدور</label>
    <select name="role" required>
      <option value="worker">كادر</option>
      <option value="owner">صاحب البيت</option>
    </select>
    <label>الاسم</label><input name="name" required>
    <label>المهنة (للكادر)</label><input name="job" placeholder="مثلاً: سيراميك، أبواب">
    <label>اسم المستخدم</label><input name="username" required>
    <label>كلمة المرور / الرمز</label><input name="password" required>
    <div style="margin-top:.8rem"><button class="btn">حفظ</button>
      <a href="{{ url_for('admin_house', hid=h['id']) }}">إلغاء</a></div>
  </form>
  <p class="muted">ملاحظة: لكل بيت أصحاب وكوادر مستقلون. سيقدر المستخدم الدخول من نفس صفحة الدخول الرئيسية.</p>
</div>
"""


@app.route("/admin/house/<int:hid>/user/new", methods=["GET", "POST"])
@login_required(roles=["admin"])
def admin_house_user_new(u, hid):
    h = get_house(hid)
    if not h:
        abort(404)
    if h["status"] != "active":
        flash("البيت مغلق", "err")
        return redirect(url_for("admin_house", hid=hid))
    if request.method == "POST":
        f = request.form
        role = f.get("role")
        if role not in ("worker", "owner"):
            flash("دور غير صالح", "err")
            return redirect(request.url)
        try:
            with db() as c:
                if role == "owner":
                    existing = c.execute(
                        "SELECT id FROM users WHERE house_id=? AND role='owner'",
                        (hid,)
                    ).fetchone()
                    if existing:
                        flash("يوجد صاحب بيت مسجل، احذفه أولاً.", "err")
                        return redirect(request.url)
                c.execute("""INSERT INTO users(username,password,role,name,house_id,job)
                             VALUES (?,?,?,?,?,?)""",
                          (f.get("username", "").strip(),
                           hash_pw(f.get("password", "")),
                           role,
                           f.get("name", "").strip(),
                           hid,
                           f.get("job", "").strip()))
            flash("تم إضافة المستخدم")
            return redirect(url_for("admin_house", hid=hid))
        except sqlite3.IntegrityError:
            flash("اسم المستخدم مستخدم سابقاً", "err")
    body = render_template_string(USER_NEW_TPL, h=h)
    return page(body, "إضافة مستخدم", user=u)


@app.route("/admin/house/<int:hid>/user/<int:uid>/delete")
@login_required(roles=["admin"])
def admin_user_delete(u, hid, uid):
    with db() as c:
        c.execute("DELETE FROM users WHERE id=? AND house_id=?", (uid, hid))
    flash("تم الحذف")
    return redirect(url_for("admin_house", hid=hid))


PURCHASE_NEW_TPL = """
<div class="card">
  <h2>إضافة مشترى لبيت {{ h['name'] }}</h2>
  <form method="post" enctype="multipart/form-data">
    <label>الفئة (مثلاً: سيراميك، تكعيب، كهرباء…)</label>
    <input name="category" list="cats" required placeholder="اكتب اسم الفئة">
    <datalist id="cats">
      {% for c in categories %}<option value="{{ c }}">{% endfor %}
    </datalist>
    <label>الصنف / الوصف</label>
    <input name="item" required placeholder="مثلاً: سيراميك أرضيات صالة">
    <label>المبلغ الإجمالي</label>
    <input name="total" type="number" step="0.01" required placeholder="المجموع كلياً">
    <label>التاجر / المصدر (اختياري)</label><input name="vendor">
    <label>الكادر المرتبط (اختياري)</label>
    <select name="worker_id">
      <option value="">— لا أحد —</option>
      {% for w in workers %}<option value="{{ w['id'] }}">{{ w['name'] or w['username'] }} {% if w['job'] %}({{ w['job'] }}){% endif %}</option>{% endfor %}
    </select>
    <label>صورة الفاتورة (اختياري)</label>
    <input type="file" name="receipt" accept="image/*">
    <label>ملاحظات (اختياري)</label><textarea name="notes" rows="2"></textarea>
    <div style="margin-top:.9rem;display:flex;gap:.5rem">
      <button class="btn">حفظ</button>
      <a class="btn btn-ghost" href="{{ url_for('admin_house', hid=h['id']) }}">إلغاء</a>
    </div>
  </form>
</div>
"""


@app.route("/admin/house/<int:hid>/purchase/new", methods=["GET", "POST"])
@login_required(roles=["admin"])
def admin_purchase_new(u, hid):
    h = get_house(hid)
    if not h:
        abort(404)
    if h["status"] != "active":
        flash("البيت مغلق", "err"); return redirect(url_for("admin_house", hid=hid))
    with db() as c:
        workers = c.execute(
            "SELECT * FROM users WHERE house_id=? AND role='worker'", (hid,)
        ).fetchall()
        cats = [r["category"] for r in c.execute(
            "SELECT DISTINCT category FROM purchases WHERE house_id=? AND category IS NOT NULL AND category!=''",
            (hid,)
        ).fetchall()]
    if request.method == "POST":
        f = request.form
        wid = f.get("worker_id") or None
        total = float(f.get("total") or 0)
        receipt = save_upload(request.files.get("receipt"))
        with db() as c:
            c.execute("""INSERT INTO purchases
                         (house_id,category,item,qty,price,vendor,notes,worker_id,receipt_image)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (hid, f.get("category"), f.get("item"),
                       1, total, f.get("vendor"), f.get("notes"),
                       int(wid) if wid else None, receipt))
        flash("تم إضافة المشترى")
        return redirect(url_for("admin_house", hid=hid))
    body = render_template_string(PURCHASE_NEW_TPL, h=h, workers=workers, categories=cats)
    return page(body, "مشترى", user=u)


PAYMENT_NEW_TPL = """
<div class="card">
  <h2>تسجيل دفعة لكادر — بيت {{ h['name'] }}</h2>
  <form method="post" enctype="multipart/form-data">
    <label>الكادر</label>
    <select name="worker_id" required>
      {% for w in workers %}<option value="{{ w['id'] }}">{{ w['name'] or w['username'] }} {% if w['job'] %}({{ w['job'] }}){% endif %}</option>{% endfor %}
    </select>
    <label>المبلغ</label><input name="amount" type="number" step="0.01" required>
    <label>صورة الإيصال (اختياري)</label>
    <input type="file" name="receipt" accept="image/*">
    <label>ملاحظات (اختياري)</label><textarea name="notes" rows="2"></textarea>
    <div style="margin-top:.9rem;display:flex;gap:.5rem">
      <button class="btn">حفظ</button>
      <a class="btn btn-ghost" href="{{ url_for('admin_house', hid=h['id']) }}">إلغاء</a>
    </div>
  </form>
</div>
"""


@app.route("/admin/house/<int:hid>/payment/new", methods=["GET", "POST"])
@login_required(roles=["admin"])
def admin_payment_new(u, hid):
    h = get_house(hid)
    if not h:
        abort(404)
    if h["status"] != "active":
        flash("البيت مغلق", "err"); return redirect(url_for("admin_house", hid=hid))
    with db() as c:
        workers = c.execute(
            "SELECT * FROM users WHERE house_id=? AND role='worker'", (hid,)
        ).fetchall()
    if not workers:
        flash("أضف كادراً أولاً قبل تسجيل دفعة", "err")
        return redirect(url_for("admin_house", hid=hid))
    if request.method == "POST":
        f = request.form
        receipt = save_upload(request.files.get("receipt"))
        with db() as c:
            c.execute("""INSERT INTO payments(house_id,worker_id,amount,notes,receipt_image)
                         VALUES (?,?,?,?,?)""",
                      (hid, int(f.get("worker_id")),
                       float(f.get("amount") or 0),
                       f.get("notes"), receipt))
        flash("تم تسجيل الدفعة")
        return redirect(url_for("admin_house", hid=hid))
    body = render_template_string(PAYMENT_NEW_TPL, h=h, workers=workers)
    return page(body, "دفعة", user=u)


@app.route("/admin/house/<int:hid>/progress", methods=["POST"])
@login_required(roles=["admin"])
def admin_house_progress(u, hid):
    h = get_house(hid)
    if not h: abort(404)
    if h["status"] != "active":
        flash("البيت مغلق", "err"); return redirect(url_for("admin_house", hid=hid))
    pct = float(request.form.get("percent") or 0)
    pct = max(0.0, min(100.0, pct))
    notes = request.form.get("notes", "")
    with db() as c:
        c.execute("INSERT INTO progress(house_id,percent,notes) VALUES (?,?,?)",
                  (hid, pct, notes))
    flash("تم تحديث نسبة الإنجاز")
    return redirect(url_for("admin_house", hid=hid))


@app.route("/admin/house/<int:hid>/request/<int:rid>/decide", methods=["POST"])
@login_required(roles=["admin"])
def admin_request_decide(u, hid, rid):
    decision = request.form.get("decision")
    note = request.form.get("note", "")
    if decision not in ("approve", "reject"):
        abort(400)
    status = "approved" if decision == "approve" else "rejected"
    with db() as c:
        c.execute("""UPDATE money_requests
                     SET status=?, admin_note=?, decided_at=?
                     WHERE id=? AND house_id=?""",
                  (status, note, now_str(), rid, hid))
        if status == "approved":
            r = c.execute("SELECT * FROM money_requests WHERE id=?", (rid,)).fetchone()
            if r:
                c.execute("""INSERT INTO payments(house_id,worker_id,amount,notes)
                             VALUES (?,?,?,?)""",
                          (hid, r["worker_id"], r["amount"],
                           f"طلب رقم {rid} - {note}".strip()))
    flash("تم حفظ القرار")
    return redirect(url_for("admin_house", hid=hid))


# ---- chat ----
CHAT_TPL = """
<div class="hero">
  <div class="flex-between" style="align-items:flex-start">
    <div>
      <h1>{{ worker['name'] or worker['username'] }}</h1>
      <div class="sub">شات بيت {{ h['name'] }}</div>
    </div>
    <a class="btn btn-light btn-sm" href="{{ back_url }}">رجوع</a>
  </div>
</div>

<div class="card">
  <div class="chat" id="chat">
    {% for m in messages %}
      <div class="msg {{ 'me' if m['from_role']==my_role else 'them' }}">
        <div class="who">{{ 'أنا' if m['from_role']==my_role else ('الإدارة' if m['from_role']=='admin' else (worker['name'] or worker['username'])) }} · {{ m['created_at'][:16] }}</div>
        {% if m['text'] %}<div>{{ m['text'] }}</div>{% endif %}
        {% if m['image_path'] %}<a href="{{ url_for('uploaded_file', filename=m['image_path']) }}" target="_blank"><img src="{{ url_for('uploaded_file', filename=m['image_path']) }}"></a>{% endif %}
      </div>
    {% endfor %}
    {% if not messages %}<div class="empty"><p>ابدأ المحادثة الآن.</p></div>{% endif %}
  </div>
  <hr>
  <form method="post" enctype="multipart/form-data">
    <textarea name="text" rows="2" placeholder="اكتب رسالة..."></textarea>
    <label>إرفاق صورة (اختياري)</label>
    <input type="file" name="image" accept="image/*">
    <div style="margin-top:.7rem"><button class="btn btn-block">إرسال</button></div>
  </form>
</div>
<script>
  var c=document.getElementById('chat'); if(c){c.scrollTop=c.scrollHeight;}
</script>
"""


def _load_chat(hid, wid):
    with db() as c:
        return c.execute("""SELECT * FROM messages WHERE house_id=? AND worker_id=?
                            ORDER BY id ASC""", (hid, wid)).fetchall()


@app.route("/admin/house/<int:hid>/chat/<int:wid>", methods=["GET", "POST"])
@login_required(roles=["admin"])
def admin_chat(u, hid, wid):
    h = get_house(hid)
    with db() as c:
        worker = c.execute("SELECT * FROM users WHERE id=? AND house_id=? AND role='worker'",
                           (wid, hid)).fetchone()
    if not h or not worker: abort(404)
    if request.method == "POST":
        text = request.form.get("text", "").strip()
        img = save_upload(request.files.get("image"))
        if text or img:
            with db() as c:
                c.execute("""INSERT INTO messages(house_id,worker_id,from_role,text,image_path)
                             VALUES (?,?,?,?,?)""", (hid, wid, "admin", text, img))
        return redirect(url_for("admin_chat", hid=hid, wid=wid))
    msgs = _load_chat(hid, wid)
    body = render_template_string(CHAT_TPL, h=h, worker=worker, messages=msgs,
                                  my_role="admin",
                                  back_url=url_for("admin_house", hid=hid))
    return page(body, "شات", user=u)


# ---- close house ----
@app.route("/admin/house/<int:hid>/close")
@login_required(roles=["admin"])
def admin_house_close(u, hid):
    h = get_house(hid)
    if not h: abort(404)
    if h["status"] == "closed":
        flash("البيت مغلق سابقاً")
        return redirect(url_for("house_pdf", hid=hid))
    with db() as c:
        c.execute("UPDATE houses SET status='closed', closed_at=? WHERE id=?",
                  (now_str(), hid))
    flash("تم إغلاق البيت. يمكنك الآن تحميل الفاتورة.")
    return redirect(url_for("house_pdf", hid=hid))


# =========================================================
#                     WORKER VIEWS
# =========================================================
WORKER_HOME_TPL = """
<div class="hero">
  <h1>أهلاً، {{ user['name'] or user['username'] }}</h1>
  <div class="sub">{{ user['job'] or 'كادر' }} · بيت {{ h['name'] }}
    {% if h['status']=='closed' %} · <span class="badge" style="background:rgba(255,255,255,.22);color:#fff">مغلق</span>{% endif %}
  </div>
  <div class="actions">
    <a class="btn btn-light btn-sm" href="{{ url_for('worker_chat') }}">فتح الشات</a>
  </div>
</div>

<div class="card">
  <div class="kpi">
    <div class="box accent"><div class="l">إجمالي المستلم</div><div class="v">{{ fm(total_paid) }}</div></div>
    <div class="box"><div class="l">إجمالي مشترياتي</div><div class="v">{{ fm(total_bought) }}</div></div>
    <div class="box"><div class="l">طلبات معلقة</div><div class="v">{{ pending }}</div></div>
  </div>
</div>

{% if h['status']=='active' %}
<div class="card">
  <h3>طلب فلوس من الإدارة</h3>
  <form method="post" action="{{ url_for('worker_request_new') }}">
    <label>المبلغ</label>
    <input name="amount" type="number" step="0.01" required placeholder="مثلاً 250000">
    <label>السبب</label>
    <textarea name="reason" rows="2" required placeholder="مثلاً: شراء سيراميك"></textarea>
    <div style="margin-top:.8rem"><button class="btn btn-block">إرسال الطلب</button></div>
  </form>
</div>
{% endif %}

<div class="card">
  <h3>طلباتي</h3>
  {% if requests %}
    {% for r in requests %}
      <div class="receipt {{ 'payment' if r['status']=='approved' else '' }}">
        <span class="rcicon">""" + ICONS["request"] + """</span>
        <div class="rcbody">
          <div class="rctitle">{{ r['reason'] or 'طلب' }}</div>
          <div class="rcmeta">
            <span>{{ r['created_at'][:16] }}</span>
            <span class="dot">•</span>
            {% if r['status']=='pending' %}<span class="badge amber">قيد الانتظار</span>
            {% elif r['status']=='approved' %}<span class="badge green">موافق</span>
            {% else %}<span class="badge red">مرفوض</span>{% endif %}
          </div>
          {% if r['admin_note'] %}<div class="muted" style="margin-top:.3rem">ملاحظة الإدارة: {{ r['admin_note'] }}</div>{% endif %}
        </div>
        <div class="rcamt">{{ fm(r['amount']) }}</div>
      </div>
    {% endfor %}
  {% else %}<div class="empty"><div class="ico">""" + ICONS["request"] + """</div><p>لا توجد طلبات بعد.</p></div>{% endif %}
</div>

<div class="card">
  <h3>دفعاتي المستلمة</h3>
  {% if payments %}
    {% for p in payments %}
      <div class="receipt payment">
        <span class="rcicon">""" + ICONS["money"] + """</span>
        <div class="rcbody">
          <div class="rctitle">{{ p['notes'] or 'دفعة' }}</div>
          <div class="rcmeta"><span>{{ p['created_at'][:16] }}</span></div>
        </div>
        <div class="rcamt">{{ fm(p['amount']) }}</div>
      </div>
    {% endfor %}
  {% else %}<div class="empty"><div class="ico">""" + ICONS["money"] + """</div><p>لم تستلم أي دفعة بعد.</p></div>{% endif %}
</div>
<style>.empty .ico svg{width:60px;height:60px;stroke:var(--text-3);stroke-width:1.6;fill:none}
.iconbox svg,.rcicon svg{width:22px;height:22px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}</style>
"""


@app.route("/worker")
@login_required(roles=["worker"])
def worker_home(u):
    if not u["house_id"]: abort(403)
    h = get_house(u["house_id"])
    with db() as c:
        payments = c.execute("""SELECT * FROM payments WHERE house_id=? AND worker_id=?
                                ORDER BY id DESC""", (h["id"], u["id"])).fetchall()
        purchases = c.execute("""SELECT * FROM purchases WHERE house_id=? AND worker_id=?""",
                              (h["id"], u["id"])).fetchall()
        reqs = c.execute("""SELECT * FROM money_requests WHERE house_id=? AND worker_id=?
                            ORDER BY id DESC""", (h["id"], u["id"])).fetchall()
    total_paid = sum(p["amount"] for p in payments)
    total_bought = sum((p["price"] or 0) for p in purchases)
    pending = sum(1 for r in reqs if r["status"] == "pending")
    body = render_template_string(WORKER_HOME_TPL, user=u, h=h,
                                  payments=payments, requests=reqs,
                                  total_paid=total_paid, total_bought=total_bought,
                                  pending=pending, fm=fmt_money)
    return page(body, "كادر - cubeq", user=u, active="home")


@app.route("/worker/request/new", methods=["POST"])
@login_required(roles=["worker"])
def worker_request_new(u):
    h = get_house(u["house_id"])
    if not h or h["status"] != "active":
        flash("البيت مغلق", "err"); return redirect(url_for("worker_home"))
    amount = float(request.form.get("amount") or 0)
    reason = request.form.get("reason", "").strip()
    if amount <= 0:
        flash("أدخل مبلغاً صحيحاً", "err"); return redirect(url_for("worker_home"))
    with db() as c:
        c.execute("""INSERT INTO money_requests(house_id,worker_id,amount,reason)
                     VALUES (?,?,?,?)""", (h["id"], u["id"], amount, reason))
    flash("تم إرسال الطلب للإدارة")
    return redirect(url_for("worker_home"))


@app.route("/worker/chat", methods=["GET", "POST"])
@login_required(roles=["worker"])
def worker_chat(u):
    h = get_house(u["house_id"])
    if not h: abort(403)
    if request.method == "POST":
        text = request.form.get("text", "").strip()
        img = save_upload(request.files.get("image"))
        if text or img:
            with db() as c:
                c.execute("""INSERT INTO messages(house_id,worker_id,from_role,text,image_path)
                             VALUES (?,?,?,?,?)""", (h["id"], u["id"], "worker", text, img))
        return redirect(url_for("worker_chat"))
    msgs = _load_chat(h["id"], u["id"])
    body = render_template_string(CHAT_TPL, h=h, worker=u, messages=msgs,
                                  my_role="worker",
                                  back_url=url_for("worker_home"))
    return page(body, "شات", user=u, active="chat")


# =========================================================
#                     OWNER VIEW
# =========================================================
OWNER_TPL = FOLDER_CSS + """
<div class="hero">
  <h1>بيت {{ h['name'] }}</h1>
  <div class="sub">{{ h['address'] or 'بدون عنوان' }}
    {% if h['status']=='closed' %} · <span class="badge" style="background:rgba(255,255,255,.22);color:#fff">مغلق</span>{% endif %}
  </div>
  <div class="actions">
    <a class="btn btn-light btn-sm" href="{{ url_for('owner_receipts') }}">عرض كل الفواتير ({{ receipts_count }})</a>
    {% if h['status']=='closed' %}
      <a class="btn btn-light btn-sm" href="{{ url_for('house_pdf', hid=h['id']) }}">الفاتورة النهائية PDF</a>
    {% endif %}
  </div>
</div>

<div class="card">
  <div class="kpi">
    <div class="box accent"><div class="l">إجمالي الصرف</div><div class="v">{{ fm(total_purchases + total_payments) }}</div></div>
    <div class="box"><div class="l">دفعات الكوادر</div><div class="v">{{ fm(total_payments) }}</div></div>
    <div class="box"><div class="l">المشتريات</div><div class="v">{{ fm(total_purchases) }}</div></div>
    <div class="box"><div class="l">قيمة المهندس ({{ h['engineer_percent'] }}%)</div><div class="v">{{ fm(engineer_amount) }}</div></div>
  </div>
  <hr>
  <label>نسبة الإنجاز: {{ percent }}%</label>
  <div class="progress"><div style="width:{{ percent }}%"></div></div>
</div>

<!-- ===== ملفات الكوادر ===== -->
<div class="folders-section">
  <div class="head">
    <h3><span class="ico">""" + ICONS["tools"] + """</span> ملفات الكوادر</h3>
    <span class="muted" style="font-size:.85rem">{{ workers|length }} كادر</span>
  </div>
  {% if workers %}
  <div class="folders-grid">
    {% for w in workers %}
    <a class="folder worker" href="{{ url_for('owner_worker_file', uid=w['id']) }}">
      <div class="tab"></div>
      <div class="ico">""" + ICONS["user"] + """</div>
      <div class="name">{{ w['name'] }}</div>
      <div class="sub">{{ w['job'] or 'بدون مهنة' }}</div>
      <div class="row"><span>المستلم</span><span class="amt">{{ fm(w['paid']) }}</span></div>
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty-mini">لم يتم تعيين كوادر بعد.</div>
  {% endif %}
</div>

<!-- ===== ملفات الأغراض / الفئات ===== -->
<div class="folders-section">
  <div class="head">
    <h3><span class="ico">""" + ICONS["package"] + """</span> ملفات الأغراض</h3>
    <span class="muted" style="font-size:.85rem">{{ categories|length }} ملف</span>
  </div>
  {% if categories %}
  <div class="folders-grid">
    {% for c in categories %}
    <a class="folder cat" href="{{ url_for('owner_category_file', name=c['name']) }}">
      <div class="tab"></div>
      <div class="ico">""" + ICONS["package"] + """</div>
      <div class="name">{{ c['name'] }}</div>
      <div class="sub">{{ c['count'] }} عملية</div>
      <div class="row"><span>الإجمالي</span><span class="amt">{{ fm(c['total']) }}</span></div>
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty-mini">لا توجد مشتريات بعد.</div>
  {% endif %}
</div>

<div class="card" style="margin-top:1rem">
  <div class="head" style="display:flex;justify-content:space-between;align-items:center">
    <h3 style="margin:0">أحدث الفواتير</h3>
    <a class="btn btn-ghost btn-sm" href="{{ url_for('owner_receipts') }}">عرض الكل</a>
  </div>
  {% if receipts %}
    {% for r in receipts %}
      <div class="receipt {{ r.kind }}">
        <span class="rcicon">{{ ICONS_MONEY|safe if r.kind=='payment' else ICONS_PACKAGE|safe }}</span>
        <div class="rcbody">
          <div class="rctitle">{{ r.title }}</div>
          <div class="rcmeta">
            <span>{{ r.date }}</span>
            <span class="dot">•</span>
            <span class="badge {{ 'blue' if r.kind=='payment' else 'green' }}">{{ 'دفعة لكادر' if r.kind=='payment' else 'مشتريات' }}</span>
            {% if r.who %}<span class="dot">•</span><span>{{ r.who }}</span>{% endif %}
            {% if r.image %}<span class="dot">•</span><a href="{{ url_for('uploaded_file', filename=r.image) }}" target="_blank">عرض الفاتورة</a>{% endif %}
          </div>
        </div>
        <div class="rcamt">{{ fm(r.amount) }}</div>
      </div>
    {% endfor %}
  {% else %}
    <div class="empty"><div class="ico">""" + ICONS["receipt"] + """</div><p>لا توجد فواتير بعد. ستظهر هنا تلقائياً عند كل عملية صرف.</p></div>
  {% endif %}
</div>
<style>.empty .ico svg{width:60px;height:60px;stroke:var(--text-3);stroke-width:1.6;fill:none}
.iconbox svg,.rcicon svg{width:22px;height:22px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}</style>
"""


OWNER_RECEIPTS_TPL = """
<div class="hero">
  <h1>الفواتير</h1>
  <div class="sub">كل دفعة صرفت من الإدارة وكل مشترى تم لبيتك</div>
  <div class="actions">
    <a class="btn btn-light btn-sm" href="{{ url_for('owner_home') }}">رجوع</a>
  </div>
</div>

<div class="card">
  <div class="kpi">
    <div class="box accent"><div class="l">عدد الفواتير</div><div class="v">{{ receipts|length }}</div></div>
    <div class="box"><div class="l">إجمالي الصرف</div><div class="v">{{ fm(total) }}</div></div>
  </div>
</div>

{% if receipts %}
<div class="card">
  {% for r in receipts %}
    <div class="receipt {{ r.kind }}">
      <span class="rcicon">{{ ICONS_MONEY|safe if r.kind=='payment' else ICONS_PACKAGE|safe }}</span>
      <div class="rcbody">
        <div class="rctitle">{{ r.title }}</div>
        <div class="rcmeta">
          <span>{{ r.date }}</span>
          <span class="dot">•</span>
          <span class="badge {{ 'blue' if r.kind=='payment' else 'green' }}">{{ 'دفعة لكادر' if r.kind=='payment' else 'مشتريات' }}</span>
          {% if r.who %}<span class="dot">•</span><span>{{ r.who }}</span>{% endif %}
          {% if r.detail %}<span class="dot">•</span><span>{{ r.detail }}</span>{% endif %}
        </div>
      </div>
      <div class="rcamt">{{ fm(r.amount) }}</div>
    </div>
  {% endfor %}
</div>
{% else %}
<div class="card">
  <div class="empty">
    <div class="ico">""" + ICONS["receipt"] + """</div>
    <p>لا توجد فواتير بعد.</p>
    <p class="muted">عند كل دفعة لكادر أو شراء، تظهر الفاتورة هنا تلقائياً.</p>
  </div>
</div>
{% endif %}
<style>.empty .ico svg{width:60px;height:60px;stroke:var(--text-3);stroke-width:1.6;fill:none}
.rcicon svg{width:22px;height:22px;stroke:currentColor;stroke-width:2;fill:none;stroke-linecap:round;stroke-linejoin:round}</style>
"""


def _build_receipts(ctx):
    """Combine purchases + payments into a unified, date-sorted receipt feed."""
    items = []
    for p in ctx["purchases"]:
        items.append({
            "kind": "purchase",
            "date": (p["created_at"] or "")[:16],
            "title": p["item"] or "مشترى",
            "amount": (p["price"] or 0),
            "who": p["category"] or "",
            "detail": "",
            "image": p["receipt_image"],
            "_sort": p["created_at"] or "",
        })
    for p in ctx["payments"]:
        items.append({
            "kind": "payment",
            "date": (p["created_at"] or "")[:16],
            "title": p["notes"] or "دفعة لكادر",
            "amount": p["amount"],
            "who": p["worker_name"] or "",
            "detail": "",
            "image": p["receipt_image"],
            "_sort": p["created_at"] or "",
        })
    items.sort(key=lambda x: x["_sort"], reverse=True)
    return items


@app.route("/owner")
@login_required(roles=["owner"])
def owner_home(u):
    if not u["house_id"]: abort(403)
    ctx = _gather_house_context(u["house_id"])
    if not ctx: abort(404)
    receipts = _build_receipts(ctx)
    body = render_template_string(
        OWNER_TPL,
        h=ctx["h"],
        percent=ctx["current_percent"],
        total_purchases=ctx["total_purchases"],
        total_payments=ctx["total_payments"],
        engineer_amount=ctx["engineer_amount"],
        workers=ctx["workers"],
        categories=ctx["categories"],
        receipts=receipts[:5],
        receipts_count=len(receipts),
        ICONS_MONEY=ICONS["money"],
        ICONS_PACKAGE=ICONS["package"],
        fm=fmt_money,
    )
    return page(body, "صاحب البيت", user=u, active="home")


@app.route("/owner/receipts")
@login_required(roles=["owner"])
def owner_receipts(u):
    if not u["house_id"]: abort(403)
    ctx = _gather_house_context(u["house_id"])
    if not ctx: abort(404)
    receipts = _build_receipts(ctx)
    total = sum(r["amount"] for r in receipts)
    body = render_template_string(
        OWNER_RECEIPTS_TPL,
        receipts=receipts,
        total=total,
        ICONS_MONEY=ICONS["money"],
        ICONS_PACKAGE=ICONS["package"],
        fm=fmt_money,
    )
    return page(body, "الفواتير", user=u, active="receipts")


@app.route("/owner/worker/<int:uid>")
@login_required(roles=["owner"])
def owner_worker_file(u, uid):
    if not u["house_id"]: abort(403)
    hid = u["house_id"]
    h = get_house(hid)
    if not h: abort(404)
    with db() as c:
        w = c.execute("SELECT * FROM users WHERE id=? AND house_id=? AND role='worker'",
                      (uid, hid)).fetchone()
        if not w: abort(404)
        payments = c.execute(
            "SELECT * FROM payments WHERE house_id=? AND worker_id=? ORDER BY id DESC",
            (hid, uid)
        ).fetchall()
        purchases = c.execute(
            "SELECT * FROM purchases WHERE house_id=? AND worker_id=? ORDER BY id DESC",
            (hid, uid)
        ).fetchall()
    paid_total = sum(p["amount"] for p in payments)
    bought_total = sum((p["price"] or 0) for p in purchases)
    body = render_template_string(
        WORKER_FILE_TPL, h=h, w=w, payments=payments, purchases=purchases,
        paid_total=paid_total, bought_total=bought_total,
        back_url=url_for("owner_home"), can_chat=False, fm=fmt_money,
    )
    return page(body, w["name"] or w["username"], user=u, active="home")


@app.route("/owner/category/<path:name>")
@login_required(roles=["owner"])
def owner_category_file(u, name):
    if not u["house_id"]: abort(403)
    hid = u["house_id"]
    h = get_house(hid)
    if not h: abort(404)
    with db() as c:
        if name == "أخرى":
            purchases = c.execute("""
                SELECT p.*, u.name AS worker_name FROM purchases p
                LEFT JOIN users u ON u.id=p.worker_id
                WHERE p.house_id=? AND (p.category IS NULL OR p.category='' OR p.category='أخرى')
                ORDER BY p.id DESC""", (hid,)).fetchall()
        else:
            purchases = c.execute("""
                SELECT p.*, u.name AS worker_name FROM purchases p
                LEFT JOIN users u ON u.id=p.worker_id
                WHERE p.house_id=? AND p.category=? ORDER BY p.id DESC""",
                (hid, name)).fetchall()
    if not purchases: abort(404)
    total = sum((p["price"] or 0) for p in purchases)
    body = render_template_string(
        CATEGORY_FILE_TPL, h=h, name=name, purchases=purchases, total=total,
        back_url=url_for("owner_home"), fm=fmt_money,
    )
    return page(body, name, user=u, active="home")


# =========================================================
#                       UPLOADS
# =========================================================
@app.route("/uploads/<path:filename>")
@login_required()
def uploaded_file(u, filename):
    return send_from_directory(UPLOAD_DIR, filename)


# =========================================================
#                       PDF REPORT
# =========================================================
@app.route("/house/<int:hid>/report.pdf")
@login_required()
def house_pdf(u, hid):
    # admins always allowed; owners/workers only if same house
    if u["role"] != "admin" and u["house_id"] != hid:
        abort(403)
    ctx = _gather_house_context(hid)
    if not ctx: abort(404)
    h = ctx["h"]

    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)
    W, H = A4
    margin = 15 * mm
    y = H - margin

    def line(text, size=11, bold=False, dy=14):
        nonlocal y
        if y < margin + 30:
            c.showPage(); y = H - margin
        c.setFont(PDF_FONT, size)
        # right aligned for arabic
        s = ar(text)
        c.drawRightString(W - margin, y, s)
        y -= dy

    def hr():
        nonlocal y
        c.setStrokeColorRGB(0.7, 0.7, 0.7)
        c.line(margin, y, W - margin, y); y -= 8

    def header_row(cols, widths, size=10, bold=True):
        nonlocal y
        if y < margin + 30:
            c.showPage(); y = H - margin
        c.setFont(PDF_FONT, size)
        x = W - margin
        for txt, w in zip(cols, widths):
            x -= w
            c.drawString(x, y, ar(txt))
        y -= 14

    def data_row(cols, widths, size=10):
        nonlocal y
        if y < margin + 25:
            c.showPage(); y = H - margin
        c.setFont(PDF_FONT, size)
        x = W - margin
        for txt, w in zip(cols, widths):
            x -= w
            c.drawString(x, y, ar(str(txt)))
        y -= 12

    line("cubeq — فاتورة نهائية", 18, dy=24)
    line(f"البيت: {h['name']}", 13, dy=18)
    if h["address"]:
        line(f"العنوان: {h['address']}", 11)
    line(f"الحالة: {'مغلق' if h['status']=='closed' else 'نشط'}", 11)
    line(f"تاريخ الإنشاء: {(h['created_at'] or '')[:10]}", 11)
    if h["closed_at"]:
        line(f"تاريخ الإغلاق: {h['closed_at'][:10]}", 11)
    line(f"نسبة الإنجاز: {ctx['current_percent']}%", 11)
    line(f"نسبة المهندس: {h['engineer_percent']}%", 11)
    hr()

    line("ملخص مالي:", 13, dy=18)
    line(f"إجمالي المشتريات: {fmt_money(ctx['total_purchases'])}", 11)
    line(f"إجمالي مدفوعات الكوادر: {fmt_money(ctx['total_payments'])}", 11)
    grand = ctx["total_purchases"] + ctx["total_payments"]
    line(f"المجموع الكلي للصرف: {fmt_money(grand)}", 12)
    line(f"قيمة المهندس المحسوبة: {fmt_money(ctx['engineer_amount'])}", 11)
    hr()

    # ===== المشتريات مرتبة حسب الفئة =====
    line("ملفات الأغراض (حسب الفئة):", 13, dy=18)
    if ctx["categories"]:
        for cat in ctx["categories"]:
            line(f"• {cat['name']} — الإجمالي: {fmt_money(cat['total'])} ({cat['count']} عملية)", 11, dy=14)
            cat_purchases = [p for p in ctx["purchases"]
                             if ((p["category"] or "أخرى").strip() or "أخرى") == cat["name"]]
            widths = [70, 80, 170, 90]
            header_row(["التاريخ", "الكادر", "الصنف", "المبلغ"], widths)
            for p in cat_purchases:
                data_row([
                    (p["created_at"] or "")[:10],
                    p["worker_name"] or "-",
                    (p["item"] or "")[:40],
                    fmt_money(p["price"]),
                ], widths)
            y -= 4
    else:
        line("لا توجد مشتريات.", 10)
    hr()

    # ===== الكوادر — كل كادر مع دفعاته ومشترياته =====
    line("ملفات الكوادر:", 13, dy=18)
    if ctx["workers"]:
        for w in ctx["workers"]:
            line(f"• {w['name']}{(' — ' + w['job']) if w['job'] else ''}", 12, dy=16)
            line(f"   إجمالي المستلم: {fmt_money(w['paid'])}    إجمالي مشترياته: {fmt_money(w['bought'])}", 10, dy=14)
            wpays = [p for p in ctx["payments"] if p["worker_id"] == w["id"]]
            if wpays:
                line("   الدفعات المستلمة:", 10, dy=12)
                widths = [70, 90, 220]
                header_row(["التاريخ", "المبلغ", "ملاحظات"], widths)
                for p in wpays:
                    data_row([
                        (p["created_at"] or "")[:10],
                        fmt_money(p["amount"]),
                        (p["notes"] or "")[:40],
                    ], widths)
            wpurch = [p for p in ctx["purchases"] if p["worker_id"] == w["id"]]
            if wpurch:
                line("   مشترياته:", 10, dy=12)
                widths = [70, 80, 180, 90]
                header_row(["التاريخ", "الفئة", "الصنف", "المبلغ"], widths)
                for p in wpurch:
                    data_row([
                        (p["created_at"] or "")[:10],
                        p["category"] or "-",
                        (p["item"] or "")[:35],
                        fmt_money(p["price"]),
                    ], widths)
            y -= 6
    else:
        line("لا يوجد كوادر.", 10)
    hr()

    line(f"المجموع الكلي للصرف: {fmt_money(grand)}", 14, dy=20)
    line(f"تم إصدار التقرير: {now_str()[:16]}", 10)

    c.save()
    buf.seek(0)
    fname = f"cubeq_house_{hid}.pdf"
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=fname)


# =========================================================
#                       ENTRY
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
