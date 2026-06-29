#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CnvAgentWorld CLI — core 도메인 위의 얇은 명령행 래퍼.

도메인 로직은 전부 core/ 에 있다. 여기는 인자 파싱만 한다.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import chat_policy, people, spaces, engine, room_manager  # noqa: E402


def _show(_):
    print("[에이전트]")
    for p in people.list_people():
        print(f"  - {p['토큰']}  engine={p['engine']} model={p['model']}  공간={p['공간']}")
    print("[공간]")
    for s in spaces.list_spaces():
        print(f"  - {s['토큰']}  멤버={[m['토큰'] for m in s['멤버']]}")


def _chat(x):
    if x.direct_diagnostic:
        return print(engine.chat(
            x.person, x.space, x.text, x.requester, x.engine, x.model,
            record_request=False,
            direct_diagnostic=True,
        ))
    return print(engine.chat(
        x.person, x.space, x.text, x.requester, x.engine, x.model,
        client_message_id=x.client_message_id,
    ))


def _space_post(x):
    requester = chat_policy.normalize_requester(x.requester)
    should_run_manager = chat_policy.should_run_space_manager(requester, not x.no_manager)
    return print(room_manager.post(
        x.space,
        x.text,
        requester,
        should_run_manager,
        x.client_message_id,
        manager_requested=should_run_manager,
    ))


def main():
    ap = argparse.ArgumentParser(description="CnvAgentWorld 런타임")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("person-new"); a.add_argument("--name", required=True)
    a.add_argument("--engine", default=None); a.add_argument("--model", default=None)
    a.set_defaults(fn=lambda x: print(people.create_person(x.name, x.engine, x.model)))

    a = sub.add_parser("space-new"); a.add_argument("--name", required=True)
    a.add_argument("--engine", default=None); a.add_argument("--model", default=None)
    a.set_defaults(fn=lambda x: print(spaces.create_space(x.name, x.engine, x.model)))

    a = sub.add_parser("join"); a.add_argument("--person", required=True); a.add_argument("--space", required=True)
    a.set_defaults(fn=lambda x: print("입장:", spaces.join(x.person, x.space)))

    a = sub.add_parser("chat"); a.add_argument("--person", required=True); a.add_argument("--space", required=True)
    a.add_argument("--text", required=True); a.add_argument("--requester", default="대표")
    a.add_argument("--engine", default=None); a.add_argument("--model", default=None)
    a.add_argument("--client-message-id", default=None)
    a.add_argument("--direct-diagnostic", action="store_true", help="공간관리/대화기록을 거치지 않고 특정 좌석 엔진만 점검한다.")
    a.set_defaults(fn=_chat)

    a = sub.add_parser("space-post"); a.add_argument("--space", required=True); a.add_argument("--text", required=True)
    a.add_argument("--requester", default="대표"); a.add_argument("--no-manager", action="store_true", help="호환용 옵션. 공개 CLI 입력에서는 무시되고 공간관리는 항상 실행된다.")
    a.add_argument("--client-message-id", default=None)
    a.set_defaults(fn=_space_post)

    a = sub.add_parser("space-tick"); a.add_argument("--space", required=True)
    a.set_defaults(fn=lambda x: print(room_manager.tick(x.space)))

    a = sub.add_parser("work"); a.add_argument("--person", required=True); a.add_argument("--space", required=True)
    a.add_argument("--task", required=True); a.add_argument("--engine", default=None); a.add_argument("--model", default=None)
    a.set_defaults(fn=lambda x: print(engine.work(x.person, x.space, x.task, x.engine, x.model)))

    a = sub.add_parser("show"); a.set_defaults(fn=_show)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
