"""Render one review round of a paper from a fixed commit of its repository."""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import blocks as B

ASSETS = Path(__file__).resolve().parent.parent / "assets"

STRIP_LUA = """
function Div(d)
  if d.classes:includes('rb') then return d.content end
end
"""


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, **kw).stdout


@contextmanager
def worktree(repo, rev):
    """A detached, throw-away checkout of ``rev``; the repo itself is untouched."""
    tmp = Path(tempfile.mkdtemp(prefix="coauthors-"))
    wt = tmp / "wt"
    git(repo, "worktree", "add", "--detach", "--quiet", str(wt), rev)
    try:
        yield wt
    finally:
        git(repo, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(tmp, ignore_errors=True)


def prepare(cfg, root):
    """Read the section files under ``root``; return (blocks per file, markdown pair).

    The pair is the plain concatenation (as build.sh makes it, notes removed)
    and the same text with every block wrapped in a ``.rb`` div.
    """
    src_dir = Path(root) / cfg["dir"]
    plain, wrapped, table, n = [], [], {}, 0
    for f in cfg["sections"]:
        text = B.strip_comments((src_dir / f).read_text())
        bl = B.split(text)
        ids = {}
        for b in bl:
            if b.wrap:
                n += 1
                ids[id(b)] = f"rb-{n}"
                table[f"rb-{n}"] = {"f": f, "a": b.first, "b": b.last,
                                    "src": b.text, "sha": sha(b.text)}
        plain.append(text.rstrip("\n") + "\n")
        wrapped.append(B.wrap(bl, ids))
    tail = "## References\n\n::: {#refs}\n:::\n" if cfg.get("references", True) else ""
    plain, wrapped = "\n".join(plain) + "\n" + tail, "\n".join(wrapped) + "\n" + tail
    if cfg.get("figure_pdf_to_png"):       # the page shows PNGs; block sources keep the PDF
        fix = lambda s: re.sub(r"\(fig/([^)]*)\.pdf\)", r"(fig/\1.png)", s)
        plain, wrapped = fix(plain), fix(wrapped)
    return table, plain, wrapped


def pandoc(args, text, cwd):
    r = subprocess.run(["pandoc", *args], input=text, capture_output=True,
                       text=True, cwd=cwd)
    if r.returncode:
        raise SystemExit(f"pandoc failed:\n{r.stderr}")
    return r.stdout


def check_wrapping(plain, wrapped, cwd):
    """The wrappers must not change the document: compare the two ASTs."""
    with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
        f.write(STRIP_LUA)
    try:
        a = json.loads(pandoc(["-f", "markdown", "-t", "json"], plain, cwd))
        b = json.loads(pandoc(["-f", "markdown", "-t", "json", "-L", f.name], wrapped, cwd))
    finally:
        os.unlink(f.name)
    if a["blocks"] == b["blocks"]:
        return
    for i, (x, y) in enumerate(zip(a["blocks"], b["blocks"])):
        if x != y:
            raise SystemExit(f"block wrapping changed top-level block {i}:\n"
                             f"  plain:   {json.dumps(x)[:300]}\n"
                             f"  wrapped: {json.dumps(y)[:300]}")
    raise SystemExit(f"block wrapping changed the block count "
                     f"({len(a['blocks'])} vs {len(b['blocks'])})")


def bib_keys(path):
    """[(key, "Author Year")] from a BibTeX file, for the page's citation list."""
    out = []
    for e in re.split(r"\n(?=@)", "\n" + Path(path).read_text()):
        m = re.match(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", e)
        if not m or m.group(1).lower() in ("comment", "string", "preamble"):
            continue
        au = re.search(r"\bauthor\s*=\s*[{\"](.+?)[}\"]\s*,?\s*\n", e, re.S | re.I)
        yr = re.search(r"\b(?:year|date)\s*=\s*[{\"]?(\d{4})", e, re.I)
        names = [a.strip() for a in au.group(1).split(" and ")] if au else []
        fam = [re.sub(r"[{}\\]", "", a.split(",")[0] if "," in a else (a.split() or [""])[-1])
               for a in names]
        who = (fam[0] + " et al." if len(fam) > 2 else " and ".join(fam))
        out.append((m.group(2), f"{who} {yr.group(1) if yr else ''}".strip()))
    return sorted(out, key=lambda kl: (kl[1].lower(), kl[0]))


def page_api(cfg):
    """API base as the page sees it: a path when it is on the page's own host."""
    from urllib.parse import urlparse
    api, url = urlparse(cfg["api"]), urlparse(cfg["url"])
    return api.path if api.netloc == url.netloc else cfg["api"]


def render(cfg, root, out, rnd, base):
    """Write the round's page into ``out`` from the checkout at ``root``."""
    out = Path(out)
    paper_dir = Path(root) / cfg["dir"]
    table, plain, wrapped = prepare(cfg, root)
    check_wrapping(plain, wrapped, paper_dir)

    if cfg.get("build"):
        subprocess.run(cfg["build"], shell=True, check=True, cwd=paper_dir)
    out.mkdir(parents=True, exist_ok=True)
    for pat in cfg.get("assets", []):
        for f in glob.glob(str(paper_dir / pat)):
            dst = out / Path(f).relative_to(paper_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
    pdfs = cfg.get("pdf") or []
    for p in [pdfs] if isinstance(pdfs, dict) else pdfs:
        shutil.copy2(paper_dir / p["from"], out / p["to"])
    css = []
    if cfg.get("css"):
        shutil.copy2(paper_dir / cfg["css"], out / "style.css")
        css = ["-c", "style.css"]
    for f in ("review.css", "review.js"):
        shutil.copy2(ASSETS / f, out / f)

    meta = {"paper": cfg["id"], "round": rnd, "base": base, "api": page_api(cfg),
            "blocks": table,
            "cites": bib_keys(paper_dir / cfg["bibliography"]) if cfg.get("bibliography") else []}
    # Content hashes in the asset URLs: Cloudflare caches .js/.css for hours.
    ver = {f: sha((ASSETS / f).read_text()) for f in ("review.css", "review.js")}
    tmp = Path(tempfile.mkdtemp(prefix="coauthors-"))
    try:
        (tmp / "head.html").write_text(
            '<meta name="robots" content="noindex, nofollow" />\n'
            '<meta name="referrer" content="no-referrer" />\n'
            # stored theme before first paint; review.js offers the menu
            "<script>try{var t=localStorage.getItem('coauthors:theme');"
            "if(/^(light|sepia|dark|black)$/.test(t))"
            "document.documentElement.dataset.theme=t}catch(e){}</script>\n"
            f'<link rel="stylesheet" href="review.css?v={ver["review.css"]}" />\n')
        (tmp / "tail.html").write_text(
            '<script type="application/json" id="rb-meta">'
            + json.dumps(meta).replace("</", "<\\/")
            + f'</script>\n<script src="review.js?v={ver["review.js"]}"></script>\n')
        args = ["-f", "markdown", "-s", "--mathml", *css,
                "--include-in-header", str(tmp / "head.html"),
                "--include-after-body", str(tmp / "tail.html"),
                "--metadata", f"pagetitle={cfg['title']}", "-V", "lang=en",
                "-o", str(out / "index.html")]
        if cfg.get("bibliography"):
            args += ["--citeproc", f"--bibliography={cfg['bibliography']}"]
        if cfg.get("banner"):
            args += ["--include-before-body", str(paper_dir / cfg["banner"])]
        elif cfg.get("banner_html"):        # kept in the paper's toml, not in the paper repo
            from datetime import date
            (tmp / "banner.html").write_text(
                cfg["banner_html"].replace("{date}", date.today().isoformat())
                                  .replace("{round}", str(rnd)))
            args += ["--include-before-body", str(tmp / "banner.html")]
        pandoc(args, wrapped, paper_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return table
