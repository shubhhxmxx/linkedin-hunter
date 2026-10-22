#!/usr/bin/env python3
"""Opportunity Hunter: low-cost, standalone hiring-post discovery dashboard."""
from __future__ import annotations
import hashlib, html, json, os, re, sqlite3, threading, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", str(ROOT / "data" / "opportunity_hunter.db"))
PORT = int(os.getenv("PORT", "8080"))
SCAN_INTERVAL = max(300, int(os.getenv("SCAN_INTERVAL_SECONDS", "3600")))
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
USER_PROFILE = os.getenv("CANDIDATE_PROFILE", "Java backend engineer with about 3 years of experience; Spring Boot, distributed systems, APIs; India-based; prioritize remote-friendly roles and hiring/referral posts.")

KEYWORDS = {
    "java": 10, "spring boot": 12, "backend": 8, "back-end": 8, "distributed systems": 12,
    "microservices": 8, "software engineer": 5, "software developer": 5, "platform engineer": 7,
    "referral": 14, "refer": 10, "we're hiring": 12, "we are hiring": 12, "hiring": 7,
    "dm me": 8, "apply": 3, "remote": 8, "india": 4, "3+ years": 4, "2+ years": 4,
}


def utcnow(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

@contextmanager
def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, feed_url TEXT NOT NULL UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS opportunities (
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, company TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
          source_id INTEGER, source_name TEXT NOT NULL DEFAULT '', published_at TEXT, discovered_at TEXT NOT NULL,
          score INTEGER NOT NULL DEFAULT 0, category TEXT NOT NULL DEFAULT 'Potential hiring post',
          fit_summary TEXT NOT NULL DEFAULT '', skills TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'new',
          dedupe_key TEXT NOT NULL UNIQUE, raw_json TEXT NOT NULL DEFAULT '{}',
          FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS scans (
          id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, sources_checked INTEGER NOT NULL DEFAULT 0,
          items_seen INTEGER NOT NULL DEFAULT 0, new_items INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '', detail_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_opportunities_discovered ON opportunities(discovered_at DESC);
        CREATE INDEX IF NOT EXISTS idx_opportunities_score ON opportunities(score DESC);
        ''')
        default_feed = os.getenv("LINKEDIN_ALERT_RSS_URL", "").strip()
        if default_feed:
            c.execute("INSERT OR IGNORE INTO sources(name,feed_url,enabled,created_at) VALUES(?,?,1,?)",
                      ("LinkedIn alert feed", default_feed, utcnow()))


def clean_url(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url.strip())
        if p.scheme not in ("http", "https") or not p.netloc: return url.strip()
        q = [(k,v) for k,v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"trk", "trackingid", "refid"}]
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urllib.parse.urlencode(q), ""))
    except Exception: return url.strip()


def plain(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_date(value):
    if not value: return None
    try: return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception: return value[:80]


def classify(title: str, body: str, url: str):
    text = f"{title} {body}".lower()
    score = min(100, sum(points for word, points in KEYWORDS.items() if word in text))
    if any(x in text for x in ("referral", "refer me", "dm me", "we're hiring", "we are hiring")):
        category = "Hiring / referral post"
    elif "linkedin.com/jobs" in url.lower() or "job opening" in text or "job description" in text:
        category = "Job listing"
    else: category = "Potential hiring post"
    skills = [s for s in ["Java", "Spring Boot", "Distributed systems", "Microservices", "Kafka", "AWS", "PostgreSQL", "Kubernetes", "REST APIs"] if s.lower() in text]
    reasons = []
    if any(s in text for s in ("java", "spring boot", "backend", "distributed systems")): reasons.append("relevant backend/Java signal")
    if any(s in text for s in ("referral", "refer", "dm me")): reasons.append("possible direct/referral path")
    if "remote" in text: reasons.append("mentions remote work; eligibility still needs checking")
    summary = "; ".join(reasons) or "Potential opportunity based on hiring-related wording; review the original post."
    return score, category, summary, skills


def llm_qualify(title, body, url):
    """Optional BYO-key enrichment; deterministic qualification remains available without a paid model."""
    if not LLM_API_KEY: return None
    payload = {"model": LLM_MODEL, "temperature": 0, "response_format": {"type": "json_object"},
               "messages": [{"role":"system", "content":"Return JSON only with score (0-100 integer), category, fit_summary, skills (array). Evaluate whether this is a real software hiring/referral opportunity for this candidate: "+USER_PROFILE+" Be conservative; don't infer unstated eligibility."},
                            {"role":"user", "content":json.dumps({"title":title,"body":body[:5000],"url":url})}]}
    req = urllib.request.Request(LLM_BASE_URL, data=json.dumps(payload).encode(), headers={"Content-Type":"application/json", "Authorization":"Bearer "+LLM_API_KEY}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=18) as res: data = json.loads(res.read().decode())
        content = data["choices"][0]["message"]["content"]
        out = json.loads(content)
        return (max(0,min(100,int(out.get("score",0)))), str(out.get("category","Potential hiring post"))[:80],
                str(out.get("fit_summary",""))[:500], [str(x)[:40] for x in out.get("skills",[])[:15]])
    except Exception: return None


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    # RSS 2.0 and Atom, including common namespaced fields.
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"): continue
        d = {}
        for child in el:
            k = child.tag.split("}")[-1].lower()
            if k == "link":
                href = child.attrib.get("href") or (child.text or "").strip()
                if href: d.setdefault("link", href)
            elif k in ("title", "description", "summary", "content", "author", "creator", "pubdate", "published", "updated"):
                d[k] = " ".join(child.itertext()).strip()
        title = plain(d.get("title", "")) or "Untitled opportunity"
        body = plain(d.get("description") or d.get("summary") or d.get("content") or "")
        link = clean_url(d.get("link", ""))
        if not link: continue
        items.append({"title":title[:300], "body":body[:12000], "url":link,
                      "author":plain(d.get("author") or d.get("creator") or "")[:180],
                      "published_at":parse_date(d.get("pubdate") or d.get("published") or d.get("updated"))})
    return items


def fetch_feed(url):
    # Fetch only user-configured feeds; do not crawl LinkedIn pages or bypass login/access controls.
    req = urllib.request.Request(url, headers={"User-Agent":"OpportunityHunter/0.1 (+RSS feed reader)"})
    with urllib.request.urlopen(req, timeout=20) as res:
        if res.status != 200: raise RuntimeError(f"Feed returned HTTP {res.status}")
        return parse_feed(res.read(2_000_000))


def save_opportunity(item, source_id=None, source_name="Manual", raw=None):
    title = plain(item.get("title", "Untitled opportunity"))[:300] or "Untitled opportunity"
    body = plain(item.get("body", ""))[:12000]
    url = clean_url(item.get("url", ""))
    parsed_url = urllib.parse.urlsplit(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc: raise ValueError("A valid http(s) source URL is required")
    company = plain(item.get("company", ""))[:180]
    identity = url.lower() if url else (title.lower()+"|"+company.lower())
    dedupe = hashlib.sha256(identity.encode()).hexdigest()
    qual = llm_qualify(title, body, url) or classify(title, body, url)
    score, category, summary, skills = qual
    with db() as c:
        existing = c.execute("SELECT id FROM opportunities WHERE dedupe_key=?", (dedupe,)).fetchone()
        if existing: return False, existing["id"]
        cur = c.execute('''INSERT INTO opportunities(title,company,url,author,body,source_id,source_name,published_at,discovered_at,score,category,fit_summary,skills,status,dedupe_key,raw_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?)''',
          (title,company,url,plain(item.get("author",""))[:180],body,source_id,source_name,item.get("published_at"),utcnow(),score,category,summary,json.dumps(skills),dedupe,json.dumps(raw or {},ensure_ascii=False)))
        return True, cur.lastrowid


def run_scan():
    started = utcnow()
    with db() as c:
        cur = c.execute("INSERT INTO scans(started_at,status) VALUES(?, 'running')", (started,))
        scan_id = cur.lastrowid
        sources = c.execute("SELECT * FROM sources WHERE enabled=1 ORDER BY id").fetchall()
    seen = added = checked = 0
    errors = []
    for source in sources:
        checked += 1
        try:
            items = fetch_feed(source["feed_url"])
            seen += len(items)
            for item in items:
                try:
                    is_new, _ = save_opportunity(item, source["id"], source["name"], {"feed":source["feed_url"]})
                    added += int(is_new)
                except Exception as e: errors.append(f"{source['name']}: item skipped ({str(e)[:120]})")
        except Exception as e:
            errors.append(f"{source['name']}: {str(e)[:240]}")
    status = "completed_with_errors" if errors else "completed"
    with db() as c:
        c.execute("UPDATE scans SET finished_at=?,status=?,sources_checked=?,items_seen=?,new_items=?,error=?,detail_json=? WHERE id=?",
                  (utcnow(),status,checked,seen,added," | ".join(errors)[:2000],json.dumps({"errors":errors}),scan_id))
    return {"id":scan_id,"status":status,"sources_checked":checked,"items_seen":seen,"new_items":added,"errors":errors,"started_at":started}


def scheduler():
    # Start immediately, then repeat hourly; no overlapping scan workers.
    while True:
        try: run_scan()
        except Exception as e:
            with db() as c: c.execute("INSERT INTO scans(started_at,finished_at,status,error) VALUES(?,?,?,?)", (utcnow(),utcnow(),"failed",str(e)[:1000]))
        time.sleep(SCAN_INTERVAL)


def rowdict(r):
    d = dict(r)
    if "skills" in d:
        try: d["skills"] = json.loads(d["skills"] or "[]")
        except Exception: d["skills"] = []
    return d

class Handler(BaseHTTPRequestHandler):
    server_version = "OpportunityHunter/0.1"
    def log_message(self, fmt, *args): print("[%s] %s" % (self.log_date_time_string(), fmt % args))
    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
    def body_json(self):
        n = int(self.headers.get("Content-Length","0"))
        if n > 100_000: raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health": return self.send_json({"ok":True,"app":"Opportunity Hunter","time":utcnow()})
        if path == "/" or path == "/index.html":
            p = ROOT / "static" / "index.html"; data = p.read_bytes(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
        if path == "/api/dashboard":
            with db() as c:
                counts = {r["status"]:r["n"] for r in c.execute("SELECT status,COUNT(*) n FROM opportunities GROUP BY status")}
                total = c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
                new_today = c.execute("SELECT COUNT(*) FROM opportunities WHERE discovered_at >= ?",(datetime.now(timezone.utc).date().isoformat()+"T00:00:00",)).fetchone()[0]
                last = c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
                sources = c.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
                strong = c.execute("SELECT COUNT(*) FROM opportunities WHERE score>=55 AND status='new'").fetchone()[0]
            return self.send_json({"total":total,"new_today":new_today,"strong_matches":strong,"sources":sources,"statuses":counts,"last_scan":rowdict(last) if last else None,"scan_interval_seconds":SCAN_INTERVAL,"llm_enabled":bool(LLM_API_KEY)})
        if path == "/api/opportunities":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query); status=q.get("status",["all"])[0]; search=q.get("q",[""])[0][:200]; min_score=int(q.get("min_score",["0"])[0])
            sql="SELECT * FROM opportunities WHERE score>=?"; args=[max(0,min(100,min_score))]
            if status != "all": sql += " AND status=?"; args.append(status)
            if search: sql += " AND (title LIKE ? OR company LIKE ? OR body LIKE ? OR author LIKE ?)"; args += [f"%{search}%"]*4
            sql += " ORDER BY score DESC, discovered_at DESC LIMIT 300"
            with db() as c: rows=[rowdict(r) for r in c.execute(sql,args).fetchall()]
            return self.send_json(rows)
        if path == "/api/sources":
            with db() as c: rows=[dict(r) for r in c.execute("SELECT id,name,feed_url,enabled,created_at FROM sources ORDER BY id DESC").fetchall()]
            return self.send_json(rows)
        if path == "/api/scans":
            with db() as c: rows=[dict(r) for r in c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 50").fetchall()]
            return self.send_json(rows)
        return self.send_json({"error":"Not found"},404)
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try: payload = self.body_json()
        except Exception as e: return self.send_json({"error":str(e)},400)
        if path == "/api/scan":
            threading.Thread(target=self._scan_async,daemon=True).start(); return self.send_json({"accepted":True,"message":"Scan started. Refresh scan history in a few seconds."},202)
        if path == "/api/opportunities/manual":
            try:
                new, oid = save_opportunity(payload, None, "Manual entry", {"manual":True})
                return self.send_json({"created":new,"id":oid,"message":"Opportunity added" if new else "Duplicate detected; existing opportunity kept"},201 if new else 200)
            except Exception as e: return self.send_json({"error":str(e)},400)
        if path == "/api/sources":
            name=plain(payload.get("name","LinkedIn alert feed"))[:120]; url=payload.get("feed_url","").strip()
            parsed=urllib.parse.urlsplit(url)
            if parsed.scheme not in ("http","https") or not parsed.netloc: return self.send_json({"error":"Enter a valid RSS/Atom feed URL (https://...)."},400)
            try:
                with db() as c: cur=c.execute("INSERT INTO sources(name,feed_url,enabled,created_at) VALUES(?,?,1,?)",(name or "Hiring feed",url,utcnow()))
                return self.send_json({"id":cur.lastrowid,"message":"Feed added"},201)
            except sqlite3.IntegrityError: return self.send_json({"error":"That feed is already configured."},409)
        return self.send_json({"error":"Not found"},404)
    def _scan_async(self):
        try: run_scan()
        except Exception as e: print("Manual scan failed:",e)
    def do_PATCH(self):
        path = urllib.parse.urlsplit(self.path).path
        m = re.fullmatch(r"/api/opportunities/(\d+)",path)
        if not m: return self.send_json({"error":"Not found"},404)
        try: payload=self.body_json()
        except Exception as e: return self.send_json({"error":str(e)},400)
        status=payload.get("status","")
        if status not in {"new","saved","applied","dismissed","contacted"}: return self.send_json({"error":"Invalid status"},400)
        with db() as c: cur=c.execute("UPDATE opportunities SET status=? WHERE id=?",(status,int(m.group(1))))
        return self.send_json({"updated":cur.rowcount==1})

if __name__ == "__main__":
    init_db()
    print(f"Opportunity Hunter starting on :{PORT}; scan interval={SCAN_INTERVAL}s; LLM={'enabled' if LLM_API_KEY else 'disabled (rules-based qualification)'}")
    threading.Thread(target=scheduler,daemon=True,name="hourly-discovery").start()
    ThreadingHTTPServer(("0.0.0.0",PORT),Handler).serve_forever()
