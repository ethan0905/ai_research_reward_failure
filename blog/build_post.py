"""Build blog/post.md from blog/post.src.md by pasting sections of results/tables.md verbatim.

A line `<!-- include: <heading text> -->` is replaced by that heading and everything under it up to
the next heading of the same or higher level. Tables are never retyped by hand.

  .venv/bin/python blog/build_post.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tables = (ROOT / "results" / "tables.md").read_text().splitlines()


def section(title: str) -> list[str]:
    for i, line in enumerate(tables):
        if line.startswith("#") and line.lstrip("#").strip() == title:
            level = len(line) - len(line.lstrip("#"))
            out = [line]
            for nxt in tables[i + 1:]:
                if nxt.startswith("#") and len(nxt) - len(nxt.lstrip("#")) <= level:
                    break
                out.append(nxt)
            while out and not out[-1].strip():
                out.pop()
            return out
    raise SystemExit(f"section not found in results/tables.md: {title!r}")


src = (ROOT / "blog" / "post.src.md").read_text().splitlines()
out = []
for line in src:
    s = line.strip()
    if s.startswith("<!-- include:") and s.endswith("-->"):
        out += ["#" + h if h.startswith("#") else h for h in section(s[len("<!-- include:"):-3].strip())]
    else:
        out.append(line)
(ROOT / "blog" / "post.md").write_text("\n".join(out) + "\n")
print("wrote blog/post.md")
