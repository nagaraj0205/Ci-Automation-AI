#!/usr/bin/env python3
"""
Checks index.html before we deploy it.

Run it the same way on your laptop or in the pipeline:

    python check-html.py index.html

Why it exists:
    The old check was just `grep -q "SITE-OK"`. That only proves one word is
    somewhere in the file. A file containing nothing but that word would pass.
"""

import sys

MARKER = "SITE-OK"

# Tags we insist on seeing in a real page.
SKELETON = ["<!DOCTYPE html>", "<title>", "</head>", "</body>", "</html>"]

# Tags we count. If a page opens 3 <h2> it must close 3 </h2>.
PAIRED = ["html", "head", "body", "div", "h1", "h2", "h3", "p",
          "ul", "ol", "li", "pre", "code", "table", "tr", "td"]


def main(path):
    problems = []

    # ---- check 1: the file exists and has something in it ----
    try:
        text = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"FAIL  {path} does not exist")
        return 1

    if not text.strip():
        problems.append("the file is empty")

    # ---- check 2: the marker appears EXACTLY ONCE ----
    # Not "at least once". If the word also shows up in ordinary page text,
    # then deleting the real marker still leaves matches behind, and the
    # health check can never fail. That was the original bug.
    found = text.count(MARKER)
    if found != 1:
        problems.append(f"'{MARKER}' appears {found} times, it must appear exactly 1 time")

    # ---- check 3: the page skeleton is there ----
    for piece in SKELETON:
        if piece not in text:
            problems.append(f"missing {piece}")

    # ---- check 4: every tag that opens also closes ----
    for tag in PAIRED:
        opened = text.count(f"<{tag}>") + text.count(f"<{tag} ")
        closed = text.count(f"</{tag}>")
        if opened != closed:
            problems.append(f"<{tag}> opens {opened} times but closes {closed} times")

    # ---- say what happened ----
    if problems:
        print(f"FAIL  {path}")
        for p in problems:
            print(f"        - {p}")
        return 1

    print(f"OK    {path} - {len(text)} bytes, 1 '{MARKER}', all tags closed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "index.html"))
