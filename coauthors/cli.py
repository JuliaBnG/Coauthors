"""coauthors: round-based co-author review of pandoc-markdown papers.

    coauthors publish  PAPER [--base REV] [--new-round] [--dry-run DIR] [--force]
    coauthors invite   PAPER NAME [--email E] [--owner]
    coauthors authors  PAPER [--revoke ID]
    coauthors status   PAPER [--round N]
    coauthors close    PAPER [--round N]
    coauthors apply    PAPER [--round N] [--force]
    coauthors archive  PAPER [--round N]

PAPER names papers/PAPER.toml. The owner key is read from $COAUTHORS_KEY or
~/.config/coauthors/PAPER.key.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import blocks as B
from . import render as R

HOME = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- config / API

def load(paper):
    p = HOME / "papers" / f"{paper}.toml"
    if not p.exists():
        sys.exit(f"no {p}")
    cfg = tomllib.loads(p.read_text())
    cfg["repo"] = Path(os.path.expanduser(cfg["repo"]))
    if cfg["id"] != paper:
        sys.exit(f"{p}: id must be {paper!r}")
    return cfg


def owner_key(paper):
    if os.environ.get("COAUTHORS_KEY"):
        return os.environ["COAUTHORS_KEY"]
    f = Path.home() / ".config" / "coauthors" / f"{paper}.key"
    if not f.exists():
        sys.exit(f"no owner key: set COAUTHORS_KEY or write it to {f} (chmod 600)")
    return f.read_text().strip()


def call(cfg, method, path, body=None):
    req = urllib.request.Request(
        cfg["api"].rstrip("/") + f"/{cfg['id']}{path}", method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {owner_key(cfg['id'])}",
                 "Content-Type": "application/json",
                 "User-Agent": "coauthors/1"})      # Cloudflare rejects Python-urllib
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e)["error"]
        except Exception:
            msg = e.reason
        sys.exit(f"{method} {path}: {e.code} {msg}")


def rounds(cfg):
    return call(cfg, "GET", "/me")["rounds"]


def current(cfg, n=None):
    rs = rounds(cfg)
    if n is not None:
        rs = [r for r in rs if r["round"] == n]
    if not rs:
        sys.exit("no such round" if n is not None else "no round yet; run publish --new-round")
    return rs[-1]


def personal_url(cfg, key):
    return f"{cfg['url']}#k={key}"


# ---------------------------------------------------------------- commands

def cmd_publish(cfg, a):
    rs = [] if a.dry_run else rounds(cfg)
    if a.dry_run or a.new_round or not rs:
        rnd = (rs[-1]["round"] + 1) if rs else 1
        base = R.git(cfg["repo"], "rev-parse", "--verify", (a.base or "HEAD") + "^{commit}").strip()
        if rs and rs[-1]["state"] == "open" and not a.dry_run:
            prev = call(cfg, "GET", f"/rounds/{rs[-1]['round']}")
            undecided = [i for i in prev["items"] if i["kind"] == "suggestion" and i["status"] == "open"]
            if undecided and not a.force:
                sys.exit(f"round {rs[-1]['round']} has {len(undecided)} undecided suggestion(s);"
                         " decide them, or pass --force")
    else:
        rnd, base = rs[-1]["round"], rs[-1]["base"]
        if a.base:
            sys.exit("a round keeps its base commit; use --new-round to move to another commit")
    dirty = R.git(cfg["repo"], "status", "--porcelain", "--", cfg["dir"]).strip()
    if dirty and (a.dry_run or a.new_round or not rs):
        print(f"note: uncommitted changes in {cfg['dir']} are not part of {base[:10]}:\n{dirty}",
              file=sys.stderr)

    out = Path(a.dry_run) if a.dry_run else Path(tempfile.mkdtemp(prefix="coauthors-out-"))
    with R.worktree(cfg["repo"], base) as wt:
        table = R.render(cfg, wt, out, rnd, base)
    print(f"round {rnd} at {base[:10]}: {len(table)} reviewable blocks")
    if a.dry_run:
        print(f"written to {out}/index.html (not published, no round opened)")
        return
    if rs and rs[-1]["round"] != rnd and rs[-1]["state"] == "open":
        call(cfg, "PUT", f"/rounds/{rs[-1]['round']}", {"state": "closed"})
        print(f"closed round {rs[-1]['round']}")
    call(cfg, "PUT", f"/rounds/{rnd}", {"base": base, "state": "open"})
    subprocess.run(["chmod", "-R", "u=rwX,go=rX", str(out)], check=True)
    subprocess.run(["rsync", "-a", "--delete", f"{out}/", cfg["dest"]], check=True)
    subprocess.run(["rm", "-rf", str(out)], check=True)
    print(f"published {cfg['url']}")


def cmd_invite(cfg, a):
    r = call(cfg, "POST", "/authors",
             {"name": a.name, "email": a.email or "", "role": "owner" if a.owner else "author"})
    print(f"{a.name} (id {r['id']}): send this link privately; it is the only copy of the key\n"
          f"  {personal_url(cfg, r['key'])}")


def cmd_authors(cfg, a):
    if a.revoke:
        call(cfg, "POST", f"/authors/{a.revoke}", {"active": False})
    for p in call(cfg, "GET", "/authors"):
        print(f"{p['id']:>3}  {p['role']:<6} {'active ' if p['active'] else 'revoked'}"
              f"  {p['name']} <{p['email']}>")


def cmd_status(cfg, a):
    r = current(cfg, a.round)
    data = call(cfg, "GET", f"/rounds/{r['round']}")
    print(f"round {r['round']} ({r['state']}) at {r['base'][:10]}, opened {r['opened']}")
    from collections import Counter
    c = Counter((i["kind"], i["status"]) for i in data["items"] if i["kind"] != "reply")
    for (k, s), n in sorted(c.items()):
        print(f"  {k:<10} {s:<9} {n}")
    print(f"  replies    {sum(i['kind'] == 'reply' for i in data['items'])}")


def cmd_close(cfg, a):
    r = current(cfg, a.round)
    call(cfg, "PUT", f"/rounds/{r['round']}", {"state": "closed"})
    print(f"closed round {r['round']}")


def cmd_archive(cfg, a):
    r = current(cfg, a.round)
    data = call(cfg, "GET", f"/rounds/{r['round']}")
    for i in data["items"]:
        i.pop("author_email", None)
    d = HOME / "archive" / cfg["id"]
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"round-{r['round']:02d}.json"
    f.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {f.relative_to(HOME)}")


# ---------------------------------------------------------------- apply

# A wrapped line must not begin with a token that pandoc reads as block syntax.
RISKY = re.compile(r"^([-+*:~=]$|[#>|]|\d+[.)]$|[a-zA-Z][.)]$|\[\^|Table:)")
PLAIN_HEAD = re.compile(r"^(#|\||:::|```|~~~|\$\$|<|!\[|[-+*]\s|\d+[.)]\s|Table:|: |\s)")


def is_prose(src):
    lines = src.split("\n")
    return not (PLAIN_HEAD.match(lines[0]) or any(
        l.endswith(("  ", "\\")) or l.startswith("|") for l in lines))


def fill(par, width=72):
    """Greedy fill like Emacs M-q, never starting a line with block syntax."""
    words, lines, cur = par.split(), [], []
    for w in words:
        if cur and len(" ".join(cur + [w])) > width:
            if RISKY.match(w) and len(cur) > 1:
                lines.append(" ".join(cur[:-1]))
                cur = [cur[-1]]
            else:
                lines.append(" ".join(cur))
                cur = []
        cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


def new_lines(orig, new):
    new = new.replace("\r\n", "\n").strip("\n")
    if not new.strip():
        return []
    if is_prose(orig) and all(is_prose(p) for p in re.split(r"\n\s*\n", new)):
        out = []
        for p in re.split(r"\n\s*\n", new):
            out += ([""] if out else []) + fill(p)
        return out
    return [l.rstrip() for l in new.split("\n")]


LABEL = re.compile(r"\{#([A-Za-z0-9_][\w:.-]*)|\\label\{([^}]+)\}")
ATX = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.M)


def auto_id(title):
    """Pandoc's auto_identifiers, for headings without an explicit {#id}."""
    t = re.sub(r"\{[^}]*\}\s*$", "", title)          # trailing {.unnumbered} etc.
    t = re.sub(r"\]\([^)]*\)", "", t)                 # link targets
    t = re.sub(r"[^\w\s.-]", "", t.lower())
    t = re.sub(r"\s+", "-", t.strip())
    return re.sub(r"^[\W\d_]+", "", t) or "section"


def labels(src):
    out = {m.group(1) or m.group(2) for m in LABEL.finditer(src)}
    out |= {auto_id(m.group(1)) for m in ATX.finditer(src) if "{#" not in m.group(1)}
    return out


def references(text, ident):
    """Lines of ``text`` that refer to ``ident``: @id, [@id], [-@id], ](#id), \\ref{id}, \\eqref{id}."""
    i = re.escape(ident)
    rx = re.compile(rf"(?<![\w@])-?@{i}(?![\w-]|[:.][\w])|\]\(#{i}\)|\\\w*ref\{{{i}\}}")
    return [n for n, l in enumerate(text.split("\n"), 1) if rx.search(l)]


def dangling(wt, cfg, acc):
    """Labels the accepted edits remove that the edited sections still cite."""
    gone = set().union(*(labels(i["orig_src"]) for i in acc)) - \
        set().union(*(labels(i["new_src"]) for i in acc))
    if not gone:
        return []
    texts = {f: B.strip_comments((wt / cfg["dir"] / f).read_text()) for f in cfg["sections"]}
    for t in texts.values():
        gone -= labels(t)                            # moved, or still defined elsewhere
    return [(ident, f, n) for ident in sorted(gone)
            for f, t in texts.items() for n in references(t, ident)]


def cmd_apply(cfg, a):
    r = current(cfg, a.round)
    rnd, base = r["round"], r["base"]
    data = call(cfg, "GET", f"/rounds/{rnd}")
    acc = [i for i in data["items"] if i["kind"] == "suggestion" and i["status"] == "accepted"]
    if not acc:
        sys.exit(f"round {rnd}: no accepted suggestions")
    if r["state"] == "open":
        print(f"note: round {rnd} is still open; later decisions need another apply", file=sys.stderr)
    branch = f"coauthors/{cfg['id']}-r{rnd}"
    repo = cfg["repo"]
    if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch],
                      capture_output=True).returncode == 0:
        sys.exit(f"branch {branch} exists in {repo}; delete it to re-apply")

    with R.worktree(repo, base) as wt:
        table, _, _ = R.prepare(cfg, wt)
        per_file, warn = {}, []
        for i in acc:
            b = table.get(i["block"])
            if b is None or b["src"] != i["orig_src"]:
                sys.exit(f"suggestion #{i['id']}: block {i['block']} does not match {base[:10]}")
            per_file.setdefault(b["f"], []).append((b, i))
        for f, edits in per_file.items():
            path = wt / cfg["dir"] / f
            lines = path.read_text().split("\n")
            for b, i in sorted(edits, key=lambda e: -e[0]["a"]):
                old = lines[b["a"] - 1:b["b"]]
                notes = re.findall(r"<!--.*?-->", "\n".join(old), re.S)
                rep = new_lines(b["src"], i["new_src"])
                if notes:
                    rep = [*"\n".join(notes).split("\n"), *rep]
                    warn.append(f"#{i['id']} {f}:{b['a']}: internal notes kept above the new text")
                hi = b["b"]
                if not rep and hi < len(lines) and lines[hi].strip() == "":
                    hi += 1                         # deleted block: drop one blank line
                lines[b["a"] - 1:hi] = rep
            path.write_text("\n".join(lines))
        bad = dangling(wt, cfg, acc)
        if bad:
            lines = [f"  {f}:{n} still refers to #{ident}" for ident, f, n in bad]
            if not a.force:
                sys.exit("accepted edits remove labels that are still referenced:\n"
                         + "\n".join(lines) + "\nfix with another suggestion, or apply --force")
            warn += [l.strip() for l in lines]
        names = {}
        for i in acc:
            names.setdefault(i["author_name"], i["author_email"])
        msg = [f"{cfg['id']}: co-author edits, review round {rnd}", ""]
        for f, edits in per_file.items():
            for b, i in sorted(edits, key=lambda e: e[0]["a"]):
                note = f": {i['body'].strip().splitlines()[0][:60]}" if i["body"].strip() else ""
                msg.append(f"- {f}:{b['a']}-{b['b']} (#{i['id']}, {i['author_name']}){note}")
        trailers = [f"Co-authored-by: {n} <{e}>" for n, e in names.items() if e]
        if trailers:
            msg += [""] + trailers
        R.git(wt, "switch", "--quiet", "-c", branch)
        R.git(wt, "commit", "--quiet", "-a", "-F", "-", input="\n".join(msg) + "\n")
        stat = R.git(wt, "show", "--stat", "--format=%h %s", "HEAD")
    print(stat)
    for w in warn:
        print("warning:", w)
    print(f"Branch {branch} (from {base[:10]}) holds {len(acc)} accepted edit(s). Next:\n"
          f"  git -C {repo} diff {base[:10]} {branch}\n"
          f"  git -C {repo} merge {branch}      # or rebase; git resolves against later work")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="coauthors", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish", help="render and upload; opens round 1 or with --new-round")
    p.add_argument("paper")
    p.add_argument("--base", help="commit of the paper repo to review (default HEAD)")
    p.add_argument("--new-round", action="store_true")
    p.add_argument("--force", action="store_true", help="close a round with undecided suggestions")
    p.add_argument("--dry-run", metavar="DIR", help="render into DIR only")
    p = sub.add_parser("invite")
    p.add_argument("paper")
    p.add_argument("name")
    p.add_argument("--email")
    p.add_argument("--owner", action="store_true")
    p = sub.add_parser("authors")
    p.add_argument("paper")
    p.add_argument("--revoke", type=int, metavar="ID")
    for c in ("status", "close", "apply", "archive"):
        p = sub.add_parser(c)
        p.add_argument("paper")
        p.add_argument("--round", type=int)
        if c == "apply":
            p.add_argument("--force", action="store_true", help="apply despite dangling references")
    a = ap.parse_args(argv)
    globals()[f"cmd_{a.cmd}"](load(a.paper), a)


if __name__ == "__main__":
    main()
