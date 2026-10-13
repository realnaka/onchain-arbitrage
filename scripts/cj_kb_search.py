#!/usr/bin/env python3
"""Search the archived CJ handbook knowledge base.

Searches the 502-tweet dataset under
docs/knowledge/cj-defi-handbook/raw/cj_arbitrage_lp_tweets.json by default,
or the extracted book text with --book. Read-only: it never writes or
modifies the knowledge base.

Examples:

    scripts/cj_kb_search.py 资金费率
    scripts/cj_kb_search.py --tag 衍生品套利 --min-likes 100
    scripts/cj_kb_search.py --book 平仓深度
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

KB_DIR = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "knowledge"
    / "cj-defi-handbook"
).resolve()
TWEETS_PATH = KB_DIR / "raw" / "cj_arbitrage_lp_tweets.json"
BOOK_PATH = KB_DIR / "raw" / "handbook-book-text.txt"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search the archived CJ DeFi handbook (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("query", nargs="*", help="关键词或正则，留空表示不过滤")
    parser.add_argument("--tag", action="append", default=[], help="按标签过滤，可重复")
    parser.add_argument("--min-likes", type=int, default=0, help="最低点赞数")
    parser.add_argument("--limit", type=int, default=20, help="最多输出条数")
    parser.add_argument("--chars", type=int, default=320, help="每条正文截断长度")
    parser.add_argument(
        "--regex", action="store_true", help="把关键词当作正则表达式"
    )
    parser.add_argument(
        "--book", action="store_true", help="改为检索电子书纯文本而非推文库"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser.parse_args(argv)


def compile_matcher(terms: list[str], as_regex: bool) -> re.Pattern[str] | None:
    if not terms:
        return None
    joined = "|".join(terms if as_regex else (re.escape(t) for t in terms))
    return re.compile(joined, re.IGNORECASE)


def search_tweets(args: argparse.Namespace) -> int:
    if not TWEETS_PATH.exists():
        print(f"缺少数据文件：{TWEETS_PATH}", file=sys.stderr)
        return 2
    tweets = json.loads(TWEETS_PATH.read_text(encoding="utf-8"))
    matcher = compile_matcher(args.query, args.regex)
    wanted_tags = set(args.tag)

    hits = []
    for tweet in tweets:
        tags = set(tweet.get("tags") or [])
        if wanted_tags and not wanted_tags.issubset(tags):
            continue
        if (tweet.get("likes") or 0) < args.min_likes:
            continue
        if matcher and not matcher.search(tweet.get("text", "")):
            continue
        hits.append(tweet)

    hits.sort(key=lambda t: -(t.get("likes") or 0))
    hits = hits[: args.limit]

    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0

    if not hits:
        print("没有匹配的推文。")
        return 1

    print(f"命中 {len(hits)} 条（按点赞排序，最多 {args.limit} 条）：\n")
    for tweet in hits:
        text = re.sub(r"\s+", " ", tweet.get("text", "")).strip()
        if len(text) > args.chars:
            text = text[: args.chars] + "…"
        print(f"[{tweet.get('likes', 0)} 赞] {tweet.get('date', '')}")
        print(f"  标签: {'/'.join(tweet.get('tags') or [])}")
        print(f"  {tweet.get('url', '')}")
        print(f"  {text}\n")
    return 0


def search_book(args: argparse.Namespace) -> int:
    if not BOOK_PATH.exists():
        print(f"缺少文本文件：{BOOK_PATH}", file=sys.stderr)
        return 2
    lines = BOOK_PATH.read_text(encoding="utf-8").splitlines()
    matcher = compile_matcher(args.query, args.regex)
    if matcher is None:
        print("--book 模式需要至少一个关键词。", file=sys.stderr)
        return 2

    printed = 0
    for index, line in enumerate(lines):
        if not matcher.search(line):
            continue
        context = lines[max(0, index - 2) : index + 3]
        print(f"--- 行 {index + 1} ---")
        print("\n".join(context))
        print()
        printed += 1
        if printed >= args.limit:
            break

    if not printed:
        print("电子书文本中没有匹配内容。")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return search_book(args) if args.book else search_tweets(args)


if __name__ == "__main__":
    raise SystemExit(main())
