"""Split a pandoc-markdown file into top-level blocks with source line ranges.

Every block is later wrapped in a fenced div ``::: {#rb-N .rb}`` so that the
rendered HTML carries the source position of each paragraph, list, table or
heading. Splitting too finely could change the rendering (a list cut in two, a
caption detached from its table), so the rules below err towards merging, and
``render.check_wrapping`` compares the pandoc AST with and without the wrappers
before anything is published.
"""

import re
from dataclasses import dataclass

COMMENT = re.compile(r"<!--.*?-->", re.S)
FENCE = re.compile(r"^(`{3,}|~{3,})")
DIV_OPEN = re.compile(r"^:{3,}\s*[^\s:]")
DIV_CLOSE = re.compile(r"^:{3,}\s*$")
LIST_ITEM = re.compile(r"^([-+*]|\d+[.)]|#[.)]|\(?[a-zA-Z][.)])\s")
# Blocks that must not sit inside a div: note and link-reference definitions.
UNWRAPPABLE = re.compile(r"^(\[\^[^\]]+\]:|\[[^\]]+\]:\s)")


@dataclass
class Block:
    first: int          # 1-based, inclusive
    last: int           # 1-based, inclusive
    lines: list         # source lines, internal comments blanked
    wrap: bool = True

    @property
    def text(self):
        return "\n".join(self.lines)


def strip_comments(text):
    """Remove <!-- --> notes but keep the line count, so line numbers survive."""
    return COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _is_table(line):
    s = line.lstrip()
    return s.startswith("|") or s.startswith("+-") or re.match(r"^-{3,}\s+-{3,}", s)


def _attaches(prev, cur):
    """Should block ``cur`` be merged into the preceding block ``prev``?"""
    head = cur.lines[0]
    if head[:1] in (" ", "\t"):
        return True                                # list continuation, indented code
    if LIST_ITEM.match(head) and LIST_ITEM.match(prev.lines[0]):
        return True                                # loose list
    if head.startswith(("Table:", ": ", ":\t", "~ ")):
        return True                                # caption, definition
    if _is_table(head) and prev.lines[0].startswith(("Table:", ": ")):
        return True                                # caption before the table
    return False


def split(text):
    """Return the blocks of ``text`` (already comment-stripped)."""
    lines = text.split("\n")
    raw, cur, start = [], [], 0
    fence = None
    div_depth = 0
    in_math = False
    yaml = bool(lines) and lines[0].rstrip() == "---"

    for i, line in enumerate(lines):
        if yaml:
            cur.append(line)
            if i > 0 and line.rstrip() in ("---", "..."):
                raw.append(Block(start + 1, i + 1, cur, wrap=False))
                cur, yaml = [], False
            continue
        if fence:
            cur.append(line)
            if line.lstrip().startswith(fence):
                fence = None
            continue
        m = FENCE.match(line)
        if m:
            if not cur:
                start = i
            cur.append(line)
            fence = m.group(1)
            continue
        if DIV_OPEN.match(line):
            div_depth += 1
        elif DIV_CLOSE.match(line) and div_depth:
            div_depth -= 1
            if not cur:
                start = i
            cur.append(line)
            continue
        if line.count("$$") % 2:
            in_math = not in_math
        if line.strip() == "" and not div_depth and not in_math:
            if cur:
                raw.append(Block(start + 1, i, cur))
                cur = []
            continue
        if not cur:
            start = i
        cur.append(line)
    if cur:
        raw.append(Block(start + 1, start + len(cur), cur, wrap=not yaml))

    blocks = []
    for b in raw:
        if blocks and b.wrap and blocks[-1].wrap and _attaches(blocks[-1], b):
            p = blocks[-1]
            gap = b.first - p.last - 1
            p.lines += [""] * gap + b.lines
            p.last = b.last
        else:
            if UNWRAPPABLE.match(b.lines[0]):
                b.wrap = False
            blocks.append(b)
    return blocks


def wrap(blocks, ids):
    """Reassemble ``blocks`` with each wrappable one inside a fenced div."""
    out = []
    for b in blocks:
        if b.wrap:
            out += [f"::::::::: {{#{ids[id(b)]} .rb}}", *b.lines, ":::::::::", ""]
        else:
            out += [*b.lines, ""]
    return "\n".join(out)
