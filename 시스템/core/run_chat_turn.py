# -*- coding: utf-8 -*-
"""채팅 턴 디스패치 러너 — 매니저 tick과 분리된 별도 프로세스에서 단일 pass 채팅 턴을 실행한다.

`room_manager._dispatch_chat_turn`이 프로덕션에서 이 모듈을 detached 서브프로세스로 띄운다:
    python -m core.run_chat_turn <dispatch_file.json>
이렇게 해서 에이전트 응답 생성(engine 호출, 분단위)이 매니저 claim/tick을 점유하지 않는다
(설계_대화작업분리 §9.3 갭1 — 티키타카 비동기화). run_work와 동형 패턴.

흐름: _run_agent_candidate(claim=None)로 응답을 후보 큐(pending_synthesis)에 저장 →
publish_ready_chat_candidates가 유효 claim으로 결정적 공개 → tick(auto_continue)으로 대화 연속.
직접 발행은 하지 않는다(발행 원장 단일소유 불변식 — 발행은 claim 보유자만).

dispatch 파일 스키마(_dispatch_chat_turn이 기록):
  { space, wake, message, reason, context(chat_chain_depth 포함), turn_id }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _run(dispatch_file: str) -> int:
    path = Path(dispatch_file)
    try:
        params = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write(f"run_chat_turn: dispatch 파일 로드 실패 {dispatch_file}: {exc}\n")
        return 2
    space = params.get("space") or ""
    wake = params.get("wake") or ""
    if not space or not wake:
        sys.stderr.write("run_chat_turn: space/wake 누락\n")
        return 2

    # 늦은 임포트(서브프로세스 시작 비용 최소화 + 순환참조 회피)
    from . import room_manager

    context = params.get("context") or {}
    try:
        depth = int(context.get("chat_chain_depth") or 0)
    except Exception:
        depth = 0
    result = 0
    try:
        room_manager._run_agent_candidate(
            space,
            wake,
            params.get("message") or "",
            None,  # claim 없음 — 디스패치한 tick의 claim은 이미 풀렸다(세대 펜스가 staleness를 지킨다)
            context,
            turn_id=params.get("turn_id") or "",
            join_policy="single_pass",
            presentation_mode="direct_publish",
            reason=params.get("reason") or "",
        )
    except Exception as exc:
        sys.stderr.write(f"run_chat_turn: 턴 실패 {wake}: {type(exc).__name__}: {exc}\n")
        try:
            room_manager.record_chat_turn_failure(
                space, wake=wake, context=context,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        except Exception:
            pass
        result = 1
    finally:
        # 디스패치 파일 정리(멱등) — publish 전에 지워 in-flight 가드가 다음 턴을 막지 않게 한다.
        try:
            path.unlink()
        except Exception:
            pass
        # 응답 후보를 곧바로 공개(결정적, LLM 없음). claim 경합이면 다음 tick/reflow가 회수한다.
        try:
            room_manager.publish_ready_chat_candidates(space)
        except Exception:
            pass
        if result == 0:
            # 대화 연속 — 공개된 답 위에서 매니저가 다음 흐름(호명된 멤버 pass·검토 연결·stop)을 판단.
            # chat_chain_depth 증가로 detached 연속이 CHAT_CHAIN_MAX_DEPTH에서 수렴한다(폭주 방지).
            try:
                room_manager.tick(
                    space,
                    f"{wake}의 채팅 턴 응답이 방에 공개됨 — 흐름을 보고 대화를 잇거나 멈춘다",
                    {**context, "chat_chain_depth": depth + 1},
                    auto_continue=True,
                )
            except Exception:
                pass
    return result


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        sys.stderr.write("용법: python -m core.run_chat_turn <dispatch_file.json>\n")
        return 2
    return _run(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
