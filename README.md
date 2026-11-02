# Coauthors

Round-based co-author review of pandoc-markdown papers. Co-authors read
the draft on a private web page and add comments and suggested edits
there, much like Word's comments and tracked changes. The owner accepts
or rejects each suggestion on the same page. Accepted edits become one
commit on a branch of the paper repository, which the owner merges
like any other branch. Discussion stays in the comment store and in
`archive/` (kept out of git); it never enters the paper repository.

## Design

```
 Papers @ commit C ──render──▶ draft page (secret URL) ◀── co-authors
        │                          │  comments, suggestions, replies
        │                          ▼
        │                    review API (sqlite)  ◀── owner: accept / reject
        │                          │
        └──── branch coauthors/<paper>-rN ◀──apply── accepted suggestions
                     │  one commit, Co-authored-by trailers
                     ▼
               git merge into main (git resolves against later work)
```

- **A round is pinned to one commit.** `publish` renders from a
  temporary worktree at a commit of the paper repository, never from
  the working tree. Every suggestion therefore refers to known source
  lines, and `apply` replaces exactly those lines at that commit. Work
  done on `main` during the round is reconciled by an ordinary
  `git merge`.
- **Block anchoring.** Each top-level block of the markdown
  (paragraph, list, table with caption, heading, display equation) is
  wrapped in a fenced div `::: {#rb-N .rb}` before rendering; the
  page carries each block's file, line range and markdown source.
  Before publishing, the pandoc AST of the wrapped document, with the
  wrappers removed, must equal the AST of the plain document, so the
  wrappers cannot change what co-authors see. `<!-- -->` notes are
  stripped as before; lines keep their numbers.
- **Suggestions are edits of a block's markdown.** The editor shows the
  block's source, and the change is displayed as a word-level tracked
  change. Co-authors therefore see `$\text{CH}_4$`, not rendered
  maths, when they edit. In return, an accepted edit is exact and
  never has to be mapped back from HTML. Comments can be on a whole
  block or on a selected passage, which is highlighted.
- **One accepted suggestion per block per round.** The server refuses a
  second one. To combine two suggestions, the owner writes a merged
  suggestion and accepts only that.
- **Applying.** Prose blocks are refilled at 72 columns, and a line
  never starts with a token pandoc would read as block syntax. Tables,
  lists and similar blocks are inserted as written. Internal notes
  inside a replaced block are kept above it, with a warning. The commit
  lists every edit with its file, lines, item number and author.
- **Deleting.** 🗑 on a block suggests deleting it (a suggestion with
  empty text). `apply` removes the block and one blank line after it.
- **Labels.** Before committing, `apply` collects the labels the
  accepted edits remove (`{#id}`, `\label{id}`, and the automatic ids
  of edited headings) and searches the section files for references to
  them (`@id`, `[-@id]`, `](#id)`, `\ref{id}`). If any remain, it stops
  and lists them; `apply --force` commits anyway, with a warning. A
  label that an edit moves to another block is not reported.

### Access

The random path component keeps the *page* unlisted. It is a bearer
secret: anyone with the URL can read the draft. Writing needs more:

- Each co-author gets a **personal key** per paper, sent as
  `…/#k=KEY`. The fragment never reaches the server, its logs or a
  `Referer`. The page moves the key into the browser's localStorage and
  removes it from the address bar, so a copied URL does not carry it.
- The server stores only SHA-256 digests of keys. Names are bound to
  keys on the server, so nobody can comment as someone else. A key can
  be revoked with `coauthors authors PAPER --revoke ID`.
- Without a key the page is read-only and shows no comments.
- Pages carry `noindex, nofollow` and `Referrer-Policy: no-referrer`.

### Side rail

Where the screen leaves room left of the text (from 1280px wide, or
1560px with the comments panel open), a rail shows the contents, with
the current section marked and open comments counted per section; a
Markdown cheat sheet for editing; and the citation keys of the paper's
bibliography. Clicking a key inserts `[@key]` into the open editor, or
copies it. On narrower screens the cheat sheet is a collapsible part of
the edit box. The edit box itself widens up to 760px, so 72-column
source lines fit.

### Presence

As in Word, each co-author sees where the others are: initials in the
right margin of the block they are reading (outlined) or editing
(filled, ✎), and a coloured dot per person in the contents rail. An
open editor warns when someone else is editing the same block. The
page reports its position every 10 s and on scrolling; *reading* is the
block a third of the way down the window. Positions are kept in the
server's memory for 30 s and never written to the database or a log.
Leaving or hiding the tab removes one at once.

### Themes

The toolbar's Theme menu offers Auto (follows the system), Light,
Sepia, Dark and Black. The choice is kept in the browser and applies to
every draft on the site; a small script in the page head applies it
before the first paint. Light and Dark use the draft stylesheet's own
colours; Sepia and Black are defined in `assets/review.css`.

If the draft itself must not leak through a forwarded URL, add an access
list in nginx-proxy-manager for the `/progress/` location (HTTP basic
auth with one shared password). The review layer is unaffected.

## Workflow

```sh
ln -s "$PWD/bin/coauthors" ~/.local/bin/        # once

cp papers/example.toml papers/my-paper.toml   # then edit it (kept out of git)
# commit the manuscript (and banner) in the paper repository, then:
coauthors publish my-paper                      # round 1 at the paper repo's HEAD
coauthors invite  my-paper "A. Coauthor" --email <address>
coauthors status  my-paper                      # counts per kind/status
# ... co-authors comment and suggest; you accept/reject on the page ...
coauthors close   my-paper                      # read-only from now on
coauthors apply   my-paper                      # -> branch coauthors/my-paper-r1
git -C <paper repo> merge coauthors/my-paper-r1
coauthors archive my-paper                      # archive/<paper>/round-01.json (local)
coauthors publish my-paper --new-round          # round 2 at the new HEAD
```

`publish` without `--new-round` re-renders the current round at its own
commit (for example after changing `review.js`). Moving a round to a
different commit would break the anchors, so the server refuses it.
`publish --new-round` closes the previous round. If that round still
has undecided suggestions, it stops unless you add `--force`.
`publish --dry-run DIR` renders into `DIR` without uploading or opening
a round.

The PDF is made by the paper's `build` command in the same worktree, so
the PDF and the HTML always come from the same commit.

## Paper template

`PAPER_TEMPLATE.md` is an editable, discipline-neutral manuscript
starter for co-authors. Copy it into the paper repository, replace the
bracketed prompts with the paper's content, and split it into the
section files that a paper's `sections` lists.

## Files

| Path | Role |
|---|---|
| `PAPER_TEMPLATE.md` | manuscript starter (see above) |
| `papers/<id>.toml` | per-paper settings (local; `papers/example.toml` is the template): repo, sections, assets, PDFs (one or a list), banner (a file in the paper repo, or `banner_html` with `{date}` and `{round}`), destination, API |
| `coauthors/` | the `coauthors` command (Python ≥ 3.11, stdlib only; pandoc, git, rsync) |
| `assets/review.js`, `assets/review.css` | the review layer on the page; no dependencies |
| `server/review_server.py` | the API, stdlib `http.server` + sqlite |
| `server/compose.review.yml`, `server/www-default.conf` | container and nginx configuration |
| `archive/<id>/round-NN.json` | closed rounds' discussions (written by `archive`; local) |

## Server set-up

The API is one stdlib Python file and an SQLite database. A sketch with
Docker Compose and an nginx container that serves the drafts:

| On the server | From |
|---|---|
| `review/review_server.py` | `server/review_server.py` |
| `review/www-default.conf` | `server/www-default.conf` |
| `review/data/review.sqlite` | created by the server |
| `review` service, extra `www` volume | `server/compose.review.yml` |

The web server forwards `/review-api/` to the API
(`server/www-default.conf`), so page and API share an origin. Drafts go
under an unguessable path, for example `/progress/<random>/`, which a
paper's `dest` and `url` name.

To add a paper, create its owner key once and keep it out of git:

```sh
docker exec <review-container> python3 /srv/review_server.py \
  bootstrap --db /data/review.sqlite --paper PAPER \
  --name "Your Name" --email you@example.org \
  > ~/.config/coauthors/PAPER.key
chmod 600 ~/.config/coauthors/PAPER.key
```

Open your own link once (`…/#k=<owner key>`) so your browser can
decide suggestions. Some proxies (Cloudflare among them) reject
Python's default user agent, so the CLI sends `User-Agent: coauthors/1`.

`review.sqlite` holds the only copy of any round that has not been
archived. Most of it usually sits in the WAL, so copying the file is not
a backup; use `sqlite3 review.sqlite ".backup copy.sqlite"` or Python's
`sqlite3` backup API.

## Local preview

```sh
python3 server/review_server.py bootstrap --db /tmp/r.sqlite --paper demo --name Me
python3 server/review_server.py serve --db /tmp/r.sqlite --port 8765 --static /tmp/www
```

Point a copy of a paper config at `http://127.0.0.1:8765/...`, and set
`dest` to a directory under `/tmp/www`.

## Limits

- Suggestions are made per block. Moving text between blocks needs one
  suggestion per block, or a comment.
- Material outside the section files (banner, figure files, tables
  produced by scripts) can be commented on but not edited.
- Footnote and link-reference definitions are not wrapped. They can be
  commented on through the paragraph that cites them.
- Quote highlighting uses the CSS Custom Highlight API. In older
  browsers the comment still works, without the highlight.
