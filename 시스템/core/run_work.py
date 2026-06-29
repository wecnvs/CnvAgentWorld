# -*- coding: utf-8 -*-
"""작업 디스패치 러너 — 매니저 tick과 분리된 별도 프로세스에서 실제 작업을 실행한다.

`room_manager._dispatch_work_plan`이 프로덕션에서 이 모듈을 detached 서브프로세스로 띄운다:
    python -m core.run_work <dispatch_file.json>
이렇게 해서 engine.work()의 분단위 블로킹이 매니저 claim/tick을 점유하지 않게 한다(설계_대화작업분리 Phase A).
이 프로세스는 별도 세션(start_new_session)이라 서버가 죽어도 살아서 작업을 마치고 결과를 release_queue에 남긴다.

dispatch 파일 스키마(_dispatch_work_plan이 기록):
  { space, plan_id, wake, worker, objective_for_work, effect_id, context }
실행: room_manager._execute_work_plan(동기)을 이 프로세스에서 호출 → 로직 중복 없음.
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
        sys.stderr.write(f"run_work: dispatch 파일 로드 실패 {dispatch_file}: {exc}\n")
        return 2
    space = params.get("space") or ""
    plan_id = params.get("plan_id") or ""
    if not space or not plan_id:
        sys.stderr.write("run_work: space/plan_id 누락\n")
        return 2

    # 늦은 임포트(서브프로세스 시작 비용 최소화 + 순환참조 회피)
    from . import room_manager, work_plan

    try:
        room_manager._execute_work_plan(
            space,
            plan_id=plan_id,
            wake=params.get("wake") or "",
            worker=params.get("worker") or "",
            objective_for_work=params.get("objective_for_work") or "",
            effect_id=params.get("effect_id") or "",
            context=params.get("context") or {},
            claim=None,
            handoff_context_pack={},
            turn_handoff_pack={},
        )
        result = 0
    except Exception as exc:
        sys.stderr.write(f"run_work: 작업 실행 실패 plan={plan_id}: {type(exc).__name__}: {exc}\n")
        try:
            work_plan.mark_finished(space, plan_id, state=work_plan.ERROR, note=f"run_work 실패: {str(exc)[:200]}")
        except Exception:
            pass
        result = 1
    finally:
        # 작업이 끝났으면 완료분(pending release)을 곧바로 방으로 회수·공개한다(설계_대화작업분리 Phase B).
        # 종전엔 reflow가 '대표 턴' 또는 '외부 폴러'에만 의존해, 작업이 끝나도 결과가 방에 늦게/안 올라왔다
        # (실증 2026-06-29: win 이식 완료 후 ~11분 침묵, 대표가 직접 prod해야 공개됨).
        # reflow_safe는 예외를 던지지 않고, 세대펜스+중복방지(already_committed)로 stale/이중공개를 막는다.
        try:
            room_manager.reflow_safe(space)
        except Exception:
            pass
        # 디스패치 파일 정리(멱등 — 없어도 무방)
        try:
            path.unlink()
        except Exception:
            pass
    return result


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        sys.stderr.write("용법: python -m core.run_work <dispatch_file.json>\n")
        return 2
    return _run(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
