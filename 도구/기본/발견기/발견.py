#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""발견기 — frontmatter의 description을 스캔해 요청에 맞는 자원 후보를 추린다.
태그 없음. 설명(description)으로 찾는다. 카탈로그는 매번 라이브 스캔(드리프트 없음).

용법: python3 발견.py "<찾는 내용>" [--type skill|knowledge|tool|asset|all] [--top 5]
출력: 점수순 후보 목록(이름·종류·설명·경로[, 도구는 호출법]). 하나가 아니라 후보 '여러 개'를 준다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]   # 도구/기본/발견기/발견.py → 루트폴더
sys.path.insert(0, str(ROOT / "시스템"))
from core import discovery  # noqa: E402

def main():
    args = sys.argv[1:]
    if not args:
        print('용법: python3 발견.py "<찾는 내용>" [--type skill|knowledge|tool|asset|all] [--top N]')
        return
    typ, top, query_parts, i = "all", 5, [], 0
    while i < len(args):
        if args[i] == "--type" and i + 1 < len(args):
            typ = args[i + 1]; i += 2
        elif args[i] == "--top" and i + 1 < len(args):
            top = int(args[i + 1]); i += 2
        else:
            query_parts.append(args[i]); i += 1
    query = " ".join(query_parts)
    try:
        hits = discovery.find(query, typ, top)
    except ValueError as exc:
        print(str(exc))
        return
    print(discovery.render_cli(query, hits))

if __name__ == "__main__":
    main()
