"""Persistence (SQLite, stdlib). Every method is plain SQL so porting to Postgres is mechanical."""
import json, re, sqlite3, threading, time, uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, item TEXT, amount REAL,
  status TEXT, carrier TEXT, expected_date TEXT, refunded INTEGER DEFAULT 0, reshipped INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, customer_email TEXT NOT NULL, status TEXT NOT NULL,
  created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, created_at REAL);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, note TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, tool TEXT, args TEXT,
  result TEXT, ok INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS pending_actions(id TEXT PRIMARY KEY, case_id TEXT, action TEXT, args TEXT, status TEXT,
  created_at REAL);
CREATE TABLE IF NOT EXISTS resolutions(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT UNIQUE, query TEXT,
  summary TEXT, resolved_by TEXT, rating INTEGER, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_msg_case ON messages(case_id);
CREATE INDEX IF NOT EXISTS ix_audit_case ON audit(case_id);
"""
STOP = {"the", "a", "an", "is", "my", "i", "to", "of", "and", "it", "me", "for", "on", "in", "has", "have", "not"}
HUMAN_HELD = ("escalated", "pending_approval")


def tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP}


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    # cases -------------------------------------------------------------
    def new_case(self, email: str) -> str:
        cid, now = "C-" + uuid.uuid4().hex[:8], time.time()
        self.x("INSERT INTO cases VALUES(?,?,?,?,?)", (cid, email.lower(), "open", now, now))
        return cid

    def case(self, cid):
        return self.one("SELECT * FROM cases WHERE id=?", (cid,))

    def set_status(self, cid, status):
        self.x("UPDATE cases SET status=?, updated_at=? WHERE id=?", (status, time.time(), cid))

    def cases(self, status=None, limit=100):
        if status:
            return self.q("SELECT * FROM cases WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit))
        return self.q("SELECT * FROM cases ORDER BY updated_at DESC LIMIT ?", (limit,))

    # conversation ------------------------------------------------------
    def add_message(self, cid, role, content):
        self.x("INSERT INTO messages(case_id,role,content,created_at) VALUES(?,?,?,?)",
               (cid, role, json.dumps(content), time.time()))

    def messages(self, cid):
        return [{"role": r["role"], "content": json.loads(r["content"])}
                for r in self.q("SELECT role,content FROM messages WHERE case_id=? ORDER BY id", (cid,))]

    def add_note(self, cid, note):
        self.x("INSERT INTO notes(case_id,note,created_at) VALUES(?,?,?)", (cid, note[:1000], time.time()))

    def notes(self, cid):
        return self.q("SELECT note,created_at FROM notes WHERE case_id=? ORDER BY id", (cid,))

    # audit + approvals -------------------------------------------------
    def add_audit(self, cid, tool, args, result, ok):
        self.x("INSERT INTO audit(case_id,tool,args,result,ok,created_at) VALUES(?,?,?,?,?,?)",
               (cid, tool, json.dumps(args)[:2000], result[:2000], int(ok), time.time()))

    def audit(self, cid):
        return self.q("SELECT tool,args,result,ok,created_at FROM audit WHERE case_id=? ORDER BY id", (cid,))

    def add_action(self, cid, action, args) -> str:
        aid = "A-" + uuid.uuid4().hex[:8]
        self.x("INSERT INTO pending_actions VALUES(?,?,?,?,?,?)", (aid, cid, action, json.dumps(args), "pending", time.time()))
        return aid

    def action(self, aid):
        a = self.one("SELECT * FROM pending_actions WHERE id=?", (aid,))
        if a:
            a["args"] = json.loads(a["args"])
        return a

    def set_action(self, aid, status):
        self.x("UPDATE pending_actions SET status=? WHERE id=?", (status, aid))

    def actions(self, cid):
        return self.q("SELECT id,action,args,status FROM pending_actions WHERE case_id=?", (cid,))

    # learning ----------------------------------------------------------
    def add_resolution(self, cid, query, summary, resolved_by):
        self.x("INSERT OR REPLACE INTO resolutions(case_id,query,summary,resolved_by,rating,created_at) "
               "VALUES(?,?,?,?,(SELECT rating FROM resolutions WHERE case_id=?),?)",
               (cid, query[:500], summary[:500], resolved_by, cid, time.time()))

    def set_rating(self, cid, rating: int) -> bool:
        return self.x("UPDATE resolutions SET rating=? WHERE case_id=?", (rating, cid)) > 0

    def recall(self, text: str, k=2):
        """Top-k similar cases that the agent resolved AND customers rated 4+ stars."""
        want = tokens(text)
        rows = self.q("SELECT query,summary FROM resolutions WHERE rating>=4 AND resolved_by='agent' "
                      "ORDER BY id DESC LIMIT 300")
        scored = sorted(((len(want & tokens(r["query"])), r) for r in rows), key=lambda t: -t[0])
        return [r for s, r in scored[:k] if s > 0]

    def metrics(self):
        c = {r["status"]: r["n"] for r in self.q("SELECT status, COUNT(*) n FROM cases GROUP BY status")}
        total = sum(c.values())
        by_agent = self.one("SELECT COUNT(*) n FROM resolutions WHERE resolved_by='agent'")["n"]
        avg = self.one("SELECT AVG(rating) a FROM resolutions WHERE rating IS NOT NULL")["a"]
        return {"cases": total, "by_status": c, "resolved_by_agent": by_agent,
                "automation_rate": round(by_agent / total, 3) if total else 0.0,
                "avg_rating": round(avg, 2) if avg else None}
