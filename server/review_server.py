#!/usr/bin/env python3
"""Comment and suggested-edit store for co-author review rounds.

Standard library only (sqlite3 + http.server), meant to run in a small
container behind the existing reverse proxy, e.g. at /review-api/.

Each person gets a personal key per paper; the key is the only credential.
Only SHA-256 digests of keys are stored. Roles: ``owner`` (opens and closes
rounds, invites, accepts or rejects) and ``author`` (comments, suggests,
replies, withdraws own items).

    review_server.py serve     --db review.sqlite [--port 8000] [--prefix /review-api]
    review_server.py bootstrap --db review.sqlite --paper ID --name NAME [--email E]
"""

import argparse
import hashlib
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 256 * 1024
MAX_TEXT = 50_000
PAPER = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
  id INTEGER PRIMARY KEY, paper TEXT NOT NULL, name TEXT NOT NULL,
  email TEXT NOT NULL DEFAULT '', role TEXT NOT NULL CHECK (role IN ('owner','author')),
  key_sha TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rounds (
  paper TEXT NOT NULL, round INTEGER NOT NULL, base TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open','closed')),
  opened TEXT NOT NULL, closed TEXT, PRIMARY KEY (paper, round));
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY, paper TEXT NOT NULL, round INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('comment','suggestion','reply')),
  parent INTEGER REFERENCES items(id), block TEXT NOT NULL DEFAULT '',
  quote TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
  orig_src TEXT, new_src TEXT, author INTEGER NOT NULL REFERENCES authors(id),
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','accepted','rejected','withdrawn')),
  decided_by INTEGER REFERENCES authors(id),
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_round ON items (paper, round);
"""

LOCK = threading.Lock()
PRESENCE_TTL = 30          # seconds without a heartbeat before someone disappears
BLOCK = re.compile(r"^rb-\d{1,6}$")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def connect(path):
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def new_author(db, paper, name, email, role):
    key = secrets.token_urlsafe(24)
    cur = db.execute(
        "INSERT INTO authors (paper, name, email, role, key_sha, created) VALUES (?,?,?,?,?,?)",
        (paper, name, email, role, digest(key), now()))
    return cur.lastrowid, key


class Fail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def text(d, k, limit=MAX_TEXT, required=False):
    v = d.get(k, "")
    if not isinstance(v, str) or len(v) > limit:
        raise Fail(400, f"bad field {k}")
    if required and not v.strip():
        raise Fail(400, f"missing field {k}")
    return v


def item_json(r):
    return {k: r[k] for k in r.keys()}


class API:
    """Routes. Every method gets (db, me, body, *path-params) and returns JSON."""

    def __init__(self, db):
        self.db = db
        # (paper, round) -> {author id: (name, block, mode, time)}. Memory only:
        # positions are never written to the database or the log.
        self.here = {}

    def me(self, db, me, body, paper):
        rounds = [dict(r) for r in db.execute(
            "SELECT round, base, state, opened, closed FROM rounds WHERE paper=? ORDER BY round",
            (paper,))]
        return {"id": me["id"], "name": me["name"], "role": me["role"], "rounds": rounds}

    def round(self, db, me, body, paper, rnd):
        r = db.execute("SELECT * FROM rounds WHERE paper=? AND round=?", (paper, rnd)).fetchone()
        if not r:
            raise Fail(404, "no such round")
        items = db.execute(
            "SELECT i.*, a.name AS author_name, a.email AS author_email, d.name AS decided_by_name "
            "FROM items i JOIN authors a ON a.id=i.author LEFT JOIN authors d ON d.id=i.decided_by "
            "WHERE i.paper=? AND i.round=? ORDER BY i.id", (paper, rnd))
        return {"round": dict(r), "items": [item_json(i) for i in items]}

    def put_round(self, db, me, body, paper, rnd):
        self.owner(me)
        state = text(body, "state") or "open"
        if state not in ("open", "closed"):
            raise Fail(400, "bad state")
        r = db.execute("SELECT * FROM rounds WHERE paper=? AND round=?", (paper, rnd)).fetchone()
        if r is None:
            base = text(body, "base", 64, required=True)
            db.execute("INSERT INTO rounds VALUES (?,?,?,?,?,NULL)",
                       (paper, rnd, base, state, now()))
        else:
            if body.get("base") and body["base"] != r["base"]:
                raise Fail(409, "a round's base commit cannot change; open a new round")
            db.execute("UPDATE rounds SET state=?, closed=? WHERE paper=? AND round=?",
                       (state, now() if state == "closed" else None, paper, rnd))
        return self.round(db, me, body, paper, rnd)

    def post_item(self, db, me, body, paper, rnd):
        r = db.execute("SELECT state FROM rounds WHERE paper=? AND round=?", (paper, rnd)).fetchone()
        if not r:
            raise Fail(404, "no such round")
        if r["state"] != "open":
            raise Fail(409, "this round is closed")
        kind = text(body, "kind")
        block, quote, msg = text(body, "block", 64), text(body, "quote", 5000), text(body, "body")
        parent = orig = new = None
        if kind == "reply":
            parent = body.get("parent")
            p = db.execute("SELECT * FROM items WHERE id=? AND paper=? AND round=? AND parent IS NULL",
                           (parent, paper, rnd)).fetchone()
            if not p:
                raise Fail(400, "bad parent")
            block = p["block"]
            if not msg.strip():
                raise Fail(400, "empty reply")
        elif kind == "comment":
            if not (block and msg.strip()):
                raise Fail(400, "a comment needs a block and a text")
        elif kind == "suggestion":
            orig, new = text(body, "orig_src", required=True), text(body, "new_src")
            if not block or orig == new:
                raise Fail(400, "a suggestion needs a block and a change")
        else:
            raise Fail(400, "bad kind")
        t = now()
        cur = db.execute(
            "INSERT INTO items (paper, round, kind, parent, block, quote, body, orig_src, new_src,"
            " author, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (paper, rnd, kind, parent, block, quote, msg, orig, new, me["id"], t, t))
        return {"id": cur.lastrowid}

    def act(self, db, me, body, paper, iid):
        i = db.execute("SELECT * FROM items WHERE id=? AND paper=?", (iid, paper)).fetchone()
        if not i:
            raise Fail(404, "no such item")
        st = db.execute("SELECT state FROM rounds WHERE paper=? AND round=?",
                        (paper, i["round"])).fetchone()["state"]
        action = text(body, "action")
        if action == "withdraw":
            if i["author"] != me["id"] or i["status"] != "open":
                raise Fail(403, "only an open item of your own can be withdrawn")
            status = "withdrawn"
        elif action in ("accept", "reject", "reopen"):
            self.owner(me)
            if i["parent"] is not None:
                raise Fail(400, "replies are not decided")
            status = {"accept": "accepted", "reject": "rejected", "reopen": "open"}[action]
            if action == "accept" and i["kind"] == "suggestion":
                clash = db.execute(
                    "SELECT id FROM items WHERE paper=? AND round=? AND block=? AND kind='suggestion'"
                    " AND status='accepted' AND id<>?", (paper, i["round"], i["block"], iid)).fetchone()
                if clash:
                    raise Fail(409, f"suggestion #{clash['id']} on this paragraph is already accepted;"
                                    " reopen it, or write one merged suggestion and accept that")
        else:
            raise Fail(400, "bad action")
        if st != "open" and me["role"] != "owner":
            raise Fail(409, "this round is closed")
        db.execute("UPDATE items SET status=?, decided_by=?, updated=? WHERE id=?",
                   (status, me["id"] if action != "withdraw" else None, now(), iid))
        return {"id": iid, "status": status}

    def presence(self, db, me, body, paper, rnd):
        """Record where the caller is (block and reading/editing); return the others."""
        block, mode = body.get("block"), body.get("mode", "reading")
        if not (block is None or isinstance(block, str) and BLOCK.match(block)) \
                or mode not in ("reading", "editing"):
            raise Fail(400, "bad presence")
        t = time.monotonic()
        here = self.here.setdefault((paper, rnd), {})
        if block:
            here[me["id"]] = (me["name"], block, mode, t)
        else:
            here.pop(me["id"], None)
        for aid in [a for a, v in here.items() if t - v[3] > PRESENCE_TTL]:
            del here[aid]
        return {"others": [{"id": a, "name": n, "block": b, "mode": m}
                           for a, (n, b, m, _) in here.items() if a != me["id"]]}

    def authors(self, db, me, body, paper):
        self.owner(me)
        return [dict(r) for r in db.execute(
            "SELECT id, name, email, role, active, created FROM authors WHERE paper=? ORDER BY id",
            (paper,))]

    def invite(self, db, me, body, paper):
        self.owner(me)
        role = text(body, "role") or "author"
        if role not in ("owner", "author"):
            raise Fail(400, "bad role")
        aid, key = new_author(db, paper, text(body, "name", 200, required=True),
                              text(body, "email", 200), role)
        return {"id": aid, "key": key}

    def set_author(self, db, me, body, paper, aid):
        self.owner(me)
        if aid == me["id"]:
            raise Fail(400, "you cannot revoke yourself")
        db.execute("UPDATE authors SET active=? WHERE id=? AND paper=?",
                   (1 if body.get("active") else 0, aid, paper))
        return {"id": aid, "active": bool(body.get("active"))}

    @staticmethod
    def owner(me):
        if me["role"] != "owner":
            raise Fail(403, "owner only")


ROUTES = [
    ("GET", r"/(?P<paper>[^/]+)/me", "me"),
    ("GET", r"/(?P<paper>[^/]+)/rounds/(?P<rnd>\d+)", "round"),
    ("PUT", r"/(?P<paper>[^/]+)/rounds/(?P<rnd>\d+)", "put_round"),
    ("POST", r"/(?P<paper>[^/]+)/rounds/(?P<rnd>\d+)/items", "post_item"),
    ("POST", r"/(?P<paper>[^/]+)/rounds/(?P<rnd>\d+)/presence", "presence"),
    ("POST", r"/(?P<paper>[^/]+)/items/(?P<iid>\d+)", "act"),
    ("GET", r"/(?P<paper>[^/]+)/authors", "authors"),
    ("POST", r"/(?P<paper>[^/]+)/authors", "invite"),
    ("POST", r"/(?P<paper>[^/]+)/authors/(?P<aid>\d+)", "set_author"),
]


def make_handler(api, prefix, static=None):
    routes = [(m, re.compile("^" + re.escape(prefix) + p + "$"), f) for m, p, f in ROUTES]

    class Handler(BaseHTTPRequestHandler):
        server_version = "review/1"

        def log_message(self, fmt, *args):      # keys travel in headers, never logged
            sys.stderr.write(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

        def reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self):
            try:
                path = self.path.split("?", 1)[0]
                if static and self.command == "GET" and not path.startswith(prefix + "/"):
                    return self.send_static(path)
                for m, rx, fn in routes:
                    hit = rx.match(path)
                    if hit and m == self.command:
                        break
                else:
                    raise Fail(404, "not found")
                params = hit.groupdict()
                if not PAPER.match(params["paper"]):
                    raise Fail(404, "not found")
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise Fail(413, "too large")
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise Fail(400, "bad body")
                auth = self.headers.get("Authorization", "")
                key = auth[7:] if auth.startswith("Bearer ") else ""
                with LOCK:
                    me = api.db.execute(
                        "SELECT * FROM authors WHERE key_sha=? AND paper=? AND active=1",
                        (digest(key), params["paper"])).fetchone() if key else None
                    if not me:
                        raise Fail(401, "unknown or revoked key")
                    args = [int(v) if k in ("rnd", "iid", "aid") else v
                            for k, v in params.items()]
                    out = getattr(api, fn)(api.db, me, body, *args)
                self.reply(200, out)        # a slow client must not hold the lock
            except Fail as e:
                self.reply(e.code, {"error": str(e)})
            except (ValueError, KeyError) as e:
                self.reply(400, {"error": f"bad request: {e}"})

        def send_static(self, path):
            """Local preview only (--static); in production the web server does this."""
            from pathlib import Path
            root = Path(static).resolve()
            f = (root / path.lstrip("/")).resolve()
            if f.is_dir():
                f = f / "index.html"
            if root not in f.parents or not f.is_file():
                raise Fail(404, "not found")
            import mimetypes
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = handle_any

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--db", required=True)
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--prefix", default="/review-api")
    s.add_argument("--static", metavar="DIR", help="also serve DIR (local preview)")
    b = sub.add_parser("bootstrap", help="create the owner of a paper and print the key")
    b.add_argument("--db", required=True)
    b.add_argument("--paper", required=True)
    b.add_argument("--name", required=True)
    b.add_argument("--email", default="")
    a = ap.parse_args()
    db = connect(a.db)
    if a.cmd == "bootstrap":
        if not PAPER.match(a.paper):
            sys.exit("paper id: lower-case letters, digits and '-'")
        _, key = new_author(db, a.paper, a.name, a.email, "owner")
        print(key)
        return
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(API(db), a.prefix.rstrip("/"), a.static))
    print(f"listening on {a.host}:{a.port}{a.prefix}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
