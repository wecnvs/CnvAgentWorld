# -*- coding: utf-8 -*-
"""공간관리 에이전트 훅과 방 대화 진행."""
from __future__ import annotations

import json
import re
import fcntl
import os
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .paths import SPACES, SYS
from .transcript import record, read, now_iso, state as transcript_state
from . import candidate_queue, case_ledger, chat_result, context_pack, engine, knowledge_ledger, lesson_ledger, manager_claim, orchestration, publish_ledger, release_queue, response_obligation, runtime, skill_smith, space_memory, task_registry, work_plan, work_settings
from .spaces import MANAGER_DIRNAME, PROJECTION_BASELINE_FILENAME
from .paths import PEOPLE
from .codes import split_token

MAX_DECISION_ATTEMPTS = 3
# 체인이 claim을 쥔 동안 들어온 대표 입력을 release 후 재처리하는 redrive 최대 연쇄.
# '실제로 들어온 미처리 입력'에만 반응하므로(허공에 안 돎) 캡을 올려도 빈 루프 비용이 없다.
# 말풍선·에이전트가 늘면 한 처리 동안 더 많은 입력이 쌓일 수 있어 여유 있게 잡는다.
MAX_REDRIVE_CHAIN = 6
# 빠른 연속 입력 누락 방지: 체인이 끝났는데도 아직 답 안 된 대표 입력(open 응답의무)이 남아 있으면
# 그 입력들을 가장 오래된 것부터 하나씩 명시해 매니저를 더 구동한다. 후보 정리 등에 턴을 쓰느라
# 빠르게 온 입력을 '읽기만' 하고 답을 빠뜨린 경우를 닫는 최종 안전망.
# ★ 진짜 런어웨이 방지는 '진전 없음 가드'(open 의무 집합이 줄지 않으면 즉시 멈춤)다 — 이 숫자 캡은
# 보조 상한일 뿐이고, sweep은 '실제로 답 안 된 입력'당 한 번만 도므로 헛돌지 않는다. 따라서 말풍선/
# 에이전트가 늘어 한 번에 여러 입력이 누락될 수 있는 상황에 맞춰 넉넉히 잡는다(실제 누락 수만큼만 비용 발생).
MAX_OBLIGATION_SWEEPS = 8
# 후보 잔류 방지: 체인이 끝나는데도 아직 방에 공개 안 된 pending 후보가 남아 있으면(자동연속이 토론
# 턴을 다 써 버려 못 비웠거나 매니저가 멈춤) 공개/선택/합성/폐기로 비울 때까지 더 구동한다.
# pending 후보 수가 줄지 않으면 멈춘다(무한루프 방지). '실제로 남은 후보'에만 반응하므로 헛돌지 않는다.
MAX_CANDIDATE_DRAINS = 4
# 대표의 새 입력 없이 에이전트끼리 자동으로 협업/토론 턴을 이어갈 최대 연속 횟수.
# 런어웨이(매니저·에이전트 LLM 연쇄 spawn) 방지를 위한 하드 캡 — 위 두 캡과 달리 '추측성' 진행이라
# 비용이 실입력과 무관하게 늘 수 있으므로 보수적으로 둔다. 매니저가 stop하면 즉시 멈춰 대표에게 넘긴다.
# 멤버가 늘면 다자 토론(각자 1턴 + 반응 라운드 + 합성)·협업단계(기획→구현→검수→보완)가 길어져 6으로 둔다.
AUTO_CONTINUE_MAX_TURNS = 6
STATUS_STALE_MS = 30_000
CHAT_AGENT_STALE_MS = 180_000
MAX_PROMPT_SOURCE_CHARS = 4000
MAX_PROMPT_STATUS_FAILURES = 6
MAX_PROMPT_STATUS_RECOVERY = 8
MAX_PARALLEL_PASS_TARGETS = 4
MAX_TASK_CONTROL_TARGETS = 8
PARALLEL_JOIN_POLICIES = {"wait_all", "timeout_then_partial"}
PARALLEL_PRESENTATION_MODES = {"silent_reference", "synthesized_summary"}
MANAGER_ACTIONS = {
    "pass",
    "parallel_pass",
    "select_candidate",
    "synthesize_candidates",
    "publish_each",
    "discard_candidate",
    "cancel_task",
    "revise_task",
    "request_progress",
    "propose_case",
    "propose_skill",
    "update_guide",
    "propose_knowledge",
    "update_summary",
    "launch_app",
    "stop",
}
# 자기성장 캡처 액션(대표 durable 피드백을 실제로 저장 — 거짓 기록 금지)
# propose_skill = 마땅한 스킬이 없을 때 새 스킬을 만들고 그 첫 케이스로 durable 교훈을 담는다.
SELF_GROWTH_ACTIONS = {"propose_case", "propose_skill", "update_guide", "propose_knowledge"}
TASK_CONTROL_ACTIONS = {"cancel_task", "revise_task", "request_progress"}
# 병렬 후보 join 타임아웃 — '천장'이다(timeout_then_partial은 다 끝나면 wait가 일찍 반환).
# 동시 콜드스타트(claude -p ×N)는 CPU/IO 경합으로 개수에 비례해 느려지므로 천장을 동시성 비례로 잡는다.
# (라이브 실증: 고정 20s에선 haiku 3 동시 중 2개가 20s 직전에 잘려 단톡방에 1명만 보였다.)
# 2026-06-29 재실증: 대화가 길어지면(맥락 64건+) opus 후보가 54s(2명) 천장을 넘겨 둘 다 잘려 '응답 0개'로
# 협업이 침묵 실패했다. 각 후보 엔진 타임아웃(300s)과의 괴리가 커서 천장이 응답을 죽인다 — opus·큰 맥락을
# 견디게 천장을 올린다(상한은 둠). 근본적으론 맥락 요약으로 후보 처리시간을 줄이는 게 더 낫다(후속: 요약 배선).
PARALLEL_CANDIDATE_JOIN_TIMEOUT_BASE_SECONDS = 60.0
PARALLEL_CANDIDATE_JOIN_TIMEOUT_PER_TARGET_SECONDS = 30.0
PARALLEL_CANDIDATE_JOIN_TIMEOUT_MAX_SECONDS = 240.0
PARALLEL_CANDIDATE_CANCEL_DRAIN_SECONDS = 5.0
PARALLEL_CANDIDATE_ENGINE_TIMEOUT_SECONDS = 300


def _parallel_join_timeout(num_targets: int) -> float:
    """동시 후보 수에 비례한 join 천장(콜드스타트 경합 반영). 다 끝나면 더 일찍 반환."""
    n = max(1, int(num_targets or 1))
    return min(
        PARALLEL_CANDIDATE_JOIN_TIMEOUT_BASE_SECONDS + PARALLEL_CANDIDATE_JOIN_TIMEOUT_PER_TARGET_SECONDS * n,
        PARALLEL_CANDIDATE_JOIN_TIMEOUT_MAX_SECONDS,
    )
MANAGER_DECISION_JSON_CONTRACT = (
    "## 출력 계약 — 반드시 지킬 것\n"
    "- 전체 응답은 유효한 JSON 객체 하나만 허용된다. 설명, 인사, markdown, 코드블록, 주석을 붙이지 않는다.\n"
    "- JSON 객체 바깥의 글자는 모두 오류이며, 시스템은 재시도한다.\n"
    "- 공개 말풍선으로 대표에게 직접 답하지 말고 action으로만 결정한다.\n"
    "- 최소 형식: "
    '{"action":"pass|parallel_pass|select_candidate|synthesize_candidates|publish_each|discard_candidate|cancel_task|revise_task|request_progress|propose_case|propose_skill|update_guide|propose_knowledge|update_summary|launch_app|stop",'
    '"wake":"멤버 토큰 또는 빈 문자열","message":"전달/합성/지시 메시지 또는 빈 문자열","reason":"한 줄 이유"}\n'
    "- pass는 wake와 message가 필요하다. stop은 wake와 message를 비운다. "
    "parallel_pass는 targets 배열, 후보 정리는 candidate_id/candidate_ids, 작업 제어는 task_id/task_ids를 함께 넣는다.\n"
    "- 후보 공개 선택: 여러 멤버가 각자 한마디씩 한 캐주얼 단톡(인사·잡담·각자 의견)이면 publish_each로 "
    "candidate_ids를 넣어 **각 후보를 그 멤버 말풍선으로 따로** 공개한다(다자 대화·사회자 침묵). "
    "여러 관점을 하나의 답으로 합쳐야 할 때만 synthesize_candidates(합성문은 공간관리 명의가 됨)를 쓴다. "
    "그대로 쓸 답 하나면 select_candidate.\n"
    "- **토론(여러 멤버가 주제로 의견을 주고받게):** 1라운드는 parallel_pass로 각자 의견 → publish_each로 공개. "
    "그 다음 **반응 라운드**를 parallel_pass로 잇는다 — 각 멤버에게 '방금 다른 멤버들이 공개한 의견을 읽고 반박·보강하라'고 시켜 토론을 진전시킨다(1~2회). "
    "토론이 무르익으면 synthesize_candidates로 결론을 정리하거나 stop으로 대표에게 넘긴다. 한 라운드 의견 나열로 끝내지 마라.\n"
    "- **순차 체인(서로 위에 쌓아야 하는 빡빡한 디베이트·설계):** parallel_pass(동시·서로 못 봄) 대신 pass로 한 명씩 이어 깨운다 — A에게 pass → A 답이 방에 공개되면 시스템이 네 턴을 다시 주니 B에게 pass(B는 방에 보이는 A 답을 읽고 그 위에 이어 말한다) → 필요하면 C… 식. 각자가 직전 발언을 보고 쌓으므로 1라운드 병렬에서 나던 중복(둘이 같은 말)을 없앤다. 대신 병렬보다 느리다. **빠른 독립 의견·가벼운 다자 답이면 parallel_pass, 한 명이 깐 논점을 다음이 받아 더 깊이 파야 하면 순차 체인**으로 골라라. 어느 쪽이든 이어지는 턴 수엔 상한이 있다.\n"
    "- propose_case는 대표 피드백/작업 결과를 읽고 '이 스킬의 경우의 수'로 남길 가치가 있다고 네가 판단했을 때만 쓴다(wake/message 비움). "
    'skill(스킬 이름)과 candidate 객체를 넣는다: {"skill":"스킬이름","candidate":{"condition":"어떤 상황","instruction":"그땐 이렇게","polarity":"worked|failed",'
    '"action":"add_case|supersede","routing_kind":"procedural","judgment_rationale":"왜 이렇게 판단했나","source_quote":"근거 발화 요약(개인/회사 식별정보는 일반화)","sensitivity":"public|confidential"}}. '
    "사실/선호(절차 아님)는 case가 아니므로 propose_case 쓰지 말고 아래 라우팅을 따른다. "
    "**action은 보통 add_case다.** supersede(기존 케이스 교체)는 바꿀 기존 case_id를 확실히 알 때만 쓰고 candidate.supersedes에 그 id를 넣는다 — 모르면 add_case로 둔다(시스템이 중복·모순을 정리한다).\n"
    "- **자기성장 라우팅 — 대표가 durable 피드백을 줬을 때 반드시 실제로 저장한다. '기록했다'고 말만 하지 마라:**\n"
    "  · **durable 피드백은 명시 마커('기억해/다음부터/항상/규칙으로')에 한정되지 않는다.** 작업 방식·산출물을 두고 하는 **규정형/교정형 발화**도 durable이다 — '이래야 돼', '저렇게 해야지', '그게 아니라 이렇게', '~하는 게 맞지', '다시 제대로 해', '왜 ~ 안 했어' 처럼 *어떻게 했어야 했는지를 규정*하면 모두 포함된다. 막연한 '좋다/싫다'·단발 잡담만 durable이 아니다.\n"
    "  · **스킬-우선 원칙 (가장 중요 — 어기면 같은 실수가 반복된다):** 방금 **스킬을 써서 한 작업**의 결과를 대표가 교정하면, 그 교정은 *그 스킬이 틀렸다/부족하다*는 신호다. **먼저 그 스킬을 고친 뒤(propose_case)** 고친 스킬로 다시 하게 한다. 스킬을 안 고치고 곧장 pass로 '다시 해'만 시키지 마라 — 그러면 스킬은 그대로라 다음에도 똑같이 틀린다. (저장하면 시스템이 자동으로 그 스킬로 재작업을 이어준다.)\n"
    "  · **성공·토론의 worked 교훈도 케이스로 잡는다(실패·교정만이 아니다 — 이게 자주 새는 곳):** 방금 **작업이 성공**했거나 멤버들이 **토론**하며, *다음에·다른 방에서 그 스킬을 더 잘 쓰게 해줄 비자명한 재사용 교훈*(새 기법·우회·함정 회피·더 안전·빠른 방법)이 나오면 — 실패가 아니어도 그 스킬에 propose_case(polarity=worked)로 남긴다. 예: '원격 GUI에서 확인 버튼을 절대좌표로 찍으면 해상도·언어팩 바뀔 때 깨지니, 캡처로 버튼을 찾아 동적 좌표로 클릭한다'(computer-use 류 스킬). **자명·일상적 성공('그냥 됨')은 케이스화하지 마라** — 다음에 분명히 도움 될 비자명 교훈만. 성공이 케이스 0으로 그냥 흘러가면 같은 시행착오를 다음 사람이 또 겪는다.\n"
    "  · **방을 가리지 않는 일반 절차 교훈**('이 스킬을 이렇게 써라', '이런 요청엔 이렇게 응답·산출하라') → propose_case (스킬 케이스, scope=global → 다른 단톡방에도 전파). **마땅한 스킬이 없으면** propose_skill로 새 스킬을 만들고 그 첫 케이스로 담는다(skill=새 이름, description=발견용 설명, candidate=첫 케이스).\n"
    "  · **오직 이 방에만 한정된** 행동·말투·취향('이 방에선 존댓말로', '환영카드는 파란톤') → update_guide (이 방에만 남는다 — 다른 단톡방엔 전파 안 됨)\n"
    "  · **재사용될 사실·기준**('우리 회사 ~', '배포는 금요일 금지') → propose_knowledge (message=사실 한 줄, knowledge=지식 주제 이름, description=발견용 설명 — 전역 지식 자원으로 졸업돼 발견기가 찾아 참고함)\n"
    "  · **①/② 오분류 주의(전파 여부가 갈린다):** 한 방에서 나온 말이라도 방 무관 일반 규칙이면 update_guide가 아니라 propose_case다. 예: 'html/md로 만들어 달라면 말풍선 미리보기로 보여줘'는 propose_case(미리보기 스킬). update_guide로 보내면 그 방에만 갇혀 다른 단톡방엔 적용되지 않는다.\n"
    "  · **pass vs propose_case 판단:** 교정 없이 '계속/다음 단계' 같은 단순 진행이면 pass. 산출물·방식을 *고쳐 달라*는 신호가 조금이라도 있으면 pass가 아니라 propose_case(없으면 propose_skill)부터다. 헷갈리면 스킬을 고치는 쪽으로 기울여라(놓친 학습 > 잉여 케이스).\n"
    "  · 위 라우팅으로 **실제 저장**한 뒤에만 대표 피드백을 처리 완료로 본다. 저장 없이 '기억했다'는 거짓 완료다.\n"
    "- **앱 실행(launch_app):** 대표가 **'우리 앱(앱 탭에 등록된 앱) 중 X 실행해줘'**(예: revit 실행)라고 하면 launch_app으로 시스템이 그 앱을 **등록된 실행경로(run_app)** 로 띄운다 — app 필드에 앱 이름이나 앱 폴더경로를 넣는다(예: app=\"Revit\" 또는 \"앱/대외비/원격레빗\"). 시스템이 그 앱의 target(원격 윈도우=cu-helper 등)에서 매니페스트 run을 실행하고 pidfile을 기록해 **대시보드 앱 탭에 '● 실행 중'으로 뜬다**(에이전트가 raw 컴퓨터유즈로 더듬는 것과 다르다 — 그건 대시보드가 모른다). 등록 안 된 임의 프로그램은 못 띄운다(앱 탭 등록 앱만). **단순 실행**이면 launch_app으로 끝. **실행 후 그 앱 화면을 조작(예: revit에서 새 프로젝트 만들기)까지** 해야 하면, launch_app으로 띄운 **뒤** pass로 담당 에이전트에게 넘겨 computer-use로 조작하게 한다. wake는 비운다.\n"
    "- **롤링 누적요약(update_summary):** 대화가 길어지면 오래된 맥락은 최근 창(topic_threads·recent) 밖으로 밀려 사라진다. 그 전에 update_summary로 `요약.md`를 갱신해 **누적 맥락을 압축 보존**한다. message에 **요약.md 전체를 대체할 갱신된 누적요약**을 넣는다 — 기존 요약(space_summary_excerpt)을 버리지 말고 **그 위에 최근 진행을 합쳐 다시 정리**한다(목표·합의된 결정·진행상태·미해결 이슈 중심, 시간순 핵심). 자잘한 잡담은 빼고 '이 방이 지금까지 무엇을 향해 어디까지 왔나'가 한눈에 보이게. wake는 비운다. 이건 방에 공개되는 말풍선이 아니라 내부 누적기록이며, 답할 대표 발언이 따로 있으면 요약을 갱신한 뒤 그 턴을 이어서 처리한다. 새 멤버가 합류했거나 토론·작업이 한 매듭 지어졌을 때가 갱신 적기다.\n"
    "- **실패에서 배우기(learning.growth_gap_open_items):** RoomStatusSnapshot의 learning 블록에 state=needs_review 인 열린 gap이 보이면, 그건 최근 **실패/반려/교정이 아직 아무 성장으로도 이어지지 않았다**는 신호다. 같은 유형이 반복될 실패면(스킬 오용·절차 누락 등) **propose_case로 케이스화**하고(방 무관 일반 교훈이면 그대로 전파됨), 재사용 사실이면 propose_knowledge로 남긴다. 일회성 환경 문제(일시 타임아웃 등)라 케이스화가 무의미하면 그냥 두라 — 오래된 gap은 시스템이 자동 정리한다. 단 **같은 gap이 여러 번 보이는데 계속 방치하지 마라** — 그게 성장루프 공회전이다.\n"
)


class StaleManagerClaim(RuntimeError):
    """현재 manager claim이 아닌 오래된 실행 결과다."""


def _load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _read_json_status(path: Path, fallback):
    if not path.exists():
        return {"status": "missing", "data": fallback, "error": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "read_error", "data": fallback, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {"status": "ok", "data": data, "error": ""}


def _read_text_status(path: Path, limit: int = MAX_PROMPT_SOURCE_CHARS) -> dict:
    if not path.exists():
        return {"status": "missing", "text": "", "error": ""}
    try:
        with path.open("r", encoding="utf-8") as f:
            text = f.read(limit + 1)
    except Exception as exc:
        return {"status": "read_error", "text": "", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {
        "status": "ok_truncated" if len(text) > limit else "ok",
        "text": text[:limit],
        "error": "",
    }


def _members_status(space: str) -> dict:
    result = _read_json_status(SPACES / space / "멤버.json", [])
    data = result.get("data")
    if result.get("status") == "ok" and not isinstance(data, list):
        return {**result, "status": "invalid_shape", "data": [], "error": "members json must be a list"}
    return {**result, "data": data if isinstance(data, list) else []}


def _member_tokens(space: str) -> set[str]:
    return {
        str(member.get("토큰") or "").strip()
        for member in _members_status(space).get("data", [])
        if isinstance(member, dict) and str(member.get("토큰") or "").strip()
    }


def _member_aliases(space: str) -> dict:
    """표시이름·코드 → 토큰 별칭맵. worker를 자연어 이름으로 써도 해석되게 한다."""
    aliases: dict = {}
    for member in _members_status(space).get("data", []):
        if not isinstance(member, dict):
            continue
        token = str(member.get("토큰") or "").strip()
        if not token:
            continue
        for key in (member.get("이름"), member.get("코드")):
            key = str(key or "").strip()
            if key and key not in aliases:
                aliases[key] = token
    return aliases


def _append_jsonl(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)   # 관리자 폴더 등 부모가 없을 수 있어 보장한다
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _state_path(space: str) -> Path:
    return SPACES / space / MANAGER_DIRNAME / "상태.json"


def _activity_path(space: str) -> Path:
    return SPACES / space / MANAGER_DIRNAME / "상태이력.jsonl"


def _status_meta_path(space: str) -> Path:
    return SPACES / space / MANAGER_DIRNAME / "상태메타.json"


def _status_lock_path(space: str) -> Path:
    return SPACES / space / MANAGER_DIRNAME / ".status.lock"


def _atomic_write_json(path: Path, data: dict):
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_status_seq(space: str, data: dict) -> int | None:
    try:
        seq = int(data.get("status_seq") or 0)
    except Exception:
        return None
    return seq if seq > 0 else None


def _parse_time(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _staleness_ms(data: dict) -> int | None:
    ts = _parse_time(data.get("status_updated_at") or data.get("시각"))
    if not ts:
        return None
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    return max(0, int((now - ts).total_seconds() * 1000))


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _jsonl_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_event_seq(row: dict, fallback: int = 0) -> int:
    try:
        seq = int(row.get("event_seq") or 0)
    except Exception:
        seq = 0
    return seq if seq > 0 else fallback


def _max_row_event_seq(rows: list[dict]) -> int:
    max_seq = 0
    for idx, row in enumerate(rows, start=1):
        max_seq = max(max_seq, _row_event_seq(row, idx))
    return max_seq


def _event_key(row: dict, fallback: int) -> str:
    seq = _row_event_seq(row, fallback)
    message_id = str(row.get("message_id") or "").strip()
    return f"seq:{seq}" if seq > 0 else f"msg:{message_id}" if message_id else f"idx:{fallback}"


def _seat_projection_baseline_seq(member: dict, seat_dir: Path) -> int:
    candidates = []
    if isinstance(member, dict):
        candidates.extend([
            _as_int(member.get("projection_baseline_event_seq")),
            _as_int(member.get("joined_event_seq")),
        ])
    baseline = _load_json(seat_dir / PROJECTION_BASELINE_FILENAME, {})
    if isinstance(baseline, dict):
        candidates.append(_as_int(baseline.get("baseline_event_seq")))
    return max([0, *candidates])


def _projection_status(space: str, source_event_seq: int) -> dict:
    if source_event_seq <= 0:
        return {
            "projection_lag": 0,
            "projection_tail_lag": 0,
            "projection_missing_count": 0,
            "projection_lag_by_member": [],
            "seat_projection_baselines": [],
            "seat_projection_baseline_count": 0,
        }
    sdir = SPACES / space
    source_rows = _jsonl_rows(sdir / "대화.jsonl")
    members = _load_json(sdir / "멤버.json", [])
    if not isinstance(members, list):
        members = []
    lag_by_member = []
    projection_baselines = []
    max_tail_lag = 0
    max_missing_count = 0
    max_member_lag = 0
    for member in members:
        token = str(member.get("토큰") or "").strip() if isinstance(member, dict) else ""
        if not token:
            continue
        seat_dir = PEOPLE / token / "공간" / space
        seat_path = seat_dir / "대화.jsonl"
        baseline_seq = _seat_projection_baseline_seq(member, seat_dir)
        required_rows = [
            (idx, row)
            for idx, row in enumerate(source_rows, start=1)
            if _row_event_seq(row, idx) > baseline_seq
        ]
        source_keys = {_event_key(row, idx) for idx, row in required_rows}
        seat_rows = _jsonl_rows(seat_path)
        seat_last_seq = _max_row_event_seq(seat_rows)
        expected_seen_seq = max(seat_last_seq, baseline_seq)
        tail_lag = max(0, source_event_seq - expected_seen_seq)
        seat_keys = {_event_key(row, idx) for idx, row in enumerate(seat_rows, start=1)}
        missing_count = max(0, len(source_keys - seat_keys))
        seat_missing = not seat_path.exists()
        member_lag = max(tail_lag, missing_count, 1 if seat_missing else 0)
        max_member_lag = max(max_member_lag, member_lag)
        max_tail_lag = max(max_tail_lag, tail_lag)
        max_missing_count = max(max_missing_count, missing_count)
        if baseline_seq > 0:
            projection_baselines.append({
                "token": token,
                "projection_baseline_event_seq": baseline_seq,
                "projection_required_event_count": len(required_rows),
                "last_event_seq": seat_last_seq,
                "tail_lag": tail_lag,
                "missing_count": missing_count,
                "seat_missing": seat_missing,
                "late_join_baseline": True,
            })
        if member_lag > 0:
            lag_by_member.append({
                "token": token,
                "tail_lag": tail_lag,
                "missing_count": missing_count,
                "last_event_seq": seat_last_seq,
                "seat_missing": seat_missing,
                "projection_baseline_event_seq": baseline_seq,
                "projection_required_event_count": len(required_rows),
                "late_join_baseline": baseline_seq > 0,
            })
    return {
        "projection_lag": max(max_tail_lag, max_missing_count, max_member_lag),
        "projection_tail_lag": max_tail_lag,
        "projection_missing_count": max_missing_count,
        "projection_lag_by_member": lag_by_member[:20],
        "seat_projection_baselines": projection_baselines[:20],
        "seat_projection_baseline_count": len(projection_baselines),
    }


def _label_for(status: str, data: dict) -> str:
    if data.get("label"):
        return str(data["label"])
    if status == "manager_queued":
        return "공간관리 대기"
    if status == "manager_running":
        return "공간관리 읽고 판단 중"
    if status == "manager_retrying":
        return "JSON 재요청 중"
    if status == "agent_running":
        return f"{data.get('current') or '에이전트'} 턴 받음"
    if status == "idle" and data.get("last_action") == "stop":
        return "턴 멈춤"
    if status == "idle" and data.get("last_action") == "pass":
        return "턴 처리 완료"
    if status == "idle" and data.get("last_action") == "lesson_application_missing":
        return "레슨 적용 보고 누락"
    if status == "idle":
        return "대기"
    return status


def _append_activity(space: str, data: dict):
    _append_jsonl(_activity_path(space), data)


def _is_lesson_application_hold(exc: BaseException) -> bool:
    return (
        isinstance(exc, lesson_ledger.LessonLedgerError)
        and str(exc).startswith((
            "lesson_must_apply_without_application",
            "lesson_pack_unavailable_hold",
            "lesson_application_report_invalid",
            "lesson_application_record_failed",
        ))
    )


def _public_error_summary(exc_or_text) -> str:
    if isinstance(exc_or_text, BaseException):
        message = str(exc_or_text).strip()
        if isinstance(exc_or_text, StaleManagerClaim):
            return "StaleManagerClaim: 오래된 공간관리 실행 결과 차단"
        if isinstance(exc_or_text, orchestration.OrchestrationStaleError):
            return f"OrchestrationStaleError: {message[:160]}"
        if isinstance(exc_or_text, lesson_ledger.LessonLedgerError):
            if _is_lesson_application_hold(exc_or_text):
                return message
            return f"LessonLedgerError: {message[:160]}"
        if isinstance(exc_or_text, chat_result.ChatAgentResultError):
            return f"ChatAgentResultError: {message[:240]}"
        if message.startswith(("TimeoutExpired:", "EngineError:", "ValueError:", "StaleManagerClaim:", "OrchestrationStaleError:")):
            return message
        return f"{type(exc_or_text).__name__}: 엔진 또는 훅 실행 실패"
    text = str(exc_or_text or "").strip()
    if text.startswith("(엔진 타임아웃)"):
        return "TimeoutExpired: 엔진 응답 시간 초과"
    if text.startswith("(stderr)"):
        return "EngineError: 엔진 stderr 반환"
    return "EngineError: 에이전트 응답 실패"


def _engine_failure_text(text: str) -> bool:
    value = (text or "").strip()
    return value.startswith("(엔진 타임아웃)") or value.startswith("(stderr)")


def _is_stale_publish_error(exc: BaseException) -> bool:
    return isinstance(exc, publish_ledger.PublishLedgerError) and "intent_stale_guard_failed" in str(exc)


def _safe_record_interaction_evaluation(space: str, **kwargs) -> dict:
    try:
        return lesson_ledger.record_post_interaction_evaluation(space, **kwargs)
    except Exception as exc:
        try:
            _append_activity(space, {
                "상태": "learning_capture_failed",
                "시각": now_iso(),
                "actor": "시스템",
                "label": "사후 평가 기록 실패",
                "detail": f"{type(exc).__name__}: {str(exc)[:160]}",
                **_context_fields(kwargs.get("context")),
            })
        except Exception:
            pass
        return {"record": {}, "duplicate": False, "error": str(exc)}


def _safe_obligation(space: str, action: str, fn) -> dict:
    try:
        return fn()
    except Exception as exc:
        try:
            _append_activity(space, {
                "상태": "response_obligation_failed",
                "시각": now_iso(),
                "actor": "시스템",
                "label": "응답 의무 원장 갱신 실패",
                "detail": f"{action}: {type(exc).__name__}: {str(exc)[:160]}",
            })
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}


def _claim_fields(claim: dict | None) -> dict:
    if not claim:
        return {}
    return {
        "manager_claim_token": claim.get("claim_token", ""),
        "manager_fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
        "lease_expires_at_utc": claim.get("lease_expires_at_utc", ""),
        "manager_redrive_required": bool(claim.get("manager_redrive_required")),
    }


def _context_fields(context: dict | None) -> dict:
    if not context:
        return {}
    return {
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "reply_to_message_id": context.get("reply_to_message_id", ""),
    }


def _coalesced_fields(context: dict | None) -> dict:
    if not context:
        return {}
    out = {}
    if isinstance(context.get("coalesced_redrive_events"), list):
        out["coalesced_redrive_events"] = context.get("coalesced_redrive_events")[-20:]
    if isinstance(context.get("coalesced_pending_inputs"), list):
        out["coalesced_pending_inputs"] = context.get("coalesced_pending_inputs")[-20:]
    return out


def _latest_context(space: str, event_seq=None) -> dict:
    rows = read(space, None)
    target = None
    if event_seq is not None:
        try:
            target_seq = int(event_seq)
        except Exception:
            target_seq = 0
        for row in reversed(rows):
            try:
                if int(row.get("event_seq") or 0) == target_seq:
                    target = row
                    break
            except Exception:
                continue
    if target is None and rows:
        target = rows[-1]
    return orchestration.context_from_message(target, space)


def _existing_client_message(space: str, client_message_id: str | None) -> dict | None:
    if not client_message_id:
        return None
    for row in reversed(read(space, None)):
        if row.get("client_message_id") == client_message_id:
            return row
    return None


def _compact_input_item(row: dict) -> dict:
    return {
        "event_seq": row.get("event_seq"),
        "message_id": row.get("message_id", ""),
        "client_message_id": row.get("client_message_id", ""),
        "intent_id": row.get("intent_id", ""),
        "conversation_thread_id": row.get("conversation_thread_id", ""),
        "room_generation": row.get("room_generation"),
        "text_preview": str(row.get("내용") or row.get("text_preview") or row.get("event") or "")[:160],
        "recorded_at": row.get("recorded_at", row.get("시각", "")),
    }


def _input_items_from_redrive_events(space: str, redrive_events: list[dict]) -> list[dict]:
    if not redrive_events:
        return []
    rows = read(space, None)
    by_seq = {_as_int(row.get("event_seq")): row for row in rows if _as_int(row.get("event_seq"))}
    by_message = {str(row.get("message_id") or ""): row for row in rows if row.get("message_id")}
    out = []
    seen = set()
    for event in redrive_events:
        if not isinstance(event, dict):
            continue
        context = event.get("context") if isinstance(event.get("context"), dict) else {}
        event_seq = _as_int(context.get("source_event_seq") or event.get("event_seq"))
        message_id = str(context.get("source_message_id") or "")
        row = by_seq.get(event_seq) or by_message.get(message_id)
        if row:
            item = _compact_input_item(row)
        else:
            item = {
                "event_seq": event_seq or None,
                "message_id": message_id,
                "client_message_id": "",
                "intent_id": context.get("intent_id", ""),
                "conversation_thread_id": context.get("conversation_thread_id", ""),
                "room_generation": context.get("room_generation"),
                "text_preview": str(event.get("event") or "")[:160],
                "recorded_at": event.get("marked_at_utc", ""),
            }
        key = item.get("message_id") or item.get("event_seq") or item.get("intent_id") or item.get("text_preview")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _rapid_input_snapshot(
    space: str,
    read_until_seq: int,
    latest_event_seq: int,
    *,
    state_data: dict | None = None,
    claim_snapshot: dict | None = None,
) -> dict:
    state_data = state_data or {}
    claim_snapshot = claim_snapshot or {}
    pending = []
    for row in read(space, None):
        if row.get("역할") != "user" or row.get("run_manager_requested") is False:
            continue
        event_seq = _as_int(row.get("event_seq"))
        if event_seq > read_until_seq:
            pending.append(row)
    coalesced_items = []
    coalesced_items.extend(_input_items_from_redrive_events(space, claim_snapshot.get("redrive_events") or []))
    coalesced_items.extend(_input_items_from_redrive_events(space, state_data.get("coalesced_redrive_events") or []))
    coalesced_items.extend([
        item for item in (state_data.get("coalesced_pending_inputs") or [])
        if isinstance(item, dict)
    ])
    by_key = {}
    for item in [_compact_input_item(row) for row in pending] + coalesced_items:
        key = item.get("message_id") or item.get("event_seq") or item.get("intent_id") or item.get("text_preview")
        if key:
            by_key[key] = item
    combined = sorted(
        by_key.values(),
        key=lambda item: (_as_int(item.get("event_seq")), str(item.get("recorded_at") or "")),
    )
    recent = combined[-6:]
    first = combined[0] if combined else {}
    latest = combined[-1] if combined else {}
    return {
        "schema": "RapidInputSnapshot.v1",
        "read_until_event_seq": read_until_seq,
        "latest_event_seq": latest_event_seq,
        "unread_event_count": max(0, latest_event_seq - read_until_seq),
        "pending_input_count": len(combined),
        "coalesced_input_count": len(coalesced_items),
        "first_pending_event_seq": _as_int(first.get("event_seq")) if first else 0,
        "latest_pending_event_seq": _as_int(latest.get("event_seq")) if latest else 0,
        "latest_pending_intent_id": latest.get("intent_id", "") if latest else "",
        "latest_pending_message_id": latest.get("message_id", "") if latest else "",
        "pending_items": recent,
    }


def _release_redrive(space: str, claim: dict, outcome: str) -> tuple[dict, list[dict]]:
    release = manager_claim.release(space, claim, outcome)
    events = [{
        "type": "manager_claim_released" if release.get("released") else "manager_claim_release_rejected",
        "claim_token": claim.get("claim_token", ""),
        "redrive_required": bool(release.get("redrive_required")),
    }]
    if release.get("redrive_required"):
        redrive_context = release.get("redrive_context") or {}
        redrive_events = release.get("redrive_events") or []
        coalesced_pending_inputs = _input_items_from_redrive_events(space, redrive_events)
        _write_state(
            space, "manager_queued",
            event=release.get("redrive_event") or "새 입력 재처리 필요",
            actor="공간관리",
            label="새 입력 재처리 대기",
            read_until_event_seq=release.get("redrive_event_seq"),
            manager_redrive_required=True,
            queue_event_type="manager_redrive_required",
            coalesced_redrive_events=redrive_events,
            coalesced_pending_inputs=coalesced_pending_inputs,
            **_context_fields(redrive_context),
        )
        events.append({
            "type": "manager_redrive_required",
            "event": release.get("redrive_event", ""),
            "event_seq": release.get("redrive_event_seq"),
            "context": redrive_context,
            "redrive_events": redrive_events,
            "coalesced_pending_inputs": coalesced_pending_inputs,
        })
    return release, events


# 안전장치(대표 제안): 작업을 맡은 에이전트가 'request_work'로 실제 작업을 만들지 않고 '하겠습니다'류
# 접수만 하면, 시스템이 그 작업을 강제로 디스패치한다(떠밀기). 매니저가 같은 멤버를 여러 번 떠밀어야
# 겨우 실행하던 비효율(특히 Gemini)을 한 번에 해소한다.
_WORK_ACK_RE = re.compile(
    r"(착수|진행|시작|작성|생성|제작|정리|준비|구현|개발|만들|그려|보완|수정|재작업)\S{0,8}"
    r"(하겠|할게|하겠습니다|드리겠|드릴게|오겠|와서|보겠|가져오겠|진행)"
)
# 매니저 지시가 '작업'을 시킨 것인지(질문/잡담이 아니라) — 작업 동사/산출물 신호.
_WORK_INSTRUCTION_RE = re.compile(
    r"(만들|작성|제작|생성|그려|그림|조사|정리|구현|개발|수정|보완|재작업|문서|파일|html|md|마크다운|"
    r"슬라이드|덱|디자인|코드|이미지|표|차트|배너|카드|보고서|기획안)"
)


def _force_work_dispatch(space, wake, instruction, claim, context, handoff_context_pack, turn_handoff_pack):
    """에이전트가 '하겠습니다'만 하고 작업을 안 만들면, 매니저 지시를 objective로 작업을 강제 생성한다.
    _handle_chat_agent_result(effect_id 멱등)을 재사용 — 중복 디스패치 없음. 실패해도 게시는 유지(best-effort)."""
    synthetic = {
        "schema": "ChatAgentResult.v1",
        "action": "request_work",
        "public_reply": "",
        "work_request": {"objective": str(instruction or "").strip(), "suggested_worker": wake},
    }
    try:
        return _handle_chat_agent_result(
            space, wake, synthetic, claim, context, handoff_context_pack, turn_handoff_pack,
        )
    except Exception as exc:
        _append_activity(space, {
            "상태": "forced_work_dispatch_failed", "시각": now_iso(), "actor": wake, "target": wake,
            "label": "착수-미실행 안전장치 강제 디스패치 실패", "detail": _public_error_summary(exc)[:200],
            **_context_fields(context), **_claim_fields(claim),
        })
        return None


def _run_agent_turn(
    space: str,
    wake: str,
    message: str,
    claim: dict | None = None,
    context: dict | None = None,
    *,
    handoff_context_pack: dict | None = None,
    turn_handoff_pack: dict | None = None,
    reason: str = "",
) -> str:
    seat = PEOPLE / wake / "공간" / space
    if not seat.exists():
        raise ValueError(f"입장 안 됨: {wake} -> {space}")
    nm, cd = split_token(wake)
    if handoff_context_pack is None:
        handoff_context_pack = context_pack.build_context_pack(
            space, mode="chat", event=message, context=context or {}, target_agent=wake
        )
    if turn_handoff_pack is None:
        turn_handoff_pack = context_pack.build_turn_handoff_pack(
            space,
            target_agent=wake,
            manager_message=message,
            reason=reason,
            context=context or {},
            manager_claim_context=claim,
            context_pack=handoff_context_pack,
        )
        context_pack.record_pack_delivery(
            space,
            recipient=wake,
            delivery_type="agent_wake",
            context_pack=handoff_context_pack,
            turn_handoff_pack=turn_handoff_pack,
            manager_claim_context=claim,
        )
    handoff_prompt = context_pack.render_turn_handoff_prompt(handoff_context_pack, turn_handoff_pack)
    # 채팅 턴 타임아웃을 공간 작업정책으로 — 300s 하드코딩 시 opus가 즉답조차 못 올리고 죽는다(wake_failed).
    try:
        _chat_timeout = int(work_settings.resolve_work_settings(space, wake).get("runner_timeout_sec") or 300)
    except Exception:
        _chat_timeout = 300
    reply = engine.run_engine(seat, engine.prompt_with_discovery(message, handoff_prompt), timeout=max(_chat_timeout, 300))
    if _engine_failure_text(reply):
        raise RuntimeError(_public_error_summary(reply))
    if claim is not None and not manager_claim.is_current(space, claim):
        raise StaleManagerClaim("StaleManagerClaim: agent reply arrived after claim changed")
    if orchestration.is_context_stale(space, context):
        raise StaleManagerClaim("StaleManagerClaim: room_generation changed before agent reply publish")
    lesson_audit = lesson_ledger.audit_reply_lesson_applications(
        space,
        content=reply,
        context_pack=handoff_context_pack,
        agent=wake,
        mode="chat",
    )
    reply = lesson_audit.get("content", reply)
    structured = chat_result.extract(reply)
    announce_only = False
    gated_plan_id = ""
    work_routed = False
    if structured:
        routed = _handle_chat_agent_result(
            space,
            wake,
            structured,
            claim,
            context,
            handoff_context_pack,
            turn_handoff_pack,
        )
        if isinstance(routed, dict) and routed.get("plan_gate"):
            # 고위험 작업계획 결재 대기. 반드시 결재 말풍선을 공개한다(불변식 A) — public_reply가
            # 없으면 시스템 기본 결재문을 쓴다. 응답의무는 assigned로 유지(승인/실행 시 종결).
            public = str(structured.get("public_reply") or "").strip() or routed["default_bubble"]
            reply = public
            announce_only = True
            gated_plan_id = routed["plan_id"]
            work_routed = True
        elif routed is not None:
            # 작업은 TaskRegistry로 라우팅됐다. 단, 에이전트가 남긴 공개문(public_reply)이
            # 있으면 방에 말풍선으로 공개해 협업이 보이게 한다(착수 알림·계획 공유).
            # 이 공개는 '착수 알림'이지 최종 응답이 아니므로, 응답의무는 위임(delegated)
            # 상태로 두고 닫지 않는다(작업 완료/공개 시 종결).
            work_routed = True
            public = str(structured.get("public_reply") or "").strip()
            if not public:
                return routed
            reply = public
            announce_only = True
        elif structured.get("public_reply"):
            reply = str(structured.get("public_reply") or "")
    # 안전장치(대표 제안): 작업을 시킨 지시에 에이전트가 'request_work' 없이 '하겠습니다'류 접수만 했으면
    # (말로만, task 미생성) 시스템이 그 작업을 강제 디스패치한다. message=매니저 지시가 작업 신호일 때만.
    if (not work_routed) and message and _WORK_INSTRUCTION_RE.search(message) and _WORK_ACK_RE.search(reply or ""):
        forced = _force_work_dispatch(space, wake, message, claim, context, handoff_context_pack, turn_handoff_pack)
        if forced and not (isinstance(forced, dict) and forced.get("plan_gate")):
            announce_only = True   # 접수 말풍선은 공개하되, 작업은 강제로 진행됨
            _append_activity(space, {
                "상태": "forced_work_dispatch", "시각": now_iso(), "actor": "공간관리", "target": wake,
                "label": "착수-미실행 안전장치: 작업 강제 디스패치",
                "detail": f"{wake} 접수만 함 → 지시를 작업으로 떠밂: {str(message)[:80]}",
                **_context_fields(context), **_claim_fields(claim),
            })
    effect_id = orchestration.effect_id(
        "agent_reply",
        space,
        wake,
        context.get("intent_id") if context else "",
        context.get("source_event_seq") if context else "",
        context.get("source_message_id") if context else "",
    )
    publish_claim = publish_ledger.claim_publish(
        space,
        publish_effect_id=effect_id,
        manager_claim_token=claim.get("claim_token") if claim else "",
        manager_claim_context=claim,
        context=context or {},
        publisher="space_manager",
        speaker=wake,
    )
    if publish_claim.get("already_committed"):
        return reply
    publish_result = publish_ledger.append_public_message(
        space,
        publish_effect_id=effect_id,
        publish_ledger_claim=publish_claim.get("publish_ledger_claim", ""),
        manager_claim_token=claim.get("claim_token") if claim else "",
        manager_claim_context=claim,
        published_message_id=publish_claim.get("published_message_id", ""),
        intent_stale_guard_passed=True,
        speaker_name=nm,
        speaker_code=cd,
        role="assistant",
        content=reply,
        context=context or {},
        extra={
            "context_pack_id": handoff_context_pack.get("context_pack_id", ""),
            "context_pack_checksum": handoff_context_pack.get("context_pack_checksum", ""),
            "lesson_pack_status": (handoff_context_pack.get("lesson_pack") or {}).get("lesson_pack_status", ""),
            "included_lessons": (handoff_context_pack.get("lesson_pack") or {}).get("included_lessons", []),
            "must_apply_lessons": [
                lesson.get("lesson_id", "")
                for lesson in (handoff_context_pack.get("lesson_pack") or {}).get("must_apply", [])
                if lesson.get("lesson_id")
            ],
            "wake_id": turn_handoff_pack.get("wake_id", ""),
            "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
            "turn_handoff_checksum": turn_handoff_pack.get("turn_handoff_checksum", ""),
        },
    )
    published_message_id = (publish_result.get("record") or {}).get("message_id", "")
    # 결재 대기 계획이면 방금 공개한 결재 말풍선을 결재 anchor로 연결한다(대화창 [진행]/[반려] 버튼).
    if gated_plan_id and published_message_id:
        try:
            work_plan.set_approval_message(space, gated_plan_id, published_message_id)
            _update_approval_marker_message(space, gated_plan_id, published_message_id)
        except Exception:
            pass
    if not announce_only:
        _safe_obligation(
            space,
            "answered_by_agent",
            lambda: response_obligation.close_for_context(
                space,
                context,
                outcome="answered",
                actor="공간관리",
                reason=f"{wake} 공개 응답 완료",
                published_message_id=published_message_id,
                responder=wake,
            ),
        )
    if not publish_result.get("duplicate"):
        orchestration.append_effect(space, {
            "effect_id": effect_id,
            "effect_type": "agent_reply_public_append",
            "speaker": wake,
            "publish_ledger_claim": publish_claim.get("publish_ledger_claim", ""),
            "published_message_id": publish_claim.get("published_message_id", ""),
            "context_pack_id": handoff_context_pack.get("context_pack_id", ""),
            "wake_id": turn_handoff_pack.get("wake_id", ""),
            "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
            **_context_fields(context),
        })
    _safe_record_interaction_evaluation(
        space,
        outcome="success",
        context=context or {},
        source_event="agent_reply_published",
        actor=wake,
        target="space",
        publish_effect_id=effect_id,
        published_message_id=(publish_result.get("record") or {}).get("message_id", ""),
        what_worked=["agent reply was published through manager-owned publish ledger"],
        lesson_candidate_needed=False,
        no_lesson_reason="no_failure_or_correction",
    )
    return reply


def _run_agent_candidate(
    space: str,
    wake: str,
    message: str,
    claim: dict | None,
    context: dict | None,
    *,
    turn_id: str,
    join_policy: str,
    presentation_mode: str,
    reason: str = "",
    cancel_event: threading.Event | None = None,
) -> dict:
    seat = PEOPLE / wake / "공간" / space
    if not seat.exists():
        raise ValueError(f"입장 안 됨: {wake} -> {space}")
    agent_context_pack = context_pack.build_context_pack(
        space, mode="chat", event=message, context=context or {}, target_agent=wake
    )
    turn_handoff_pack = context_pack.build_turn_handoff_pack(
        space,
        target_agent=wake,
        manager_message=message,
        reason=reason,
        context=context or {},
        manager_claim_context=claim,
        context_pack=agent_context_pack,
    )
    context_pack.record_pack_delivery(
        space,
        recipient=wake,
        delivery_type="chat_turn_wake" if join_policy == "single_pass" else "parallel_candidate_wake",
        context_pack=agent_context_pack,
        turn_handoff_pack=turn_handoff_pack,
        manager_claim_context=claim,
    )
    if join_policy == "single_pass":
        # detached 단일 pass — 병렬 수집이 아니라 매니저가 이 멤버 한 명에게 시킨 일반 채팅 턴이다.
        # '독립 초안' 문구는 오해(동료 발언 무시)를 부르므로 단일 턴 규칙으로 바꾼다.
        rules_block = (
            "\n\n# 응답 처리 규칙(백그라운드 턴)\n\n"
            "- 네 답은 시스템이 곧바로 네 말풍선으로 방에 공개한다 — 평소처럼 대화 맥락에 이어 답하라.\n"
            "- **매니저 메시지가 '조사/제작/실행' 같은 실제 작업을 맡긴 것이면**: 이 채팅 턴에서 직접 수행하지 말고 "
            "ChatAgentResult.v1 JSON으로 `action=\"request_work\"`와 `work_request.objective`를 반환한다 "
            "— 시스템이 너를 작업자로 **비동기 작업**을 띄운다. `public_reply`에 착수 한마디를 남겨 협업이 보이게 한다.\n"
            "- 직접 작업 폴더를 만들거나 결과를 방에 직접 공개하지 않는다(공개는 시스템이 한다).\n"
        )
    else:
        rules_block = (
            "\n\n# 병렬 후보 응답 규칙\n\n"
            "- 다른 후보의 내용을 보지 못한 독립 초안으로 답한다(동시 수집).\n"
            "- **매니저 메시지가 '조사/제작/실행' 같은 실제 작업을 맡긴 것이면**: 이 채팅 턴에서 직접 수행하지 말고 "
            "ChatAgentResult.v1 JSON으로 `action=\"request_work\"`와 `work_request.objective`(무엇을 조사·산출할지 구체적으로)를 반환한다 "
            "— 시스템이 너를 작업자로 **비동기 작업**을 띄운다. `public_reply`에는 무엇을 맡아 착수하는지 한 줄로 남겨 토론에도 보이게 한다.\n"
            "- **매니저가 '의견/관점'을 물은 것이면**: 작업을 만들지 말고 텍스트 의견(public_reply)으로 답한다.\n"
            "- 직접 작업 폴더를 만들거나 결과를 방에 직접 공개하지 않는다(공개는 사회자가 한다).\n"
        )
    handoff_prompt = (
        context_pack.render_turn_handoff_prompt(agent_context_pack, turn_handoff_pack)
        + rules_block
    )
    cancel_event = cancel_event or threading.Event()

    def _candidate_cancel_requested() -> bool:
        return cancel_event.is_set()

    def _candidate_heartbeat(phase: str, note: str = "") -> None:
        _append_activity(space, {
            "상태": "parallel_candidate_heartbeat",
            "시각": now_iso(),
            "actor": wake,
            "target": "CandidateQueue",
            "label": "병렬 후보 실행 중",
            "detail": note or phase,
            "heartbeat_phase": phase,
            "turn_id": turn_id,
            "context_pack_id": agent_context_pack.get("context_pack_id", ""),
            "wake_id": turn_handoff_pack.get("wake_id", ""),
            "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
            **_context_fields(context),
            **_claim_fields(claim),
        })

    prompt = engine.prompt_with_discovery(message, handoff_prompt)
    if engine.run_engine is getattr(engine, "_ORIGINAL_RUN_ENGINE", None):
        reply = engine.run_engine_polling(
            seat,
            prompt,
            timeout=PARALLEL_CANDIDATE_ENGINE_TIMEOUT_SECONDS,
            cancel_check=_candidate_cancel_requested,
            heartbeat=_candidate_heartbeat,
        )
    else:
        reply = engine.run_engine(
            seat,
            prompt,
            timeout=PARALLEL_CANDIDATE_ENGINE_TIMEOUT_SECONDS,
        )
    if cancel_event.is_set():
        raise RuntimeError("TimeoutExpired: parallel candidate join timeout")
    if str(reply or "").strip().startswith("(엔진 취소됨"):
        raise RuntimeError("TimeoutExpired: parallel candidate join timeout")
    if _engine_failure_text(reply):
        raise RuntimeError(_public_error_summary(reply))
    if claim is not None and not manager_claim.is_current(space, claim):
        raise StaleManagerClaim("StaleManagerClaim: candidate reply arrived after claim changed")
    if orchestration.is_context_stale(space, context):
        raise StaleManagerClaim("StaleManagerClaim: room_generation changed before candidate enqueue")
    lesson_audit = lesson_ledger.audit_reply_lesson_applications(
        space,
        content=reply,
        context_pack=agent_context_pack,
        agent=wake,
        mode="chat",
    )
    reply = lesson_audit.get("content", reply)
    structured = chat_result.extract(reply)
    candidate_reply = reply
    if structured and structured.get("public_reply"):
        candidate_reply = str(structured.get("public_reply") or "")
    if cancel_event.is_set():
        raise RuntimeError("TimeoutExpired: parallel candidate join timeout")
    # 병렬 후보가 작업을 요청하면(request_work/mixed) 단일 pass와 동일하게 '비동기 작업'으로 디스패치한다.
    #  계약(request_work_via_manager / space_manager_task_registry) 이행 — 한 번의 parallel_pass로
    #  각 멤버가 동시에 자기 작업을 띄우고(동시 작업 할당), public_reply는 토론 후보로 남는다.
    #  _dispatch_work_plan은 비동기(detached)라 후보 join 타임아웃을 막지 않는다.
    #  작업 디스패치 실패는 후보(토론)를 죽이지 않는다 — 활동에만 남기고 계속 진행한다.
    if structured and str(structured.get("action") or "").strip() in {"request_work", "mixed"}:
        try:
            routed = _handle_chat_agent_result(
                space, wake, structured, claim, context,
                agent_context_pack, turn_handoff_pack,
            )
            note = (f"결재대기 plan={routed.get('plan_id')}"
                    if isinstance(routed, dict) and routed.get("plan_gate")
                    else (str(routed)[:160] if routed else ""))
            if note:
                _append_activity(space, {
                    "상태": "parallel_candidate_work_dispatched", "시각": now_iso(), "actor": wake,
                    "target": wake, "label": "병렬 후보 작업 디스패치", "detail": note,
                    "turn_id": turn_id, **_context_fields(context), **_claim_fields(claim),
                })
        except Exception as exc:
            _append_activity(space, {
                "상태": "parallel_candidate_work_dispatch_failed", "시각": now_iso(), "actor": wake,
                "target": wake, "label": "병렬 후보 작업 디스패치 실패",
                "detail": _public_error_summary(exc)[:200],
                "turn_id": turn_id, **_context_fields(context), **_claim_fields(claim),
            })
    enqueue = candidate_queue.enqueue_candidate(
        space,
        turn_id=turn_id,
        target_agent=wake,
        manager_message=message,
        reply=candidate_reply,
        context=context or {},
        work_dir=seat,
        context_pack=agent_context_pack,
        turn_handoff_pack=turn_handoff_pack,
        manager_claim_context=claim,
        reason=reason,
        join_policy=join_policy,
        presentation_mode=presentation_mode,
        structured_result=structured,
    )
    _append_activity(space, {
        "상태": "parallel_candidate_ready",
        "시각": now_iso(),
        "actor": wake,
        "target": "CandidateQueue",
        "label": "병렬 후보 응답 저장",
        "detail": str(candidate_reply or "")[:160],
        "candidate_id": (enqueue.get("event") or {}).get("candidate_id", ""),
        "turn_id": turn_id,
        "context_pack_id": agent_context_pack.get("context_pack_id", ""),
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
        **_context_fields(context),
        **_claim_fields(claim),
    })
    return {
        "ok": True,
        "person": wake,
        "reply": candidate_reply,
        "candidate": enqueue.get("event") or {},
        "duplicate": bool(enqueue.get("duplicate")),
        "context_pack_id": agent_context_pack.get("context_pack_id", ""),
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
    }


def _approval_required_path(space: str) -> Path:
    return SPACES / space / "approval_required.json"


def _read_approval_required(space: str) -> dict:
    try:
        path = _approval_required_path(space)
        if not path.exists():
            return {"pending": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"pending": []}
    except Exception:
        return {"pending": []}


def _write_approval_required(space: str, data: dict) -> None:
    try:
        _atomic_write_json(_approval_required_path(space), data)
    except Exception:
        pass


def _with_approval_marker_lock(space: str, fn):
    """approval_required.json 의 read-modify-write 직렬화(flock).

    종전엔 락이 없어 백그라운드 tick 의 _mark 와 대시보드 approve 스레드의 _clear 가 겹치면
    한쪽 갱신이 유실됐다(_atomic_write_json 은 파일 손상만 막지 lost update 는 못 막는다) —
    결재 마커가 남아 이미 승인한 결재 말풍선이 계속 강조되거나, 새 결재가 마커에서 사라졌다.
    """
    lock = SPACES / space / ".approval_required.lock"
    try:
        lock.touch(exist_ok=True)
        with lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    except Exception:
        return fn()  # 락 실패가 마커 갱신 자체를 막으면 안 된다(종전 무락 동작으로 폴백)


def _mark_approval_required(space: str, plan: dict) -> None:
    """대표 결재가 필요한 계획을 마커에 추가한다(대시보드가 결재 말풍선 강조·버튼 렌더).

    highlight_message_id는 결재 말풍선 발행 후 set_plan_approval_message로 채워진다(P5).
    """
    def mutate():
        data = _read_approval_required(space)
        pending = [p for p in (data.get("pending") or []) if p.get("plan_id") != plan.get("plan_id")]
        pending.append({
            "plan_id": plan.get("plan_id", ""),
            "worker": plan.get("worker", ""),
            "requesting_agent": plan.get("requesting_agent", ""),
            "objective": str(plan.get("objective", ""))[:240],
            "approval_reason": plan.get("approval_reason", ""),
            "highlight_message_id": plan.get("approval_message_id", ""),
            "intent_id": plan.get("intent_id", ""),
            "room_generation": plan.get("room_generation"),
            "at": now_iso(),
        })
        _write_approval_required(space, {
            "schema": "ApprovalRequired.v1",
            "needs_representative": bool(pending),
            "pending": pending[-50:],
        })

    _with_approval_marker_lock(space, mutate)


def _clear_approval_required(space: str, plan_id: str) -> None:
    def mutate():
        data = _read_approval_required(space)
        pending = [p for p in (data.get("pending") or []) if p.get("plan_id") != plan_id]
        _write_approval_required(space, {
            "schema": "ApprovalRequired.v1",
            "needs_representative": bool(pending),
            "pending": pending,
        })

    _with_approval_marker_lock(space, mutate)


def _update_approval_marker_message(space: str, plan_id: str, message_id: str) -> None:
    """결재 마커의 highlight_message_id를 결재 말풍선 message_id로 채운다(대화창 버튼 anchor)."""
    def mutate():
        data = _read_approval_required(space)
        changed = False
        for entry in data.get("pending") or []:
            if entry.get("plan_id") == plan_id and entry.get("highlight_message_id") != message_id:
                entry["highlight_message_id"] = message_id
                changed = True
        if changed:
            _write_approval_required(space, data)

    _with_approval_marker_lock(space, mutate)


def read_approval_required(space: str) -> dict:
    """대표 결재 대기 중인 작업계획 목록(라우터/대시보드용)."""
    return _read_approval_required(space)


# 작업 디스패치 정책 (설계_대화작업분리 Phase A/E)
WORK_DISPATCH_ASYNC = True          # 킬스위치: False면 항상 인라인 동기(구 동작)
MAX_IN_FLIGHT_TASKS = 3             # 방별 동시 실행 작업 상한(폭주·비용 억제, 9.2)

# ── 채팅 턴 디스패치 정책 (설계_대화작업분리 §9.3 갭1 해소 — 티키타카 비동기화) ──
# 종전엔 단일 pass 채팅 턴이 manager claim을 쥔 채 engine.run_engine(≥300s)을 동기 실행해,
# 에이전트가 '생각 중'인 동안 대표의 새 메시지가 claim_busy로 튕겨 티키타카가 구조적으로 막혔다
# (실측 manager_redrive_required 16회/일). 이제 pass 턴을 detached 프로세스(core.run_chat_turn)로
# 던지고 claim을 즉시 놓는다. 응답은 후보 큐(pending_synthesis)로 회수돼 publish_ready_chat_candidates
# 가 결정적으로(LLM 없이) 공개한다 — 발행은 여전히 '유효 claim 보유자'가 하므로 발행 원장 단일소유·
# 세대 펜스 불변식이 그대로 유지된다(옛 턴 내용을 새 claim으로 발행하는 것은 후보 경로가 이미 증명).
CHAT_DISPATCH_ASYNC = True          # 킬스위치: False면 항상 인라인 동기(구 동작)
CHAT_TURN_ID_PREFIX = "chatturn"    # 후보 큐에서 '단일 pass 채팅 턴' 후보를 식별하는 turn_id 접두사
CHAT_CHAIN_MAX_DEPTH = 8            # detached 연속(턴 완료→tick→pass→…) 하드캡. 초과 시 인라인(기존 체인 상한으로 수렴)
CHAT_DISPATCH_STALE_SEC = 15 * 60   # 디스패치 파일이 이 나이를 넘으면 자식 사망으로 보고 정리(엔진 상한 300s의 3배)


def _chat_dispatch_dir(space: str) -> Path:
    return SPACES / space / "dispatch_chat"


def _chat_turn_in_flight(space: str, wake: str) -> bool:
    """같은 멤버의 detached 채팅 턴이 이미 도는 중인지(중복 wake 방지)."""
    ddir = _chat_dispatch_dir(space)
    if not ddir.is_dir():
        return False
    for dfile in ddir.glob("*.json"):
        try:
            if time.time() - dfile.stat().st_mtime > CHAT_DISPATCH_STALE_SEC:
                continue
            if (_load_json(dfile, {}) or {}).get("wake") == wake:
                return True
        except Exception:
            continue
    return False


def _dispatch_chat_turn(space: str, *, wake: str, message: str, context: dict | None, reason: str) -> bool:
    """단일 pass 채팅 턴을 detached 프로세스로 디스패치한다. False면 호출자가 인라인으로 폴백.

    _dispatch_work_plan과 같은 패턴: 킬스위치·엔진 몽키패치(테스트)·기록/Popen 실패 시 인라인 폴백이라
    기존 동기-가정 테스트가 무수정으로 유지된다. 성공 시 claim은 이번 tick 끝에서 곧바로 풀리고,
    응답 공개·대화 연속은 자식(run_chat_turn)이 publish_ready_chat_candidates + tick으로 잇는다.
    """
    if not CHAT_DISPATCH_ASYNC:
        return False
    if engine.run_engine is not getattr(engine, "_ORIGINAL_RUN_ENGINE", None):
        return False  # 테스트 몽키패치 → 인라인(결정성 유지)
    context = context or {}
    depth = _as_int(context.get("chat_chain_depth"))
    if depth >= CHAT_CHAIN_MAX_DEPTH:
        return False  # detached 연속 하드캡 초과 → 인라인 체인(기존 AUTO_CONTINUE 상한)으로 수렴
    if _chat_turn_in_flight(space, wake):
        _append_activity(space, {
            "상태": "chat_turn_duplicate_skipped", "시각": now_iso(), "actor": "공간관리", "target": wake,
            "label": "이미 생각 중 — 중복 채팅 턴 생략",
            "detail": f"{wake}의 detached 채팅 턴이 진행 중이라 새로 띄우지 않음",
            **_context_fields(context),
        })
        return True  # 이미 도는 턴이 답을 가져온다 — 중복 기동하지 않음
    turn_id = f"{CHAT_TURN_ID_PREFIX}_{uuid4().hex[:12]}"
    ddir = _chat_dispatch_dir(space)
    try:
        ddir.mkdir(parents=True, exist_ok=True)
        dfile = ddir / f"{turn_id}.json"
        _atomic_write_json(dfile, {
            "schema": "ChatTurnDispatch.v1",
            "space": space, "wake": wake, "message": message,
            "reason": reason, "context": {**context, "chat_chain_depth": depth},
            "turn_id": turn_id, "at": now_iso(),
        })
    except Exception:
        return False  # durable 기록 실패 → 인라인 폴백(턴 유실 방지)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SYS) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "core.run_chat_turn", str(dfile)],
            cwd=str(SYS), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
    except Exception:
        try:
            dfile.unlink()
        except Exception:
            pass
        return False  # Popen 실패 → 인라인 폴백
    _append_activity(space, {
        "상태": "chat_turn_dispatched", "시각": now_iso(), "actor": "공간관리", "target": wake,
        "label": "채팅 턴 비동기 디스패치(방 안 막음)",
        "detail": f"turn={turn_id} pid={proc.pid}", "turn_id": turn_id,
        **_context_fields(context),
    })
    return True


def record_chat_turn_failure(space: str, *, wake: str, context: dict | None, error: str) -> None:
    """detached 채팅 턴 실패를 기록하고 응답의무·방 상태를 자가치유 경로로 되돌린다(wake_failed 대칭)."""
    _append_activity(space, {
        "상태": "chat_turn_failed", "시각": now_iso(), "actor": wake, "target": "CandidateQueue",
        "label": "detached 채팅 턴 실패", "detail": str(error or "")[:200],
        **_context_fields(context),
    })
    _safe_obligation(
        space,
        "reopen_after_chat_turn_failed",
        lambda: response_obligation.reopen_for_context(
            space, context, actor="시스템",
            reason=f"detached 채팅 턴 실패({wake}) — 의무 재개: {str(error or '')[:160]}",
        ),
    )
    try:
        state = _load_json(_state_path(space), {})
        if str(state.get("상태") or "") == "agent_running" and state.get("current") == wake:
            _write_state(space, "idle", last_action="chat_turn_failed", last_target=wake,
                         label="채팅 턴 실패", reason=str(error or "")[:200])
    except Exception:
        pass


def publish_ready_chat_candidates(space: str) -> dict:
    """detached 채팅 턴이 남긴 pending 후보(turn_id=chatturn_*)를 결정적으로(LLM 없이) 공개한다.

    단일 pass 후보는 매니저가 이미 그 멤버에게 답을 시킨 것이라 선택 판단이 필요 없다 —
    세대 펜스 확인 후 그 멤버 말풍선으로 곧바로 공개하고 응답의무를 닫는다(request_work 후보는
    착수 알림만 공개, 의무는 delegated 유지 — _run_agent_turn의 announce_only와 동일 규칙).
    호출처: run_chat_turn(자식) 직후 + _run_tick_chain 진입부 + reflow 백스톱(자식 사망 대비).
    """
    try:
        snap = candidate_queue.snapshot(space)
    except Exception:
        return {"published": 0, "events": []}
    pending = [
        item for item in (snap.get("pending_items") or [])
        if str(item.get("turn_id") or "").startswith(CHAT_TURN_ID_PREFIX)
    ]
    if not pending:
        return {"published": 0, "events": []}
    published = 0
    events: list[dict] = []
    for item in sorted(pending, key=lambda r: _as_int(r.get("source_event_seq"))):
        candidate_id = str(item.get("candidate_id") or "")
        if not candidate_id:
            continue
        try:
            candidate = candidate_queue.get_candidate(space, candidate_id)
        except Exception:
            continue
        if str(candidate.get("state") or "") != "pending_synthesis":
            continue
        context = _candidate_context(candidate)
        if orchestration.is_context_stale(space, context):
            try:
                candidate_queue.supersede_candidates(
                    space, [candidate_id], actor="시스템",
                    reason="세대 변경 — 늦은 채팅 턴 후보 자동 정리", manager_claim_context=None,
                )
            except Exception:
                pass
            events.append({"type": "chat_candidate_stale", "candidate_id": candidate_id})
            continue
        delivery = transcript_state(space)
        claim_result = manager_claim.acquire(
            space, f"chat turn publish: {candidate_id}", delivery.get("last_event_seq"), context,
        )
        claim = claim_result.get("claim") or {}
        if claim_result.get("corrupt") or not claim_result.get("acquired"):
            # 다른 tick이 방을 쥐고 있다 — 그 체인의 진입부/다음 reflow가 이 후보를 회수한다.
            events.append({"type": "chat_candidate_publish_deferred", "candidate_id": candidate_id})
            break
        outcome = "chat_turn_publish_failed"
        try:
            content = str(candidate.get("structured_public_reply") or candidate.get("reply") or "").strip()
            publish_result = _publish_candidate_message(
                space, claim=claim, candidate=candidate, candidates=[candidate],
                content=content, mode="select", reason="단일 pass 채팅 턴 자동 공개",
            )
            candidate_queue.mark_selected(
                space, candidate_id, actor="공간관리", reason="단일 pass 채팅 턴 자동 공개",
                publish_effect_id=publish_result["publish_effect_id"],
                published_message_id=publish_result["published_message_id"],
                event_seq=publish_result.get("event_seq"),
                manager_claim_context=claim,
            )
            orchestration.append_effect(space, {
                "effect_id": publish_result["publish_effect_id"],
                "effect_type": "chat_turn_candidate_public_append",
                "candidate_id": candidate_id,
                "candidate_turn_id": candidate.get("turn_id", ""),
                "published_message_id": publish_result["published_message_id"],
                "publish_ledger_claim": publish_result.get("publish_ledger_claim", ""),
                **_context_fields(publish_result.get("context") or {}),
                **_claim_fields(claim),
            })
            _append_activity(space, {
                "상태": "chat_turn_published", "시각": now_iso(), "actor": "공간관리",
                "target": candidate.get("target_agent", ""),
                "label": "채팅 턴 응답 공개", "detail": content[:160],
                "candidate_id": candidate_id,
                "published_message_id": publish_result["published_message_id"],
                **_context_fields(publish_result.get("context") or {}),
                **_claim_fields(claim),
            })
            announce_only = str(candidate.get("structured_action") or "").strip() in {"request_work", "mixed"}
            if not announce_only:
                _safe_obligation(
                    space,
                    "answered_by_chat_turn",
                    lambda pr=publish_result, cand=candidate: response_obligation.close_for_context(
                        space, pr.get("context") or {}, outcome="answered", actor="공간관리",
                        reason="detached 채팅 턴 응답 공개",
                        published_message_id=pr["published_message_id"],
                        responder=cand.get("target_agent", ""),
                    ),
                )
            _write_state(space, "idle", last_action="chat_turn_published",
                         last_target=candidate.get("target_agent", ""), label="채팅 턴 공개",
                         **_context_fields(context), **_claim_fields(claim))
            published += 1
            outcome = "chat_turn_published"
            events.append({
                "type": "chat_turn_published", "candidate_id": candidate_id,
                "person": candidate.get("target_agent", ""),
                "published_message_id": publish_result["published_message_id"],
            })
        except orchestration.OrchestrationStaleError:
            try:
                candidate_queue.supersede_candidates(
                    space, [candidate_id], actor="시스템",
                    reason="세대 변경 — 늦은 채팅 턴 후보 자동 정리", manager_claim_context=claim,
                )
            except Exception:
                pass
            events.append({"type": "chat_candidate_stale", "candidate_id": candidate_id})
        except candidate_queue.CandidateQueueError as exc:
            # 공개할 본문이 없는 후보(request_work인데 public_reply 없음 등) — 작업은 이미 자식이
            # 디스패치했으므로 후보만 정리한다(의무는 delegated 경로가 담당).
            try:
                candidate_queue.discard_candidates(
                    space, [candidate_id], actor="시스템",
                    reason=f"공개 불가 후보 정리: {_public_error_summary(exc)[:120]}",
                    manager_claim_context=claim,
                )
            except Exception:
                pass
            events.append({"type": "chat_candidate_discarded", "candidate_id": candidate_id})
        except Exception as exc:
            events.append({
                "type": "chat_turn_publish_failed", "candidate_id": candidate_id,
                "error": _public_error_summary(exc),
            })
        finally:
            _release_redrive(space, claim, outcome)
    return {"published": published, "events": events}


def _cleanup_stale_chat_dispatch(space: str) -> list[dict]:
    """자식(run_chat_turn)이 죽어 남긴 낡은 디스패치 파일을 정리하고 의무·상태를 되돌린다(백스톱)."""
    ddir = _chat_dispatch_dir(space)
    if not ddir.is_dir():
        return []
    events: list[dict] = []
    for dfile in sorted(ddir.glob("*.json")):
        try:
            if time.time() - dfile.stat().st_mtime <= CHAT_DISPATCH_STALE_SEC:
                continue
            params = _load_json(dfile, {}) or {}
            record_chat_turn_failure(
                space,
                wake=str(params.get("wake") or ""),
                context=params.get("context") or {},
                error="chat dispatch stale — 자식 프로세스 사망 추정(응답 미도착)",
            )
            dfile.unlink()
            events.append({"type": "chat_dispatch_stale_cleaned", "turn_id": params.get("turn_id", "")})
        except Exception:
            continue
    return events


def _dispatch_work_plan(
    space: str,
    *,
    plan_id: str,
    wake: str,
    worker: str,
    objective_for_work: str,
    effect_id: str,
    context: dict | None,
    claim: dict | None,
    handoff_context_pack: dict,
    turn_handoff_pack: dict,
) -> str:
    """작업을 실행한다 — 매니저 tick을 막지 않게 '디스패치'한다(Phase A).

    - 테스트(run_engine 몽키패치) 또는 킬스위치(WORK_DISPATCH_ASYNC=False) → 인라인 동기 실행(무회귀).
    - 프로덕션 → 별도 detached 프로세스(core.run_work)로 던지고 즉시 반환. engine.work의 분단위
      블로킹이 claim/tick을 점유하지 않아 대화가 안 막힌다. 결과는 release_queue에 남고 reflow가 공개.
    """
    inline = (not WORK_DISPATCH_ASYNC) or (
        engine.run_engine is not getattr(engine, "_ORIGINAL_RUN_ENGINE", None)
    )
    if inline:
        return _execute_work_plan(
            space, plan_id=plan_id, wake=wake, worker=worker,
            objective_for_work=objective_for_work, effect_id=effect_id,
            context=context, claim=claim,
            handoff_context_pack=handoff_context_pack, turn_handoff_pack=turn_handoff_pack,
        )

    # 동시 작업 상한 (9.2 폭주·비용 억제). 초과 시 디스패치 보류 — plan은 approved로 남아 reflow/다음 tick이 재시도.
    try:
        inflight = int(task_registry.snapshot(space).get("active_count") or 0)
    except Exception:
        inflight = 0
    if inflight >= MAX_IN_FLIGHT_TASKS:
        _append_activity(space, {
            "상태": "work_dispatch_deferred", "시각": now_iso(), "actor": "공간관리", "target": worker,
            "label": "동시 작업 상한 — 디스패치 보류",
            "detail": f"in-flight {inflight}/{MAX_IN_FLIGHT_TASKS}", "plan_id": plan_id,
            **_context_fields(context),
        })
        return f"작업 디스패치 보류(동시 {inflight}/{MAX_IN_FLIGHT_TASKS}): {worker} · {plan_id}"

    # detached 디스패치: (1) dispatch 파일 durable 기록 → (2) Popen(start_new_session) → 즉시 반환.
    dispatch_dir = SPACES / space / "dispatch"
    try:
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        dfile = dispatch_dir / f"{plan_id}.json"
        _atomic_write_json(dfile, {
            "schema": "WorkDispatch.v1",
            "space": space, "plan_id": plan_id, "wake": wake, "worker": worker,
            "objective_for_work": objective_for_work, "effect_id": effect_id,
            "context": context or {}, "at": now_iso(),
        })
    except Exception as exc:
        # 기록 실패 → 인라인 폴백(작업 유실 방지)
        return _execute_work_plan(
            space, plan_id=plan_id, wake=wake, worker=worker,
            objective_for_work=objective_for_work, effect_id=effect_id,
            context=context, claim=claim,
            handoff_context_pack=handoff_context_pack, turn_handoff_pack=turn_handoff_pack,
        )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SYS) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "core.run_work", str(dfile)],
            cwd=str(SYS), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
    except Exception:
        # Popen 실패 → 인라인 폴백(유실 방지)
        return _execute_work_plan(
            space, plan_id=plan_id, wake=wake, worker=worker,
            objective_for_work=objective_for_work, effect_id=effect_id,
            context=context, claim=claim,
            handoff_context_pack=handoff_context_pack, turn_handoff_pack=turn_handoff_pack,
        )
    _append_activity(space, {
        "상태": "work_dispatched", "시각": now_iso(), "actor": "공간관리", "target": worker,
        "label": "작업 비동기 디스패치", "detail": f"plan={plan_id} pid={proc.pid}", "plan_id": plan_id,
        "context_pack_id": handoff_context_pack.get("context_pack_id", ""),
        **_context_fields(context),
    })
    return f"작업 디스패치됨(비동기): {worker} · {plan_id} · pid={proc.pid}"


def _execute_work_plan(
    space: str,
    *,
    plan_id: str,
    wake: str,
    worker: str,
    objective_for_work: str,
    effect_id: str,
    context: dict | None,
    claim: dict | None,
    handoff_context_pack: dict,
    turn_handoff_pack: dict,
) -> str:
    """승인된 작업계획을 실제 작업으로 실행한다(engine.work). plan 상태를 executing→done|error로 전이.
    이 함수는 인라인(테스트) 또는 detached 러너(core.run_work) 안에서 호출된다 — 둘 다 동기."""
    try:
        work = engine.work(
            worker,
            space,
            objective_for_work,
            context=context,
            requested_by=f"chat_agent:{wake}",
            approved_by="space_manager_chat_request",
        )
    except Exception as exc:
        try:
            work_plan.mark_finished(space, plan_id, state=work_plan.ERROR, note=str(exc)[:240])
        except Exception:
            pass
        raise
    task_id = work.get("작업코드", "")
    state = str(work.get("상태", "")).lower()
    finish = work_plan.ERROR if any(k in state for k in ("error", "에러", "fail", "실패")) else work_plan.DONE
    try:
        work_plan.mark_executing(space, plan_id, task_id=task_id)
        work_plan.mark_finished(space, plan_id, state=finish, note=str(work.get("상태", ""))[:240])
    except work_plan.WorkPlanError:
        pass
    _safe_obligation(
        space,
        "delegated_to_task",
        lambda: response_obligation.delegate_to_task(
            space,
            context,
            task_id=task_id,
            worker_agent=worker,
            actor="공간관리",
            reason=f"{wake}가 작업 요청으로 위임(plan={plan_id})",
        ),
    )
    orchestration.append_effect(space, {
        "effect_id": effect_id,
        "effect_type": "chat_request_work_task_created",
        "requesting_agent": wake,
        "worker_agent": worker,
        "plan_id": plan_id,
        "task_id": task_id,
        "task_state": work.get("상태", ""),
        "context_pack_id": handoff_context_pack.get("context_pack_id", ""),
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
        **_context_fields(context),
    })
    _append_activity(space, {
        "상태": "task_created_from_chat_request",
        "시각": now_iso(),
        "actor": "공간관리",
        "target": worker,
        "label": "작업요청을 작업에이전트에 전달",
        "detail": f"{task_id} · {work.get('상태', '')}",
        "plan_id": plan_id,
        "task_id": task_id,
        "context_pack_id": handoff_context_pack.get("context_pack_id", ""),
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
        **_context_fields(context),
        **_claim_fields(claim),
    })
    _safe_record_interaction_evaluation(
        space,
        outcome="success",
        context=context or {},
        source_event="chat_request_work",
        actor=wake,
        target=worker,
        what_worked=["work plan approved and routed through manager-owned TaskRegistry path"],
        lesson_candidate_needed=False,
        no_lesson_reason="managed_work_request_routed",
    )
    return f"작업 요청 등록: {worker} · {task_id} · {work.get('상태', '')}"


def approve_plan(space: str, plan_id: str, *, actor: str = "대표") -> dict:
    """대표 승인의 '동기' 부분 — 승인 전이 + 결재 마커 즉시 해제 + 활동기록(버튼이 바로 반응하게).
    실제 작업 실행(블로킹)은 호출부가 별도 백그라운드로 돌린다(execute_approved_plan)."""
    plan = work_plan.get(space, plan_id)
    if plan.get("state") == work_plan.PENDING:
        work_plan.approve(space, plan_id, actor=actor, mode="representative", reason="대표 승인")
        plan = work_plan.get(space, plan_id)
    _clear_approval_required(space, plan_id)
    _append_activity(space, {
        "상태": "work_plan_approved", "시각": now_iso(), "actor": actor,
        "target": plan.get("worker", ""), "label": "작업계획 승인", "detail": str(plan.get("objective", ""))[:160],
        "plan_id": plan_id,
    })
    return plan


def execute_approved_plan(
    space: str,
    plan_id: str,
    *,
    actor: str = "대표",
    claim: dict | None = None,
    context: dict | None = None,
) -> str:
    """대표 승인 후 호출되는 진입점 — 승인 처리 + 결재 마커 해제 + 실행(P5 라우터에서 사용)."""
    plan = approve_plan(space, plan_id, actor=actor)
    objective_for_work = plan.get("objective", "")
    constraints = plan.get("constraints") or []
    if constraints:
        objective_for_work = objective_for_work + "\n\n# 제약\n" + "\n".join(f"- {item}" for item in constraints)
    return _dispatch_work_plan(
        space,
        plan_id=plan_id,
        wake=plan.get("requesting_agent", ""),
        worker=plan.get("worker", ""),
        objective_for_work=objective_for_work,
        effect_id=orchestration.effect_id("work_plan_execute", space, plan_id),
        context=context or {},
        claim=claim,
        handoff_context_pack={},
        turn_handoff_pack={},
    )


# 디스패치 파일이 이 나이(초)를 넘도록 plan이 approved에 머물면 run_work가 죽은 것으로 보고 재디스패치.
REDISPATCH_STALE_DISPATCH_SEC = 15 * 60


def redispatch_deferred_plans(space: str) -> list[dict]:
    """승인됐지만 착수하지 못한 plan을 재디스패치한다 — 종전엔 이 경로가 없었다.

    _dispatch_work_plan은 동시 작업 상한(MAX_IN_FLIGHT_TASKS) 초과 시 plan을 approved로 남기고
    보류하는데, 주석의 'reflow/다음 tick이 재시도'는 실제 코드가 없어 거짓이었다(감사 확정) —
    상한에 걸린 승인 작업이 조용히 유실됐다. reflow 주기마다 여기서 회수한다.
    레이스 가드: 디스패치 파일(dispatch/{plan_id}.json)이 방금 쓰였다면 run_work가 살아있을 수
    있으므로 mtime이 임계(15분)를 넘긴 것만 재기동한다. 파일이 아예 없으면 상한 보류였던 것이라
    즉시 재기동한다. 낡은 세대 plan은 supersede로 정리한다.
    """
    try:
        plans = work_plan.list_plans(space, states={work_plan.APPROVED})
    except Exception:
        return []
    plans = [p for p in plans if not str(p.get("task_id") or "").strip()]
    if not plans:
        return []
    current_gen = orchestration.current_generation(space)
    dispatch_dir = SPACES / space / "dispatch"
    events: list[dict] = []
    for plan in plans:
        plan_id = str(plan.get("plan_id") or "")
        rg = plan.get("room_generation")
        if rg is not None and _as_int(rg) != current_gen:
            try:
                work_plan.supersede(space, [plan_id], actor="시스템",
                                    reason=f"세대 전진(room_generation {current_gen})으로 낡은 승인 plan 정리")
            except Exception:
                pass
            events.append({"type": "deferred_plan_superseded", "plan_id": plan_id})
            continue
        dfile = dispatch_dir / f"{plan_id}.json"
        if dfile.exists():
            try:
                age_sec = max(0.0, time.time() - dfile.stat().st_mtime)
            except Exception:
                age_sec = 0.0
            if age_sec < REDISPATCH_STALE_DISPATCH_SEC:
                continue  # 최근 디스패치 — run_work가 진행 중일 수 있어 건드리지 않는다
        try:
            inflight = int(task_registry.snapshot(space).get("active_count") or 0)
        except Exception:
            inflight = 0
        if inflight >= MAX_IN_FLIGHT_TASKS:
            break  # 여전히 상한 — 다음 reflow 주기에 재시도
        objective_for_work = plan.get("objective", "")
        constraints = plan.get("constraints") or []
        if constraints:
            objective_for_work = objective_for_work + "\n\n# 제약\n" + "\n".join(f"- {item}" for item in constraints)
        try:
            detail = _dispatch_work_plan(
                space,
                plan_id=plan_id,
                wake=plan.get("requesting_agent", ""),
                worker=plan.get("worker", ""),
                objective_for_work=objective_for_work,
                effect_id=orchestration.effect_id("work_plan_redispatch", space, plan_id),
                context={
                    "intent_id": plan.get("intent_id", ""),
                    "conversation_thread_id": plan.get("conversation_thread_id", ""),
                    "room_generation": plan.get("room_generation"),
                    "source_event_seq": plan.get("source_event_seq"),
                    "source_message_id": plan.get("source_message_id", ""),
                },
                claim=None,
                handoff_context_pack={},
                turn_handoff_pack={},
            )
            events.append({"type": "deferred_plan_redispatched", "plan_id": plan_id, "detail": detail})
        except Exception as exc:
            events.append({"type": "deferred_plan_redispatch_failed", "plan_id": plan_id,
                           "error": _public_error_summary(exc)})
    if events:
        _append_activity(space, {
            "상태": "deferred_plan_redispatch", "시각": now_iso(), "actor": "시스템",
            "label": "승인 후 미착수 plan 회수", "detail": f"{len(events)}건 처리",
        })
    return events


def _append_guide_rule(space: str, rule: str, *, source: str = "대표 피드백") -> dict:
    """공간지침(방지침)에 학습된 규칙을 '누적 append'한다(덮어쓰기 아님, 중복 방지)."""
    rule = str(rule or "").strip()
    if not rule:
        return {"appended": False, "reason": "empty"}
    path = SPACES / space / "공간지침.md"
    text = path.read_text(encoding="utf-8") if path.exists() else f"# 공간: {space}\n"
    rule_line = f"- {rule}"
    if rule_line in text:
        return {"appended": False, "duplicate": True}
    section = "## 학습된 규칙(대표 지시)"
    if section not in text:
        text = text.rstrip() + f"\n\n{section}\n"
    text = text.rstrip() + f"\n{rule_line}  <!-- {source} {now_iso()[:10]} -->\n"
    path.write_text(text, encoding="utf-8")
    return {"appended": True}


def _write_rolling_summary(space: str, summary: str) -> dict:
    """롤링 누적요약을 요약.md에 기록한다 — append가 아니라 '전체 대체'다(사회자가 기존 요약 위에
    최근 진행을 합쳐 갱신된 전문을 준다). 요약.md는 context_pack의 space_summary_excerpt로 다시
    들어가 다음 wake의 누적 맥락이 된다(최근 창 밖으로 밀린 오래된 맥락의 보존처)."""
    summary = str(summary or "").strip()
    if not summary:
        return {"written": False, "reason": "empty"}
    path = SPACES / space / "요약.md"
    body = f"# {space} 요약\n\n<!-- 롤링 누적요약 · 공간관리 자동갱신 {now_iso()[:19]} -->\n\n{summary}\n"
    path.write_text(body, encoding="utf-8")
    return {"written": True, "char_count": len(summary)}


_DEPLOY_ROOTS = {"스킬", "지식", "도구", "앱", "자산"}


def _deploy_staged_outputs(space: str, release: dict) -> list[str]:
    """이식/파일생성 작업이 작업폴더 `이식산출/`에 미러 구조로 스테이징한 산출물을, release 공개 시
    실제 목적지(루트의 스킬/·지식/·도구/·앱/·자산/)로 배치한다. release는 결과 '메시지'만 공개하고
    파일을 옮기지 않던 빈틈을 메운다 — 그래서 이식 스킬이 만들어지고도 발견기에 안 잡히던 문제 해소.
    안전: 알려진 최상위 폴더만, `이식산출/` 아래 것만, 예외안전. work_dir 없으면 no-op(대부분 작업)."""
    import shutil
    worker = str(release.get("worker_agent") or "").strip()
    task_id = str(release.get("source_task_id") or "").strip()
    if not worker or not task_id:
        return []
    staged = PEOPLE / worker / "공간" / space / "작업" / task_id / "이식산출"
    if not staged.is_dir():
        return []
    root = SPACES.parent
    deployed: list[str] = []
    for top in staged.iterdir():
        if not top.is_dir() or top.name not in _DEPLOY_ROOTS:
            continue
        for src in top.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(staged)          # 예: 스킬/추가/보철근배근v2/SKILL.md
            # 지식 발견 규약 정규화: 발견기는 지식을 '지식/<등급>/<이름>/지식.md'(폴더+지식.md)로만 스캔한다
            # (discovery.py: knowledge filename="지식.md"). 작업자가 평면 '지식/<등급>/<이름>.md'로 스테이징하면
            # 배포돼도 발견기에 안 잡힌다(실증 2026-07-01 시공지식 31건 누락). 평면 지식 .md를 폴더형으로 접어 배치한다.
            parts = rel.parts
            if (top.name == "지식" and len(parts) == 3
                    and parts[2].endswith(".md") and parts[2] != "지식.md"):
                name = parts[2][:-3]               # <이름>.md → <이름>
                rel = Path(parts[0]) / parts[1] / name / "지식.md"
            dest = root / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                deployed.append(rel.as_posix())
            except Exception:
                continue
    return deployed


def _resolve_app(ref: str) -> dict | None:
    """앱 이름 또는 앱 폴더경로(ref)로 등록된 앱을 찾는다. 등록 앱만 허용(임의 명령 차단)."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    from . import apps as core_apps
    try:
        apps_list = core_apps.list_apps().get("apps", [])
    except Exception:
        return None
    ref_l = ref.lower().strip("/")
    # 1) dir 정확일치 → 2) 이름 정확일치 → 3) 이름/ dir 부분일치
    for a in apps_list:
        if str(a.get("dir", "")).strip("/").lower() == ref_l:
            return a
    for a in apps_list:
        if str(a.get("name", "")).strip().lower() == ref_l:
            return a
    for a in apps_list:
        if ref_l in str(a.get("name", "")).lower() or ref_l in str(a.get("dir", "")).lower():
            return a
    return None


def _append_space_knowledge(space: str, claim: str, *, source: str = "대표 피드백") -> dict:
    """방 지식메모(지식메모.md)에 사실/기준을 누적 append한다(전역 지식 자원 졸업은 후속)."""
    claim = str(claim or "").strip()
    if not claim:
        return {"appended": False, "reason": "empty"}
    path = SPACES / space / "지식메모.md"
    if not path.exists():
        path.write_text(f"# {space} 지식메모\n\n이 방에서 대표가 알려준 사실·기준을 누적한다.\n\n## 사실/기준\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    line = f"- {claim}"
    if line in text:
        return {"appended": False, "duplicate": True}
    path.write_text(text.rstrip() + f"\n{line}  <!-- {source} {now_iso()[:10]} -->\n", encoding="utf-8")
    return {"appended": True}


def reject_plan(space: str, plan_id: str, *, actor: str = "대표", reason: str = "") -> dict:
    """대표가 작업계획을 반려한다 — plan rejected + 결재 마커 해제 + 활동기록(P5 라우터에서 사용)."""
    result = work_plan.reject(space, plan_id, actor=actor, reason=reason)
    _clear_approval_required(space, plan_id)
    # 반려도 대표의 처리다 — plan 승인 대기 때 'assigned'로 잡아둔 응답의무를 manager_closed로 종결한다.
    # 안 닫으면 원장에 영구 assigned로 남아 미응답으로 오인된다(스냅샷 open_items·overdue 오염).
    _rec = result.get("record") or {}
    if _rec.get("source_message_id") or _rec.get("source_event_seq") is not None:
        _safe_obligation(
            space,
            "closed_by_plan_reject",
            lambda: response_obligation.close_for_context(
                space,
                {
                    "source_event_seq": _rec.get("source_event_seq"),
                    "source_message_id": _rec.get("source_message_id", ""),
                    "room_generation": _rec.get("room_generation"),
                },
                outcome="manager_closed",
                actor=actor,
                reason=f"작업계획 반려: {str(reason or '')[:200]}",
            ),
        )
    _append_activity(space, {
        "상태": "work_plan_rejected",
        "시각": now_iso(),
        "actor": actor,
        "target": plan_id,
        "label": "작업계획 반려",
        "detail": str(reason or "")[:240],
        "plan_id": plan_id,
    })
    return result.get("record") or {}


def _handle_chat_agent_result(
    space: str,
    wake: str,
    result: dict,
    claim: dict | None,
    context: dict | None,
    handoff_context_pack: dict,
    turn_handoff_pack: dict,
) -> str | None:
    request = chat_result.work_request(
        result,
        default_worker=wake,
        member_tokens=_member_tokens(space),
        worker_aliases=_member_aliases(space),
    )
    if not request:
        return None
    clean_objective = request["objective"]
    worker = request["worker"]
    constraints = request.get("constraints") or []
    objective_for_work = clean_objective
    if constraints:
        objective_for_work = clean_objective + "\n\n# 제약\n" + "\n".join(f"- {item}" for item in constraints)
    effect_id = orchestration.effect_id(
        "chat_request_work",
        space,
        wake,
        worker,
        context.get("intent_id") if context else "",
        context.get("source_event_seq") if context else "",
        clean_objective,
    )
    if orchestration.effect_exists(space, effect_id):
        return f"작업 요청 중복 감지: {worker}"

    # ── 중복 기동 방지: 같은 worker에 이미 살아있는 작업이 있으면 새로 띄우지 않고 그쪽에 진행을 독촉(steer) ──
    # 실증 2026-06-29: 대표 "이어서 가봐"가 살아있는 985a(이식 2/4 진행 중)를 둔 채 잉여 62cc를 새로 띄워
    # ②③④ 중복 생산 임박. 한 worker는 동시에 하나의 작업만 — 다자 동시작업은 '서로 다른 worker'로 한다
    # (목표.md: 동시작업은 살리되 한 일꾼의 중복 기동은 금지). heartbeat가 죽은(stale) 작업은 막지 않는다
    # (그건 재개/재실행 대상). request_progress는 기존 작업을 재실행하지 않아 진행 손실이 없다.
    try:
        _alive_same_worker = [
            t for t in (task_registry.snapshot(space).get("active_items") or [])
            if t.get("worker_agent") == worker
            and t.get("state") == "running"
            and not t.get("heartbeat_stale")
        ]
    except Exception:
        _alive_same_worker = []
    if _alive_same_worker:
        _busy_id = _alive_same_worker[0].get("task_id", "")
        try:
            request_task_steering(
                space,
                _busy_id,
                action="request_progress",
                instruction=("대표 추가 지시/이어가기: " + str(clean_objective)[:400]),
                actor=wake,
                control_context=context,
            )
        except Exception:
            pass
        _append_activity(space, {
            "상태": "chat_request_work_steered_existing",
            "시각": now_iso(),
            "actor": wake,
            "target": worker,
            "label": "기존 작업에 진행 독촉(중복 기동 방지)",
            "detail": f"worker={worker} 이미 running task={_busy_id} → 새 작업 대신 steer",
            "task_id": _busy_id,
            **_context_fields(context),
            **_claim_fields(claim),
        })
        return f"기존 작업 재지시(중복 기동 방지): {worker} task={_busy_id}"

    # ── 중복 결재대기 방지: 같은 worker에 이미 '결재 대기(pending_approval)' plan이 있으면 새로 만들지 않는다 ──
    # 실증 2026-06-29: 구조적으로 게이트된 3단계를 구현자가 9분간 6번 재요청 → pending_approval plan 6개 누적
    # (헛돎·결재 말풍선 스팸). 위 running 가드는 pending_approval을 못 막는다(state=running만 봄). 게다가
    # plan_id가 objective 기반 stable_id라 요청 문구가 조금만 달라도 매번 새 plan이 생긴다. 그래서 한 worker는
    # 동시에 '미결 결재 plan'도 하나만 갖게 막는다 — 이미 있으면 재요청을 흡수하고 대표 승인/반려를 기다린다
    # (목표.md: 헛돎·폭주 금지). 죽은/처리된 plan은 latest state가 PENDING이 아니므로 자동 제외된다.
    try:
        _pending_same_worker = [
            p for p in work_plan.list_plans(space, states={work_plan.PENDING})
            if p.get("worker") == worker
        ]
    except Exception:
        _pending_same_worker = []
    # 세대(room_generation)가 지난 pending plan은 낡은 결재다 — 대표가 방향을 바꾸거나 작업이 취소돼
    # 세대가 전진했는데도 supersede가 안 불려(전이 트리거 부재) 그 worker의 새 작업을 영구 차단했다
    # (감사 확정: 대표가 결재 말풍선을 방치하면 worker가 영원히 막힘). 낡은 세대 plan은 여기서
    # superseded로 정리하고 차단 근거에서 제외한다.
    if _pending_same_worker:
        _current_gen = orchestration.current_generation(space)
        _stale_plans = [
            p for p in _pending_same_worker
            if p.get("room_generation") is not None and _as_int(p.get("room_generation")) != _current_gen
        ]
        if _stale_plans:
            try:
                work_plan.supersede(
                    space,
                    [p.get("plan_id", "") for p in _stale_plans],
                    actor="시스템",
                    reason=f"세대 전진(room_generation {_current_gen})으로 낡은 결재대기 plan 자동 정리",
                )
            except Exception:
                pass
            else:
                _stale_ids = {p.get("plan_id", "") for p in _stale_plans}
                _pending_same_worker = [
                    p for p in _pending_same_worker if p.get("plan_id", "") not in _stale_ids
                ]
    if _pending_same_worker:
        _existing_plan = _pending_same_worker[0].get("plan_id", "")
        _append_activity(space, {
            "상태": "chat_request_work_pending_dedup",
            "시각": now_iso(),
            "actor": wake,
            "target": worker,
            "label": "이미 결재 대기 중 — 중복 plan 생성 차단",
            "detail": f"worker={worker} 이미 pending_approval plan={_existing_plan} → 새 plan 안 만듦(헛돎/스팸 방지). 대표 승인/반려 대기.",
            "plan_id": _existing_plan,
            **_context_fields(context),
            **_claim_fields(claim),
        })
        return f"이미 결재 대기 중(중복 방지): {worker} plan={_existing_plan} — 대표 승인/반려를 기다림"

    # ── 승인 게이트 (설계_작업계획승인.md) ──────────────────────────────────
    # 곧장 engine.work() 하지 않는다. 먼저 '계획'을 등록하고, 승인 필요 여부로 분기한다.
    plan_steps = request.get("plan") or [clean_objective]
    assessment = work_plan.assess_approval(
        clean_objective,
        plan_steps,
        request.get("needs_approval"),
        agent_risk_level=request.get("risk_level") or None,
        agent_reason=request.get("approval_reason") or request.get("risk_reason") or "",
        constraints=constraints,
    )
    registered = work_plan.register(
        space,
        requesting_agent=wake,
        worker=worker,
        objective=clean_objective,
        plan_steps=plan_steps,
        assessment=assessment,
        constraints=constraints,
        context=context,
    )
    plan_id = registered["record"]["plan_id"]
    _append_activity(space, {
        "상태": "chat_request_work_received",
        "시각": now_iso(),
        "actor": wake,
        "target": worker,
        "label": "채팅에이전트 작업 요청 접수",
        "detail": clean_objective[:240],
        "plan_id": plan_id,
        "needs_approval": bool(assessment["needs_approval"]),
        "approval_mode": assessment["approval_mode"],
        "context_pack_id": handoff_context_pack.get("context_pack_id", ""),
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
        **_context_fields(context),
        **_claim_fields(claim),
    })

    if assessment["needs_approval"]:
        # 대표 결재 대기 — 실행하지 않는다(불변식 C). public_reply가 결재 말풍선으로 공개된다(호출부).
        _mark_approval_required(space, registered["record"])
        _safe_obligation(
            space,
            "assigned_to_plan_approval",
            lambda: response_obligation.assign_for_context(
                space,
                context,
                assignee=f"plan_approval:{worker}",
                actor="공간관리",
                reason=assessment.get("approval_reason", ""),
            ),
        )
        orchestration.append_effect(space, {
            "effect_id": effect_id,
            "effect_type": "work_plan_registered_pending_approval",
            "requesting_agent": wake,
            "worker_agent": worker,
            "plan_id": plan_id,
            "approval_mode": assessment["approval_mode"],
            "approval_reason": assessment.get("approval_reason", ""),
            "context_pack_id": handoff_context_pack.get("context_pack_id", ""),
            "wake_id": turn_handoff_pack.get("wake_id", ""),
            **_context_fields(context),
        })
        _append_activity(space, {
            "상태": "work_plan_pending_approval",
            "시각": now_iso(),
            "actor": "공간관리",
            "target": worker,
            "label": "결재 대기 — 대표 승인 필요",
            "detail": assessment.get("approval_reason", "")[:240],
            "plan_id": plan_id,
            "context_pack_id": handoff_context_pack.get("context_pack_id", ""),
            "wake_id": turn_handoff_pack.get("wake_id", ""),
            **_context_fields(context),
            **_claim_fields(claim),
        })
        _safe_record_interaction_evaluation(
            space,
            outcome="success",
            context=context or {},
            source_event="work_plan_pending_approval",
            actor=wake,
            target=worker,
            what_worked=["work plan gated for representative approval before any execution"],
            lesson_candidate_needed=False,
            no_lesson_reason="work_plan_pending_representative_approval",
        )
        # 게이트 결과를 dict로 반환 → 호출부(_run_agent_turn)가 결재 말풍선을 보장 공개하고(불변식 A)
        # 그 말풍선 message_id를 plan에 연결한다(대화창 결재 버튼 anchor).
        return {
            "plan_gate": True,
            "plan_id": plan_id,
            "worker": worker,
            "approval_reason": assessment.get("approval_reason", ""),
            "default_bubble": (
                f"📋 작업계획 결재 요청 — {worker}\n"
                f"· 무엇: {clean_objective[:160]}\n"
                f"· 승인 필요 사유: {assessment.get('approval_reason', '')}\n"
                f"· [진행]을 누르면 작업을 시작합니다. (plan {plan_id})"
            ),
        }

    # 자동승인(저위험) → 공간관리가 자동 승인하고 디스패치(Phase A: tick 안 막힘). 테스트/킬스위치는 인라인.
    work_plan.approve(space, plan_id, actor="공간관리", mode="auto_manager", reason="시스템 자동승인(승인 불필요)")
    return _dispatch_work_plan(
        space,
        plan_id=plan_id,
        wake=wake,
        worker=worker,
        objective_for_work=objective_for_work,
        effect_id=effect_id,
        context=context,
        claim=claim,
        handoff_context_pack=handoff_context_pack,
        turn_handoff_pack=turn_handoff_pack,
    )


def _release_context(release: dict) -> dict:
    return {
        "space_id": release.get("space_id", ""),
        "intent_id": release.get("intent_id", ""),
        "conversation_thread_id": release.get("conversation_thread_id", ""),
        "room_generation": release.get("room_generation"),
        "source_event_seq": release.get("source_event_seq"),
        "source_message_id": release.get("source_message_id", ""),
        "reply_to_message_id": release.get("source_message_id", ""),
    }


def _candidate_context(candidate: dict) -> dict:
    return candidate_queue.candidate_context(candidate)


def _assert_committed_candidate_publish_matches(
    space: str,
    *,
    claim_row: dict,
    speaker_name: str,
    speaker_code: str,
    content: str,
    context: dict,
    mode: str,
    candidate_id: str,
    candidate_ids: list[str],
):
    published_message_id = claim_row.get("published_message_id", "")
    record_row = {}
    for row in reversed(read(space)):
        if row.get("message_id") == published_message_id:
            record_row = row
            break
    if not record_row:
        raise publish_ledger.PublishLedgerError("committed transcript row missing")
    expected_context = _context_fields(context)
    for key, expected in expected_context.items():
        actual = record_row.get(key)
        if key in {"room_generation", "source_event_seq"}:
            try:
                if int(actual or 0) != int(expected or 0):
                    raise publish_ledger.PublishLedgerError("idempotency_payload_mismatch")
            except publish_ledger.PublishLedgerError:
                raise
            except Exception as exc:
                raise publish_ledger.PublishLedgerError("idempotency_payload_mismatch") from exc
        elif (actual or "") != (expected or ""):
            raise publish_ledger.PublishLedgerError("idempotency_payload_mismatch")
    stored_ids = record_row.get("candidate_ids") or []
    if not isinstance(stored_ids, list):
        stored_ids = [stored_ids]
    if (
        record_row.get("화자") != speaker_name
        or record_row.get("코드") != speaker_code
        or record_row.get("역할") != "assistant"
        or record_row.get("내용") != content
        or record_row.get("candidate_publish_mode") != mode
        or record_row.get("candidate_id", "") != candidate_id
        or sorted(str(item) for item in stored_ids) != sorted(str(item) for item in candidate_ids)
    ):
        raise publish_ledger.PublishLedgerError("idempotency_payload_mismatch")


def _publish_candidate_message(
    space: str,
    *,
    claim: dict,
    candidate: dict | None,
    candidates: list[dict],
    content: str,
    mode: str,
    reason: str = "",
) -> dict:
    candidates = [item for item in candidates if isinstance(item, dict)]
    if candidate is None and candidates:
        candidate = candidates[0]
    candidate = candidate or {}
    context = _candidate_context(candidate)
    if orchestration.is_context_stale(space, context):
        raise orchestration.OrchestrationStaleError("OrchestrationStaleError: candidate stale generation")
    clean = str(content or "").strip()
    if not clean:
        raise candidate_queue.CandidateQueueError("candidate publish content required")
    if mode == "select":
        if candidate.get("structured_action") == "request_work" and not str(candidate.get("structured_public_reply") or "").strip():
            raise candidate_queue.CandidateQueueError("request_work candidate has no public reply; synthesize or discard")
        publish_effect_id = orchestration.effect_id(
            "candidate_select_publish",
            space,
            candidate.get("candidate_id", ""),
            candidate.get("intent_id", ""),
            candidate.get("source_event_seq", ""),
        )
        state = str(candidate.get("state") or "")
        if state not in {"pending_synthesis", "selected_published"}:
            raise candidate_queue.CandidateQueueError(f"candidate is not selectable: state={state}")
        if state == "selected_published" and candidate.get("publish_effect_id") != publish_effect_id:
            raise candidate_queue.CandidateQueueError("selected candidate publish_effect_id mismatch")
        speaker_token = candidate.get("target_agent", "")
        speaker_name, speaker_code = split_token(speaker_token)
        if not speaker_name:
            speaker_name = "공간관리"
            speaker_code = "manager"
    elif mode == "synthesize":
        ids = sorted(item.get("candidate_id", "") for item in candidates if item.get("candidate_id"))
        base_context = _candidate_context(candidates[0])
        for item in candidates[1:]:
            item_context = _candidate_context(item)
            if (
                item.get("turn_id") != candidates[0].get("turn_id")
                or item_context.get("intent_id") != base_context.get("intent_id")
                or item_context.get("conversation_thread_id") != base_context.get("conversation_thread_id")
                or item_context.get("room_generation") != base_context.get("room_generation")
                or item_context.get("source_event_seq") != base_context.get("source_event_seq")
            ):
                raise candidate_queue.CandidateQueueError("synthesize candidates must share turn/intent/thread/generation")
        publish_effect_id = orchestration.effect_id(
            "candidate_synthesis_publish",
            space,
            *ids,
            base_context.get("intent_id", ""),
            base_context.get("source_event_seq", ""),
        )
        for item in candidates:
            item_context = _candidate_context(item)
            if orchestration.is_context_stale(space, item_context):
                raise orchestration.OrchestrationStaleError("OrchestrationStaleError: candidate stale generation")
            state = str(item.get("state") or "")
            if state not in {"pending_synthesis", "synthesized_published"}:
                raise candidate_queue.CandidateQueueError(f"candidate is not synthesizable: state={state}")
            if state == "synthesized_published" and item.get("publish_effect_id") != publish_effect_id:
                raise candidate_queue.CandidateQueueError("synthesized candidate publish_effect_id mismatch")
        speaker_name = "공간관리"
        speaker_code = "manager"
    else:
        raise candidate_queue.CandidateQueueError(f"unsupported candidate publish mode: {mode}")
    candidate_ids = [item.get("candidate_id", "") for item in candidates if item.get("candidate_id")]
    try:
        claim_row = publish_ledger.claim_publish(
            space,
            publish_effect_id=publish_effect_id,
            manager_claim_token=claim.get("claim_token", ""),
            manager_claim_context=claim,
            context=context,
            publisher="space_manager",
            speaker=speaker_name,
        )
        if claim_row.get("already_committed"):
            _assert_committed_candidate_publish_matches(
                space,
                claim_row=claim_row,
                speaker_name=speaker_name,
                speaker_code=speaker_code,
                content=clean,
                context=context,
                mode=mode,
                candidate_id=candidate.get("candidate_id", ""),
                candidate_ids=candidate_ids,
            )
            publish_result = {
                "ok": True,
                "duplicate": True,
                "record": {
                    "message_id": claim_row.get("published_message_id", ""),
                    "event_seq": claim_row.get("event_seq"),
                },
                "ledger": claim_row,
            }
        else:
            publish_result = publish_ledger.append_public_message(
                space,
                publish_effect_id=publish_effect_id,
                publish_ledger_claim=claim_row.get("publish_ledger_claim", ""),
                manager_claim_token=claim.get("claim_token", ""),
                manager_claim_context=claim,
                published_message_id=claim_row.get("published_message_id", ""),
                intent_stale_guard_passed=True,
                speaker_name=speaker_name,
                speaker_code=speaker_code,
                role="assistant",
                content=clean,
                context=context,
                extra={
                    "candidate_publish_mode": mode,
                    "candidate_id": candidate.get("candidate_id", ""),
                    "candidate_ids": candidate_ids,
                    "candidate_turn_id": candidate.get("turn_id", ""),
                    "candidate_target_agent": candidate.get("target_agent", ""),
                    "candidate_source_claim_token": candidate.get("manager_claim_token", ""),
                    "selection_reason": str(reason or "")[:500],
                },
            )
    except publish_ledger.PublishLedgerError as exc:
        if _is_stale_publish_error(exc):
            raise orchestration.OrchestrationStaleError("OrchestrationStaleError: candidate publish stale generation") from exc
        raise
    record_row = publish_result.get("record") or {}
    return {
        "publish_effect_id": publish_effect_id,
        "publish_ledger_claim": claim_row.get("publish_ledger_claim", ""),
        "published_message_id": record_row.get("message_id", claim_row.get("published_message_id", "")),
        "event_seq": record_row.get("event_seq"),
        "publish": publish_result,
        "context": context,
        "speaker_name": speaker_name,
        "speaker_code": speaker_code,
        "content": clean,
    }


def _acquire_release_manager_claim(space: str, release: dict, action_label: str) -> tuple[dict, dict]:
    context = _release_context(release)
    if orchestration.is_context_stale(space, context):
        raise release_queue.ReleaseQueueError("release stale generation; request revision")
    delivery = transcript_state(space)
    claim_result = manager_claim.acquire(
        space,
        f"ReleaseQueue {action_label}: {release.get('release_id', '')}",
        delivery.get("last_event_seq"),
        context,
    )
    claim = claim_result.get("claim") or {}
    if claim_result.get("corrupt"):
        raise release_queue.ReleaseQueueError("manager claim corrupt")
    if not claim_result.get("acquired"):
        raise release_queue.ReleaseQueueError(f"manager claim busy; retry {action_label} later")
    return claim, context


def approve_release(space: str, release_id: str, *, actor: str = "대표", reason: str = "") -> dict:
    release = release_queue.get_release(space, release_id)
    claim, context = _acquire_release_manager_claim(space, release, "승인")
    outcome = "release_approve_failed"
    try:
        result = release_queue.approve_release(space, release_id, actor=actor, reason=reason)
        event = result.get("event") or {}
        if not result.get("duplicate"):
            _append_activity(space, {
                "상태": "release_approved",
                "시각": now_iso(),
                "actor": actor,
                "target": event.get("source_task_id", ""),
                "label": "ReleaseQueue 승인",
                "detail": reason or event.get("public_summary", "")[:160],
                "release_id": event.get("release_id", ""),
                "release_queue_id": event.get("release_queue_id", ""),
                **_context_fields(_release_context(event)),
                **_claim_fields(claim),
            })
        outcome = "release_approved"
        return result
    except Exception as exc:
        _append_activity(space, {
            "상태": "release_approve_failed",
            "시각": now_iso(),
            "actor": actor,
            "target": release.get("source_task_id", ""),
            "label": "ReleaseQueue 승인 실패",
            "detail": _public_error_summary(exc),
            "release_id": release.get("release_id", ""),
            "release_queue_id": release.get("release_queue_id", ""),
            **_context_fields(context),
            **_claim_fields(claim),
        })
        raise
    finally:
        _release_redrive(space, claim, outcome)


def reject_release(space: str, release_id: str, *, actor: str = "대표", reason: str = "") -> dict:
    release = release_queue.get_release(space, release_id)
    claim, context = _acquire_release_manager_claim(space, release, "거절")
    outcome = "release_reject_failed"
    try:
        result = release_queue.reject_release(space, release_id, actor=actor, reason=reason)
        event = result.get("event") or {}
        if not result.get("duplicate"):
            event_context = _release_context(event)
            _append_activity(space, {
                "상태": "release_rejected",
                "시각": now_iso(),
                "actor": actor,
                "target": event.get("source_task_id", ""),
                "label": "ReleaseQueue 거절",
                "detail": reason or "공개 거절",
                "release_id": event.get("release_id", ""),
                "release_queue_id": event.get("release_queue_id", ""),
                **_context_fields(event_context),
                **_claim_fields(claim),
            })
            _safe_record_interaction_evaluation(
                space,
                outcome="rejected",
                context=event_context,
                source_event="release_rejected",
                actor=actor,
                target="release_queue",
                what_failed=[reason or "release rejected"],
                lesson_candidate_needed=True,
                no_lesson_reason="release_rejection_requires_review",
            )
        outcome = "release_rejected"
        return result
    except Exception as exc:
        _append_activity(space, {
            "상태": "release_reject_failed",
            "시각": now_iso(),
            "actor": actor,
            "target": release.get("source_task_id", ""),
            "label": "ReleaseQueue 거절 실패",
            "detail": _public_error_summary(exc),
            "release_id": release.get("release_id", ""),
            "release_queue_id": release.get("release_queue_id", ""),
            **_context_fields(context),
            **_claim_fields(claim),
        })
        raise
    finally:
        _release_redrive(space, claim, outcome)


def publish_release(space: str, release_id: str, *, actor: str = "대표", text: str | None = None) -> dict:
    release = release_queue.get_release(space, release_id)
    if release.get("state") == "published":
        return {"ok": True, "duplicate": True, "release": release}
    if release.get("approval_state") != "granted":
        raise release_queue.ReleaseQueueError("release must be approved before publish")
    context = _release_context(release)
    if orchestration.is_context_stale(space, context):
        raise release_queue.ReleaseQueueError("release stale generation; request revision")
    approved_content = str(release.get("public_summary", ""))
    requested_content = "" if text is None else str(text)
    if requested_content.strip() and requested_content != approved_content:
        raise release_queue.ReleaseQueueError("custom publish text requires separate approval")
    content = approved_content
    if not content.strip():
        raise release_queue.ReleaseQueueError("publish content required")
    publish_effect_id = orchestration.effect_id(
        "release_publish",
        space,
        release.get("release_id", ""),
        release.get("task_pack_checksum_seen", ""),
    )
    delivery = transcript_state(space)
    claim_result = manager_claim.acquire(
        space,
        f"ReleaseQueue 공개: {release.get('release_id', '')}",
        delivery.get("last_event_seq"),
        context,
    )
    claim = claim_result.get("claim") or {}
    if claim_result.get("corrupt"):
        raise release_queue.ReleaseQueueError("manager claim corrupt")
    if not claim_result.get("acquired"):
        raise release_queue.ReleaseQueueError("manager claim busy; retry publish later")
    # 완료 보고는 '공간관리'가 아니라 실제 작업을 한 워커(에이전트) 명의로 방에 올린다.
    # (공간관리는 오케스트레이션만 — 말풍선을 남기지 않는다. 화자 없으면 공간관리로 폴백.)
    worker_token = str(release.get("worker_agent") or "").strip()
    # split_token으로 통일 — 종전엔 이 경로만 코드에 토큰 전체(pm_266f)를 넣어 다른 경로(266f)와
    # 화자 코드가 갈렸다(같은 pm이 두 화자로 보이는 attribution 혼선, 실방 실측).
    if worker_token and "_" in worker_token:
        speaker_disp, speaker_cd = split_token(worker_token)
    elif worker_token:
        speaker_disp, speaker_cd = worker_token, worker_token
    else:
        speaker_disp, speaker_cd = "공간관리", "manager"
    try:
        claim_row = publish_ledger.claim_publish(
            space,
            publish_effect_id=publish_effect_id,
            manager_claim_token=claim.get("claim_token", ""),
            manager_claim_context=claim,
            context=context,
            publisher="space_manager",
            speaker=speaker_disp,
        )
        if claim_row.get("already_committed"):
            record_row = {
                "message_id": claim_row.get("published_message_id", ""),
                "event_seq": claim_row.get("event_seq"),
            }
            publish_result = {
                "ok": True,
                "duplicate": True,
                "record": record_row,
                "ledger": claim_row,
            }
        else:
            publish_result = publish_ledger.append_public_message(
                space,
                publish_effect_id=publish_effect_id,
                publish_ledger_claim=claim_row.get("publish_ledger_claim", ""),
                manager_claim_token=claim.get("claim_token", ""),
                manager_claim_context=claim,
                published_message_id=claim_row.get("published_message_id", ""),
                intent_stale_guard_passed=True,
                speaker_name=speaker_disp,
                speaker_code=speaker_cd,
                role="assistant",
                content=content,
                context=context,
                extra={
                    "release_id": release.get("release_id", ""),
                    "release_queue_id": release.get("release_queue_id", ""),
                    "source_task_id": release.get("source_task_id", ""),
                    "task_pack_id": release.get("task_pack_id", ""),
                },
            )
            record_row = publish_result.get("record") or {}
        marked = release_queue.mark_published(
            space,
            release.get("release_id", release_id),
            actor=actor,
            publish_effect_id=publish_effect_id,
            published_message_id=record_row.get("message_id", claim_row.get("published_message_id", "")),
            event_seq=record_row.get("event_seq"),
        )
        orchestration.append_effect(space, {
            "effect_id": publish_effect_id,
            "effect_type": "release_public_append",
            "release_id": release.get("release_id", ""),
            "release_queue_id": release.get("release_queue_id", ""),
            "source_task_id": release.get("source_task_id", ""),
            "published_message_id": record_row.get("message_id", ""),
            "publish_ledger_claim": claim_row.get("publish_ledger_claim", ""),
            **_context_fields(context),
        })
        _append_activity(space, {
            "상태": "release_published",
            "시각": now_iso(),
            "actor": actor,
            "target": release.get("source_task_id", ""),
            "label": "ReleaseQueue 공개",
            "detail": content[:160],
            "release_id": release.get("release_id", ""),
            "release_queue_id": release.get("release_queue_id", ""),
            "published_message_id": record_row.get("message_id", ""),
            **_context_fields(context),
            **_claim_fields(claim),
        })
        # 이식/파일생성 산출물 자동 배치: 작업폴더 이식산출/ → 루트(스킬/지식/도구/앱/자산). 예외안전.
        try:
            _deployed = _deploy_staged_outputs(space, release)
            if _deployed:
                _append_activity(space, {
                    "상태": "staged_outputs_deployed", "시각": now_iso(), "actor": actor,
                    "target": release.get("source_task_id", ""),
                    "label": "이식 산출물 자동 배치", "detail": f"{len(_deployed)}개: " + ", ".join(_deployed[:5]),
                    **_context_fields(context),
                })
        except Exception:
            pass
        _safe_obligation(
            space,
            "answered_by_release_publish",
            lambda: response_obligation.close_for_context(
                space,
                context,
                outcome="answered",
                actor=actor,
                reason="ReleaseQueue 공개 완료",
                published_message_id=record_row.get("message_id", ""),
                responder="공간관리",
                task_id=release.get("source_task_id", ""),
            ),
        )
        _safe_record_interaction_evaluation(
            space,
            outcome="success",
            context=context,
            source_event="release_published",
            actor=actor,
            target="space",
            publish_effect_id=publish_effect_id,
            published_message_id=record_row.get("message_id", ""),
            what_worked=["ReleaseQueue approved result was published through manager-owned publish ledger"],
            lesson_candidate_needed=False,
            no_lesson_reason="release_publish_success",
        )
        _release_redrive(space, claim, "release_published")
        return {
            "ok": True,
            "duplicate": bool(publish_result.get("duplicate") or marked.get("duplicate")),
            "release": marked.get("event") or release,
            "publish": publish_result,
        }
    except Exception as exc:
        _append_activity(space, {
            "상태": "release_publish_failed",
            "시각": now_iso(),
            "actor": actor,
            "target": release.get("source_task_id", ""),
            "label": "ReleaseQueue 공개 실패",
            "detail": _public_error_summary(exc),
            "release_id": release.get("release_id", ""),
            "release_queue_id": release.get("release_queue_id", ""),
            **_context_fields(context),
            **_claim_fields(claim),
        })
        _safe_record_interaction_evaluation(
            space,
            outcome="failed",
            context=context,
            source_event="release_publish_failed",
            actor=actor,
            target="release_queue",
            what_failed=[_public_error_summary(exc)],
            lesson_candidate_needed=True,
            no_lesson_reason="release_publish_failure_requires_review",
        )
        _release_redrive(space, claim, "release_publish_failed")
        raise


def _append_task_cancel_activity(space: str, result: dict, *, actor: str, reason: str, generation_advanced: bool):
    event = result.get("event") or {}
    _append_activity(space, {
        "상태": "task_cancel_requested",
        "시각": now_iso(),
        "actor": actor,
        "target": event.get("worker_agent", ""),
        "label": "작업 취소 요청",
        "detail": reason[:240] or "취소 요청",
        "task_id": event.get("task_id", ""),
        "task_pack_id": event.get("task_pack_id", ""),
        "cancellation_request_id": result.get("cancellation_request_id", ""),
        "generation_advanced": bool(generation_advanced),
        "control_request_room_generation": event.get("control_request_room_generation"),
        "control_request_source_event_seq": event.get("control_request_source_event_seq"),
        **_context_fields(event),
    })


def _append_task_steering_activity(space: str, result: dict, *, actor: str, action: str, instruction: str, extra: dict | None = None):
    event = result.get("event") or {}
    reason_code = result.get("steering_reason_code") or event.get("steering_reason_code", "")
    if action == "request_progress" and reason_code == task_registry.TASK_PROGRESS_REPORT_DUE_REASON_CODE:
        label = "작업 부분 보고 필요 자동 요청"
    else:
        label = "작업 부분 보고 요청" if action == "request_progress" else "작업 재지시 요청"
    _append_activity(space, {
        "상태": action,
        "시각": now_iso(),
        "actor": actor,
        "target": event.get("worker_agent", ""),
        "label": label,
        "detail": instruction[:240] or label,
        "task_id": event.get("task_id", ""),
        "task_pack_id": event.get("task_pack_id", ""),
        "steering_seq": result.get("steering_seq", 0),
        "steering_reason_code": reason_code,
        "steering_dedupe_key": result.get("steering_dedupe_key") or event.get("steering_dedupe_key", ""),
        "requires_worker_ack": bool(result.get("requires_worker_ack")),
        "pending_steering_ack": bool(result.get("pending_steering_ack")),
        "pending_ack_steering_seq": event.get("pending_ack_steering_seq", 0),
        "pending_ack_steering_action": event.get("pending_ack_steering_action", ""),
        "control_request_room_generation": event.get("control_request_room_generation"),
        "control_request_source_event_seq": event.get("control_request_source_event_seq"),
        **(extra or {}),
        **_context_fields(event),
    })


def _task_control_request_context(task: dict, control_context: dict | None = None) -> dict:
    control_context = control_context or {}
    return {
        "room_generation_at_request": (
            control_context.get("room_generation")
            if control_context.get("room_generation") is not None
            else task.get("room_generation")
        ),
        "source_event_seq": (
            control_context.get("source_event_seq")
            if control_context.get("source_event_seq") is not None
            else task.get("source_event_seq")
        ),
    }


def request_task_cancel(space: str, task_id: str, *, actor: str = "대표", reason: str = "", control_context: dict | None = None) -> dict:
    task = task_registry.get_task(space, task_id)
    closed_states = {"done", "error", "blocked", "partial_ready", "cancelled"}
    if task.get("state") in closed_states:
        raise task_registry.TaskRegistryError("task is already closed")
    current_generation = orchestration.current_generation(space)
    control = _task_control_request_context(task, control_context)
    if task.get("cancel_requested") or task.get("state") == "cancel_requested":
        result = task_registry.request_cancel(
            space,
            task_id,
            actor=actor,
            reason=reason,
            **control,
        )
        return {"ok": True, "duplicate": True, "generation_advanced": False, **result}

    result = task_registry.request_cancel(
        space,
        task_id,
        actor=actor,
        reason=reason,
        **control,
    )
    try:
        task_generation = int(task.get("room_generation") or orchestration.DEFAULT_ROOM_GENERATION)
    except Exception:
        task_generation = orchestration.DEFAULT_ROOM_GENERATION
    should_advance_generation = False
    if not result.get("duplicate") and task_generation == int(current_generation):
        advanced = orchestration.advance_generation_if_current(
            space,
            int(current_generation),
            f"task_cancel_requested:{task_id}",
            source_event_seq=task.get("source_event_seq"),
            source_message_id=task.get("source_message_id", ""),
        )
        should_advance_generation = bool(advanced.get("advanced"))
    if not result.get("duplicate"):
        _append_task_cancel_activity(
            space,
            result,
            actor=actor,
            reason=reason,
            generation_advanced=should_advance_generation,
        )
    return {
        "ok": True,
        "duplicate": bool(result.get("duplicate")),
        "generation_advanced": bool(should_advance_generation),
        **result,
    }


def request_task_steering(space: str, task_id: str, *, action: str, instruction: str = "", actor: str = "대표", control_context: dict | None = None) -> dict:
    task = task_registry.get_task(space, task_id)
    closed_states = {"done", "error", "blocked", "partial_ready", "cancelled"}
    if task.get("state") in closed_states:
        raise task_registry.TaskRegistryError("task is already closed")
    current_generation = orchestration.current_generation(space)
    control = _task_control_request_context(task, control_context)
    result = task_registry.request_steering(
        space,
        task_id,
        action=action,
        instruction=instruction,
        actor=actor,
        **control,
    )
    if not result.get("duplicate"):
        _append_task_steering_activity(
            space,
            result,
            actor=actor,
            action=action,
            instruction=instruction,
        )
    return {
        "ok": True,
        "duplicate": bool(result.get("duplicate")),
        "generation_advanced": False,
        **result,
    }


def update_task_work_settings(space: str, task_id: str, settings: dict | None = None, *, actor: str = "대표") -> dict:
    result = task_registry.update_task_work_settings(space, task_id, settings, actor=actor)
    event = result.get("event") or {}
    work_settings_data = result.get("work_settings") or {}
    detail = (
        f"timeout {work_settings_data.get('runner_timeout_sec')}s · "
        f"hb {work_settings_data.get('heartbeat_interval_sec')}s · "
        f"stale {work_settings_data.get('heartbeat_stale_ms')}ms · "
        f"due {work_settings_data.get('progress_report_due_ms')}ms"
    )
    _append_activity(space, {
        "상태": "task_work_settings_updated",
        "시각": now_iso(),
        "actor": actor,
        "target": event.get("worker_agent", ""),
        "label": "작업 실행설정 수정",
        "detail": detail,
        "task_id": event.get("task_id", task_id),
        "task_pack_id": event.get("task_pack_id", ""),
        **_context_fields(event),
    })
    return {"ok": True, **result}


def _request_due_task_progress_reports(space: str, *, claim: dict, context: dict) -> list[dict]:
    instruction = (
        "작업 heartbeat가 기준 시간을 넘었습니다. 현재 진행 상황, 막힌 점, 다음 단계, "
        "부분 결과를 work_status/상태에 남기고 가능한 한 빨리 heartbeat를 갱신해줘."
    )
    try:
        result = task_registry.request_due_progress_reports(
            space,
            actor="공간관리",
            instruction=instruction,
            room_generation_at_request=context.get("room_generation"),
            source_event_seq=context.get("source_event_seq"),
        )
    except Exception as exc:
        err = _public_error_summary(exc)
        _append_activity(space, {
            "상태": "task_progress_due_scan_failed",
            "시각": now_iso(),
            "actor": "공간관리",
            "label": "작업 부분 보고 필요 확인 실패",
            "detail": err,
            **_context_fields(context),
            **_claim_fields(claim),
        })
        return [{"type": "task_progress_due_scan_failed", "error": err}]

    events = []
    for item in result.get("requested") or []:
        _append_task_steering_activity(
            space,
            {
                "event": item.get("event") or {},
                "steering_seq": item.get("steering_seq", 0),
                "steering_event_id": item.get("steering_event_id", ""),
                "steering_reason_code": task_registry.TASK_PROGRESS_REPORT_DUE_REASON_CODE,
            },
            actor="공간관리",
            action="request_progress",
            instruction=instruction,
            extra={
                "heartbeat_age_ms": item.get("heartbeat_age_ms"),
                "heartbeat_phase": item.get("heartbeat_phase", ""),
                **_claim_fields(claim),
            },
        )
    for item in result.get("errors") or []:
        _append_activity(space, {
            "상태": "task_progress_due_request_failed",
            "시각": now_iso(),
            "actor": "공간관리",
            "target": item.get("worker_agent", ""),
            "label": "작업 부분 보고 자동 요청 실패",
            "detail": item.get("error", ""),
            "task_id": item.get("task_id", ""),
            **_context_fields(context),
            **_claim_fields(claim),
        })
    if result.get("requested_count") or result.get("error_count"):
        events.append({
            "type": "task_progress_due_requested",
            "requested_count": result.get("requested_count", 0),
            "duplicate_count": result.get("duplicate_count", 0),
            "skipped_count": result.get("skipped_count", 0),
            "error_count": result.get("error_count", 0),
            "threshold_ms": result.get("threshold_ms", 0),
        })
    return events


def _write_state(space: str, status: str, **extra):
    lock = _status_lock_path(space)
    lock.touch(exist_ok=True)
    with lock.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            meta = _load_json(_status_meta_path(space), {})
            current = _load_json(_state_path(space), {})
            status_seq = max(
                _as_int(meta.get("last_status_seq")),
                _as_int(current.get("status_seq")),
            ) + 1
            ts = now_iso()
            data = {"상태": status, "시각": ts, "status_seq": status_seq, "status_updated_at": ts, **extra}
            data["label"] = _label_for(status, data)
            _atomic_write_json(_status_meta_path(space), {
                "last_status_seq": status_seq,
                "updated": ts,
            })
            _atomic_write_json(_state_path(space), data)
            _append_activity(space, data)
            return data
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def status(space: str) -> dict:
    p = _state_path(space)
    if not p.exists():
        data = {"상태": "unknown"}
    else:
        data = _load_json(p, {"상태": "unknown"})
    activity_rows = activity(space, 12)
    flow_activity_rows = activity(space, 80)
    delivery = transcript_state(space)
    orchestration_state = orchestration.read_state(space)
    current_room_generation = orchestration_state.get("current_room_generation")
    publish_snapshot = publish_ledger.snapshot(space)
    candidate_snapshot = candidate_queue.snapshot(space)
    context_pack_snapshot = context_pack.snapshot(space)
    learning_snapshot = lesson_ledger.snapshot(space)
    task_snapshot = task_registry.snapshot(space)
    release_snapshot = release_queue.snapshot(space)
    claim_snapshot = manager_claim.snapshot(space)
    obligation_snapshot = response_obligation.snapshot(space)
    status_seq = _read_status_seq(space, data)
    state_name = data.get("상태")
    snapshot_source_event_seq = _as_int(delivery.get("last_event_seq"))
    memory_snapshot = space_memory.snapshot(space, latest_event_seq=snapshot_source_event_seq)
    claim_active = bool(claim_snapshot.get("active"))
    claim_read_until = claim_snapshot.get("read_until_event_seq")
    state_read_until = data.get("read_until_event_seq")
    if claim_active and claim_read_until is not None:
        read_until_value = claim_read_until
    else:
        read_until_value = state_read_until
    if read_until_value is None:
        read_until_value = claim_read_until
    if read_until_value is None and state_name == "idle":
        read_until_value = snapshot_source_event_seq
    if read_until_value is None:
        read_until_value = snapshot_source_event_seq
    read_until_seq = _as_int(read_until_value, snapshot_source_event_seq)
    manager_read_lag = max(0, snapshot_source_event_seq - read_until_seq)
    rapid_input_snapshot = _rapid_input_snapshot(
        space,
        read_until_seq,
        snapshot_source_event_seq,
        state_data=data,
        claim_snapshot=claim_snapshot,
    )
    projection = _projection_status(space, snapshot_source_event_seq)
    projection_lag = projection["projection_lag"]
    staleness_ms = _staleness_ms(data)
    status_staleness_unknown = staleness_ms is None
    active_state = state_name in {"manager_queued", "manager_running", "manager_retrying", "agent_running"}
    active_stale_threshold_ms = CHAT_AGENT_STALE_MS if state_name == "agent_running" else STATUS_STALE_MS
    manager_state_without_live_claim = state_name in {"manager_running", "manager_retrying"} and not claim_active
    # 에이전트가 장기 작업(task)을 실행 중이면 방의 status 타임스탬프는 작업 시작 시점에 멈춰 있다.
    # 이때 실제 '생존 신호'는 실행 중 task의 하트비트이므로, task가 신선하게 하트비트 중이면
    # 멈춘 status 타임스탬프 기준 staleness로 '지연(stale)' 판정하지 않는다(거짓 stale 방지).
    agent_task_alive = (
        state_name == "agent_running"
        and _as_int(task_snapshot.get("running_count")) > 0
        and not task_snapshot.get("latest_heartbeat_stale")
        and not task_snapshot.get("latest_heartbeat_missing")
    )
    staleness_exceeded = (not agent_task_alive) and (
        status_staleness_unknown or staleness_ms > active_stale_threshold_ms
    )
    status_stale = (
        projection_lag > 0
        or manager_read_lag > 0
        or manager_state_without_live_claim
        or (active_state and staleness_exceeded)
    )
    failures = [
        row for row in activity_rows
        if row.get("상태") in {"wake_failed", "manager_failed", "lesson_application_missing"}
        or "실패" in str(row.get("label") or "")
    ][-5:]
    if claim_snapshot.get("claim_file_corrupt"):
        failures.append({
            "상태": "manager_claim_corrupt",
            "시각": data.get("시각", ""),
            "actor": "공간관리",
            "label": "manager claim 파일 손상",
            "detail": "자동 새 claim을 잡지 않고 복구가 필요함",
        })
    if context_pack_snapshot.get("ledger_corrupt"):
        failures.append({
            "상태": "context_pack_ledger_corrupt",
            "시각": data.get("시각", ""),
            "actor": "시스템",
            "label": "ContextPack ledger 파일 손상",
            "detail": "; ".join(context_pack_snapshot.get("ledger_errors") or []),
        })
    if publish_snapshot.get("ledger_corrupt"):
        failures.append({
            "상태": "publish_ledger_corrupt",
            "시각": data.get("시각", ""),
            "actor": "시스템",
            "label": "PublishLedger 파일 손상",
            "detail": "; ".join(publish_snapshot.get("ledger_errors") or []),
        })
    if candidate_snapshot.get("ledger_corrupt"):
        failures.append({
            "상태": "candidate_queue_corrupt",
            "시각": data.get("시각", ""),
            "actor": "시스템",
            "label": "CandidateQueue 파일 손상",
            "detail": "; ".join(candidate_snapshot.get("ledger_errors") or []),
        })
    if learning_snapshot.get("ledger_corrupt"):
        failures.append({
            "상태": "lesson_ledger_corrupt",
            "시각": data.get("시각", ""),
            "actor": "시스템",
            "label": "LessonLedger 파일 손상",
            "detail": "; ".join(learning_snapshot.get("ledger_errors") or []),
        })
    if task_snapshot.get("ledger_corrupt"):
        failures.append({
            "상태": "task_registry_corrupt",
            "시각": data.get("시각", ""),
            "actor": "시스템",
            "label": "TaskRegistry 파일 손상",
            "detail": "; ".join(task_snapshot.get("ledger_errors") or []),
        })
    if release_snapshot.get("ledger_corrupt"):
        failures.append({
            "상태": "release_queue_corrupt",
            "시각": data.get("시각", ""),
            "actor": "시스템",
            "label": "ReleaseQueue 파일 손상",
            "detail": "; ".join(release_snapshot.get("ledger_errors") or []),
        })
    if obligation_snapshot.get("ledger_corrupt"):
        failures.append({
            "상태": "response_obligation_corrupt",
            "시각": data.get("시각", ""),
            "actor": "시스템",
            "label": "ResponseObligation 원장 손상",
            "detail": "; ".join(obligation_snapshot.get("ledger_errors") or []),
        })
    if memory_snapshot.get("projection_corrupt"):
        failures.append({
            "상태": "space_memory_projection_corrupt",
            "시각": data.get("시각", ""),
            "actor": "시스템",
            "label": "SpaceMemory projection 손상",
            "detail": "; ".join(memory_snapshot.get("projection_errors") or []),
        })
    if task_snapshot.get("hold_task_count"):
        failures.append({
            "상태": "task_lesson_application_hold",
            "시각": data.get("시각", ""),
            "actor": task_snapshot.get("latest_hold_worker", "작업에이전트"),
            "label": "작업 레슨 적용 보고 누락으로 완료 보류",
            "detail": task_snapshot.get("latest_hold_error", "") or task_snapshot.get("latest_hold_task_id", ""),
            "task_id": task_snapshot.get("latest_hold_task_id", ""),
            "task_pack_id": task_snapshot.get("latest_hold_task_pack_id", ""),
        })
    if task_snapshot.get("release_enqueue_failed_count"):
        failures.append({
            "상태": "task_release_enqueue_failed",
            "시각": data.get("시각", ""),
            "actor": task_snapshot.get("latest_worker", "작업에이전트"),
            "label": "작업 결과 공개 대기열 등록 실패",
            "detail": task_snapshot.get("latest_release_enqueue_failed_error", ""),
            "task_id": task_snapshot.get("latest_release_enqueue_failed_task_id", ""),
            "release_queue_state": task_snapshot.get("latest_release_queue_state", ""),
        })
    if task_snapshot.get("release_followup_missing_count"):
        failures.append({
            "상태": "task_release_followup_missing",
            "시각": data.get("시각", ""),
            "actor": task_snapshot.get("latest_release_followup_missing_worker", "작업에이전트"),
            "label": "작업 완료 후 공개 후속 처리 누락",
            "detail": "task_finalized 이후 release follow-up 이벤트가 없어 재확인이 필요함",
            "task_id": task_snapshot.get("latest_release_followup_missing_task_id", ""),
        })
    active_wakes = []
    stale_wakes = []
    if claim_active:
        active_wakes.append({
            "type": "space_manager",
            "actor": "공간관리",
            "state": "manager_running",
            "event": claim_snapshot.get("source_event", ""),
            "read_until_event_seq": claim_read_until,
            "claim_token": claim_snapshot.get("claim_token", ""),
            "lease_expires_at_utc": claim_snapshot.get("lease_expires_at_utc", ""),
            "manager_redrive_required": bool(claim_snapshot.get("manager_redrive_required")),
            "intent_id": claim_snapshot.get("intent_id", ""),
            "conversation_thread_id": claim_snapshot.get("conversation_thread_id", ""),
            "room_generation": claim_snapshot.get("room_generation"),
        })
    elif state_name == "manager_queued":
        active_wakes.append({
            "type": "space_manager",
            "actor": "공간관리",
            "state": state_name,
            "event": data.get("event", ""),
            "read_until_event_seq": state_read_until,
            "claim_token": data.get("manager_claim_token", ""),
            "lease_expires_at_utc": data.get("lease_expires_at_utc", ""),
            "manager_redrive_required": bool(data.get("manager_redrive_required")),
            "intent_id": data.get("intent_id", ""),
            "conversation_thread_id": data.get("conversation_thread_id", ""),
            "room_generation": data.get("room_generation"),
        })
    elif state_name in {"manager_running", "manager_retrying"}:
        stale_wakes.append({
            "type": "space_manager",
            "actor": "공간관리",
            "state": state_name,
            "event": data.get("event", ""),
            "read_until_event_seq": state_read_until,
            "claim_token": data.get("manager_claim_token", ""),
            "lease_expires_at_utc": data.get("lease_expires_at_utc", ""),
            "reason": "live manager claim 없음 또는 lease 만료",
            "intent_id": data.get("intent_id", ""),
            "conversation_thread_id": data.get("conversation_thread_id", ""),
            "room_generation": data.get("room_generation"),
        })
    if state_name == "agent_running":
        active_wakes.append({
            "type": "agent",
            "actor": data.get("current", ""),
            "state": state_name,
            "reason": data.get("reason", ""),
            "context_pack_id": data.get("context_pack_id", ""),
            "wake_id": data.get("wake_id", ""),
            "turn_handoff_id": data.get("turn_handoff_id", ""),
            "wake_pack_manifest_id": data.get("wake_pack_manifest_id", ""),
            "intent_id": data.get("intent_id", ""),
            "conversation_thread_id": data.get("conversation_thread_id", ""),
            "room_generation": data.get("room_generation"),
        })
    recovery_actions = []
    if failures:
        recovery_actions.append("상태이력에서 실패 원인을 확인한 뒤 수동 진행 또는 재전송")
    if stale_wakes:
        recovery_actions.append("상태 파일은 실행 중으로 보이나 live claim이 없어 재조회 또는 수동 진행 확인")
    if claim_snapshot.get("claim_file_corrupt"):
        recovery_actions.append("manager_claim.json 손상 여부를 확인하고 복구 또는 수동 초기화")
    if context_pack_snapshot.get("ledger_corrupt"):
        recovery_actions.append("context/wake pack ledger 손상 파일을 확인하고 필요 시 백업 후 재생성")
    if publish_snapshot.get("ledger_corrupt"):
        recovery_actions.append("PublishLedger 손상 파일을 확인하고 공개 중복/누락 여부를 점검")
    if candidate_snapshot.get("ledger_corrupt"):
        recovery_actions.append("CandidateQueue 손상 파일을 확인하고 병렬 후보 중복/누락 여부를 점검")
    if learning_snapshot.get("ledger_corrupt"):
        recovery_actions.append("learning ledger 손상 파일을 확인하고 필요 시 백업 후 재생성")
    if task_snapshot.get("ledger_corrupt"):
        recovery_actions.append("TaskRegistry/task pack manifest 손상 파일을 확인하고 필요 시 백업 후 재생성")
    if release_snapshot.get("ledger_corrupt"):
        recovery_actions.append("ReleaseQueue 손상 파일을 확인하고 필요 시 백업 후 재생성")
    if obligation_snapshot.get("ledger_corrupt"):
        recovery_actions.append("ResponseObligation 원장 손상 파일을 확인하고 열린 응답 의무를 재구성")
    if memory_snapshot.get("projection_corrupt"):
        recovery_actions.append("memory/projection.json 손상 여부를 확인하고 다음 ContextPack 생성으로 재구성")
    if task_snapshot.get("hold_task_count"):
        recovery_actions.append("작업 폴더의 레슨적용보고.json 또는 task_pack lesson_pack을 확인한 뒤 재실행/검토")
    if task_snapshot.get("release_enqueue_failed_count"):
        recovery_actions.append("작업 결과는 draft로 남아 있으므로 ReleaseQueue 손상/권한 문제를 복구한 뒤 재등록")
    if task_snapshot.get("release_followup_missing_count"):
        recovery_actions.append("완료 작업의 release follow-up 누락 여부를 확인하고 release_request 재등록 또는 작업 재검증")
    if task_snapshot.get("stale_task_count"):
        recovery_actions.append("작업 heartbeat가 기준을 넘었으므로 작업 폴더/취소요청/최근 로그를 확인하고 공간관리 재판단 또는 수동 취소")
    if task_snapshot.get("progress_report_due_count"):
        recovery_actions.append("진행 중 작업의 부분 보고 기한이 지났으므로 다음 공간관리 tick에서 작업자에게 진행 보고 steering을 요청")
    if task_snapshot.get("pending_steering_count"):
        recovery_actions.append("재지시 steering을 작업자가 아직 반영하지 않았으므로 자동 공개 전 작업 폴더의 last_seen_steering_seq를 확인")
    snapshot = {
        "delivery": delivery,
        "active_wakes": active_wakes,
        "stale_wakes": stale_wakes,
        "read_until": read_until_seq,
        "snapshot_source_event_seq": snapshot_source_event_seq,
        "snapshot_status_seq": status_seq,
        "current_room_generation": current_room_generation,
        "orchestration": orchestration_state,
        "publish_ledger": publish_snapshot,
        "candidate_queue": candidate_snapshot,
        "context_packs": context_pack_snapshot,
        "learning": learning_snapshot,
        "tasks": task_snapshot,
        "task_runtime_activity": task_snapshot.get("runtime_activity_items", []),
        "release_queue": release_snapshot,
        "response_obligations": obligation_snapshot,
        "space_memory": memory_snapshot,
        "rapid_input": rapid_input_snapshot,
        "status_legacy": status_seq is None,
        "staleness_ms": staleness_ms,
        "active_stale_threshold_ms": active_stale_threshold_ms if active_state else STATUS_STALE_MS,
        "status_staleness_unknown": status_staleness_unknown,
        "projection_lag": projection_lag,
        "projection_tail_lag": projection["projection_tail_lag"],
        "projection_missing_count": projection["projection_missing_count"],
        "projection_lag_by_member": projection["projection_lag_by_member"],
        "seat_projection_baselines": projection.get("seat_projection_baselines", []),
        "seat_projection_baseline_count": projection.get("seat_projection_baseline_count", 0),
        "manager_read_lag": manager_read_lag,
        "status_stale": status_stale,
        "failures": failures,
        "recovery_actions": recovery_actions,
        "manager_claim": claim_snapshot,
        "manager_redrive_required": bool(claim_snapshot.get("manager_redrive_required") or data.get("manager_redrive_required")),
    }
    chat_flow = _chat_flow_snapshot(
        space,
        state_data=data,
        activity_rows=flow_activity_rows,
        claim_snapshot=claim_snapshot,
        task_snapshot=task_snapshot,
        candidate_snapshot=candidate_snapshot,
        release_snapshot=release_snapshot,
        obligation_snapshot=obligation_snapshot,
        staleness_ms=staleness_ms,
        status_stale=status_stale,
    )
    snapshot["chat_flow"] = chat_flow
    data["activity"] = activity_rows
    data.update(snapshot)
    data["snapshot"] = snapshot
    data["chat_flow"] = chat_flow
    return data


def activity(space: str, limit: int = 30) -> list[dict]:
    path = _activity_path(space)
    if not path.exists():
        return []
    try:
        safe_limit = max(int(limit or 30), 1)
    except Exception:
        safe_limit = 30
    recent_lines = deque(maxlen=safe_limit)
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            recent_lines.append(line)
    rows = []
    for line in recent_lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _latest_user_message(space: str) -> dict:
    rows = read(space)
    for row in reversed(rows):
        if row.get("역할") == "user" and row.get("run_manager_requested") is not False:
            return row
    for row in reversed(rows):
        if row.get("역할") == "user":
            return row
    return {}


def _chat_flow_matches(row: dict, latest: dict) -> bool:
    if not latest:
        return True
    latest_seq = _as_int(latest.get("event_seq"))
    source_seq = _as_int(row.get("source_event_seq"))
    event_seq = _as_int(row.get("event_seq"))
    if latest_seq and (source_seq == latest_seq or event_seq == latest_seq):
        return True
    latest_msg = str(latest.get("message_id") or "")
    if latest_msg and str(row.get("source_message_id") or row.get("message_id") or "") == latest_msg:
        return True
    latest_intent = str(latest.get("intent_id") or "")
    return bool(latest_intent and str(row.get("intent_id") or "") == latest_intent)


def _latest_matching_activity(rows: list[dict], latest: dict, states: set[str] | None = None) -> dict:
    for row in reversed(rows):
        if states is not None and row.get("상태") not in states:
            continue
        if _chat_flow_matches(row, latest):
            return row
    return {}


def _chat_flow_activity_rows(space: str, latest: dict, fallback_rows: list[dict]) -> list[dict]:
    if not latest:
        return fallback_rows
    path = _activity_path(space)
    if not path.exists():
        return fallback_rows
    rows = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if _chat_flow_matches(row, latest):
                    rows.append(row)
    except Exception:
        return fallback_rows
    return rows or fallback_rows


def _matching_flow_items(items: list[dict], latest: dict) -> list[dict]:
    return [
        item for item in (items or [])
        if isinstance(item, dict) and _chat_flow_matches(item, latest)
    ]


def _flow_phase(key: str, label: str, state: str, detail: str = "", **extra) -> dict:
    return {
        "key": key,
        "label": label,
        "state": state,
        "detail": str(detail or ""),
        **extra,
    }


def _chat_flow_snapshot(
    space: str,
    *,
    state_data: dict,
    activity_rows: list[dict],
    claim_snapshot: dict,
    task_snapshot: dict,
    candidate_snapshot: dict,
    release_snapshot: dict,
    obligation_snapshot: dict,
    staleness_ms: int | None,
    status_stale: bool,
) -> dict:
    latest = _latest_user_message(space)
    activity_rows = _chat_flow_activity_rows(space, latest, activity_rows)
    state_name = str(state_data.get("상태") or "")
    last_action = str(state_data.get("last_action") or "")
    manager_decision = _latest_matching_activity(activity_rows, latest, {"manager_decision"})
    manager_failure = _latest_matching_activity(activity_rows, latest, {"manager_failed", "manager_claim_corrupt", "manager_generation_stale"})
    wake_failure = _latest_matching_activity(activity_rows, latest, {"wake_failed", "lesson_application_missing"})
    agent_reply = _latest_matching_activity(activity_rows, latest, {"agent_replied"})
    candidate_running = _latest_matching_activity(activity_rows, latest, {"parallel_candidate_running"})
    task_created = _latest_matching_activity(activity_rows, latest, {"task_created_from_chat_request"})
    release_created = _latest_matching_activity(activity_rows, latest, {"release_enqueued", "release_approved", "release_published"})
    flow_pending_releases = _matching_flow_items(release_snapshot.get("pending_items") or [], latest)
    flow_approved_releases = _matching_flow_items(release_snapshot.get("approved_items") or [], latest)
    flow_candidates = _matching_flow_items(candidate_snapshot.get("latest") or [], latest)
    flow_pending_candidates = _matching_flow_items(candidate_snapshot.get("pending_items") or [], latest)
    flow_obligations = _matching_flow_items(obligation_snapshot.get("latest") or [], latest)
    latest_obligation = flow_obligations[-1] if flow_obligations else {}
    current = {
        "state": state_name or "unknown",
        "label": state_data.get("label", ""),
        "actor": state_data.get("actor", ""),
        "target": state_data.get("target") or state_data.get("current") or state_data.get("last_target") or "",
        "detail": state_data.get("reason") or state_data.get("event") or "",
        "staleness_ms": staleness_ms,
        "status_stale": bool(status_stale),
    }
    phases = []
    if latest:
        phases.append(_flow_phase(
            "input",
            "입력 기록",
            "done",
            str(latest.get("내용") or "")[:180],
            event_seq=latest.get("event_seq"),
            message_id=latest.get("message_id", ""),
            speaker=latest.get("화자", ""),
            intent_id=latest.get("intent_id", ""),
        ))
    else:
        phases.append(_flow_phase("input", "입력 기록", "pending", "아직 대표 입력이 없습니다."))

    manager_state = "pending"
    manager_detail = ""
    manager_target = ""
    if state_name == "manager_queued":
        manager_state = "current"
        manager_detail = state_data.get("label", "공간관리 대기")
    elif state_name in {"manager_running", "manager_retrying"}:
        manager_state = "current"
        manager_detail = state_data.get("label", "공간관리 판단 중")
    elif manager_failure:
        manager_state = "failed"
        manager_detail = manager_failure.get("detail") or manager_failure.get("label") or ""
    elif manager_decision:
        manager_state = "done"
        manager_detail = manager_decision.get("detail") or manager_decision.get("label") or ""
        manager_target = manager_decision.get("target", "")
    elif latest:
        manager_state = "queued" if state_data.get("manager_redrive_required") else "pending"
        manager_detail = "공간관리 처리 이력이 아직 보이지 않습니다."
    phases.append(_flow_phase(
        "manager",
        "공간관리 판단",
        manager_state,
        manager_detail,
        action="manager_failed" if manager_failure else manager_decision.get("action", ""),
        target=manager_target,
        claim_active=bool(claim_snapshot.get("active")),
        redrive_required=bool(claim_snapshot.get("manager_redrive_required") or state_data.get("manager_redrive_required")),
    ))

    decision_action = str(("manager_failed" if manager_failure else "") or manager_decision.get("action") or last_action or "")
    if manager_failure:
        decision_state = "failed"
        decision_detail = manager_failure.get("detail", "")
    elif manager_decision:
        decision_state = "done"
        decision_detail = " · ".join(
            item for item in [
                decision_action,
                manager_decision.get("target", ""),
                manager_decision.get("detail", ""),
            ] if item
        )
    elif state_name in {"manager_queued", "manager_running", "manager_retrying"}:
        decision_state = "pending"
        decision_detail = "아직 유효한 JSON 결정이 나오지 않았습니다."
    else:
        decision_state = "pending"
        decision_detail = ""
    phases.append(_flow_phase(
        "decision",
        "JSON 결정",
        decision_state,
        decision_detail,
        action=decision_action,
        target=manager_decision.get("target", ""),
    ))

    turn_state = "pending"
    turn_detail = ""
    turn_target = state_data.get("current") or state_data.get("last_target") or manager_decision.get("target", "")
    if state_name == "agent_running":
        turn_state = "current"
        turn_detail = state_data.get("label", "")
        turn_target = state_data.get("current") or turn_target
    elif candidate_running:
        turn_state = "current"
        turn_detail = candidate_running.get("label", "")
        turn_target = candidate_running.get("target", "")
    elif wake_failure:
        turn_state = "failed"
        turn_detail = wake_failure.get("detail") or wake_failure.get("label") or ""
        turn_target = wake_failure.get("target", turn_target)
    elif agent_reply:
        turn_state = "done"
        turn_detail = agent_reply.get("detail") or agent_reply.get("label") or ""
        turn_target = agent_reply.get("actor", turn_target)
    elif task_created:
        turn_state = "done"
        turn_detail = task_created.get("detail") or task_created.get("label") or ""
        turn_target = task_created.get("target", turn_target)
    elif decision_action == "parallel_pass":
        pending = len(flow_pending_candidates)
        total = len(flow_candidates) or pending
        turn_state = "done" if total else "pending"
        turn_detail = f"병렬 후보 {total}개 · 대기 {pending}개"
    elif decision_action in {"cancel_task", "revise_task", "request_progress"}:
        turn_state = "done" if state_name == "idle" else "current"
        turn_detail = state_data.get("label", "") or decision_action
    elif decision_action == "stop" or last_action == "stop":
        turn_state = "stopped"
        turn_detail = state_data.get("reason") or "공간관리가 이번 턴을 멈췄습니다."
    elif last_action in {"pass", "parallel_pass", "select_candidate", "synthesize_candidates"}:
        turn_state = "done"
        turn_detail = state_data.get("label", "")
    phases.append(_flow_phase(
        "turn",
        "턴/작업 진행",
        turn_state,
        turn_detail,
        target=turn_target,
    ))

    obligation_state = "pending"
    obligation_detail = ""
    obligation_target = ""
    if latest_obligation:
        raw_state = latest_obligation.get("state", "")
        obligation_target = latest_obligation.get("assigned_to") or latest_obligation.get("target_actor") or ""
        obligation_detail = " · ".join(item for item in [
            raw_state,
            latest_obligation.get("transition_reason", ""),
            latest_obligation.get("published_message_id", ""),
        ] if item)
        if raw_state == "answered":
            obligation_state = "done"
        elif raw_state == "manager_closed":
            obligation_state = "stopped"
        elif raw_state in {"superseded", "cancelled", "timed_out"}:
            obligation_state = "failed" if raw_state == "timed_out" else "stopped"
        elif raw_state in {"assigned", "delegated"}:
            obligation_state = "current"
        else:
            obligation_state = "pending"
    elif latest:
        obligation_detail = "응답 의무 원장 대기"
    phases.append(_flow_phase(
        "obligation",
        "응답 의무",
        obligation_state,
        obligation_detail,
        target=obligation_target,
        obligation_id=latest_obligation.get("obligation_id", ""),
    ))

    output_state = "pending"
    output_detail = ""
    if wake_failure or manager_failure:
        output_state = "failed"
        output_detail = (wake_failure or manager_failure).get("detail", "")
    elif flow_pending_releases:
        output_state = "approval"
        output_detail = f"공개 승인 대기 {len(flow_pending_releases)}건"
    elif flow_approved_releases:
        output_state = "approval"
        output_detail = f"승인 후 공개 대기 {len(flow_approved_releases)}건"
    elif release_created:
        output_state = "done"
        output_detail = release_created.get("detail") or release_created.get("label") or ""
    elif agent_reply or last_action in {"pass", "select_candidate", "synthesize_candidates"}:
        output_state = "done"
        output_detail = state_data.get("label", "공개 흐름 완료")
    elif last_action == "stop" or decision_action == "stop":
        output_state = "stopped"
        output_detail = state_data.get("reason", "")
    phases.append(_flow_phase(
        "output",
        "공개/멈춤",
        output_state,
        output_detail,
        pending_release_count=len(flow_pending_releases),
    ))

    blockers = []
    if status_stale:
        blockers.append("상태가 오래되었거나 projection/read lag가 있습니다.")
    if manager_failure:
        blockers.append(manager_failure.get("detail") or manager_failure.get("label") or "공간관리 실패")
    if wake_failure:
        blockers.append(wake_failure.get("detail") or wake_failure.get("label") or "턴 전달 실패")
    if _as_int(task_snapshot.get("stale_task_count")):
        blockers.append("작업 heartbeat가 기준 시간을 넘었습니다.")
    if _as_int(task_snapshot.get("pending_steering_count")):
        blockers.append("작업자가 재지시를 아직 반영하지 않았습니다.")

    if manager_failure:
        decision_summary = {
            "action": "manager_failed",
            "target": manager_failure.get("target", ""),
            "reason": manager_failure.get("detail") or manager_failure.get("label") or "",
        }
    else:
        decision_summary = {
            "action": manager_decision.get("action", decision_action),
            "target": manager_decision.get("target", ""),
            "reason": manager_decision.get("detail", ""),
        }

    return {
        "schema": "RoomChatFlowSnapshot.v1",
        "space_id": space,
        "latest_message": {
            "event_seq": latest.get("event_seq"),
            "message_id": latest.get("message_id", ""),
            "client_message_id": latest.get("client_message_id", ""),
            "speaker": latest.get("화자", ""),
            "text_preview": str(latest.get("내용") or "")[:180],
            "intent_id": latest.get("intent_id", ""),
            "conversation_thread_id": latest.get("conversation_thread_id", ""),
            "room_generation": latest.get("room_generation"),
        } if latest else {},
        "current": current,
        "phases": phases,
        "decision": decision_summary,
        "blockers": blockers,
        "manager": {
            "claim_active": bool(claim_snapshot.get("active")),
            "read_until_event_seq": claim_snapshot.get("read_until_event_seq") or state_data.get("read_until_event_seq"),
            "redrive_required": bool(claim_snapshot.get("manager_redrive_required") or state_data.get("manager_redrive_required")),
        },
    }


def record_space_work_settings_updated(space: str, result: dict, *, actor: str = "대표") -> dict:
    settings = result.get("effective_settings") or result
    detail = (
        f"timeout {settings.get('runner_timeout_sec')}s · "
        f"hb {settings.get('heartbeat_interval_sec')}s · "
        f"stale {settings.get('heartbeat_stale_ms')}ms · "
        f"due {settings.get('progress_report_due_ms')}ms"
    )
    row = {
        "상태": "space_work_settings_updated",
        "시각": now_iso(),
        "actor": actor,
        "target": space,
        "label": "공간 작업 실행설정 수정",
        "detail": detail,
        "configured_keys": settings.get("configured_keys", []),
    }
    _append_activity(space, row)
    return row


def record_seat_work_settings_updated(space: str, person: str, result: dict, *, actor: str = "대표") -> dict:
    settings = result.get("effective_settings") or result
    seat_settings = result.get("seat_settings") or {}
    configured_keys = seat_settings.get("configured_keys", [])
    key_label = ", ".join(configured_keys) if configured_keys else "상속만 사용"
    detail = (
        f"{person} · 직접 {key_label} · "
        f"timeout {settings.get('runner_timeout_sec')}s · "
        f"hb {settings.get('heartbeat_interval_sec')}s · "
        f"stale {settings.get('heartbeat_stale_ms')}ms · "
        f"due {settings.get('progress_report_due_ms')}ms"
    )
    row = {
        "상태": "seat_work_settings_updated",
        "시각": now_iso(),
        "actor": actor,
        "target": person,
        "label": "좌석 작업 실행설정 수정",
        "detail": detail,
        "configured_keys": configured_keys,
    }
    _append_activity(space, row)
    return row


def _manager_has_seen_event(space: str, event_seq) -> bool:
    try:
        target_seq = int(event_seq or 0)
    except Exception:
        return False
    if target_seq <= 0:
        return False
    target = None
    for row in reversed(read(space, None)):
        try:
            if int(row.get("event_seq") or 0) == target_seq:
                target = row
                break
        except Exception:
            continue
    claim = manager_claim.snapshot(space)
    try:
        if claim.get("active") and int(claim.get("source_event_seq") or 0) == target_seq:
            return True
    except Exception:
        pass
    seen_states = {
        "manager_running",
        "manager_retrying",
        "manager_decision",
        "manager_failed",
        "agent_running",
        "agent_replied",
        "wake_failed",
        "lesson_application_missing",
        "task_created_from_chat_request",
    }
    for row in activity(space, 1000):
        if row.get("상태") == "posted":
            continue
        if row.get("상태") not in seen_states:
            continue
        if target and _chat_flow_matches(row, target):
            return True
    return False


def queue_manager(space: str, event: str, context: dict | None = None):
    delivery = transcript_state(space)
    context = context or _latest_context(space, delivery.get("last_event_seq"))
    source_event_seq = _as_int(context.get("source_event_seq")) or delivery.get("last_event_seq")
    claim_result = manager_claim.mark_redrive(space, event, source_event_seq, context)
    if claim_result.get("marked"):
        claim = claim_result.get("claim") or {}
        return _write_state(
            space, "manager_running", event=event, actor="공간관리",
            label="공간관리 처리 중 · 새 입력 재처리 예약",
            read_until_event_seq=delivery.get("last_event_seq"),
            queue_event_type="manager_redrive_required",
            **_context_fields(context),
            **_coalesced_fields(context),
            **_claim_fields(claim),
        )
    return _write_state(
        space, "manager_queued", event=event, actor="공간관리", label="공간관리 대기",
            read_until_event_seq=delivery.get("last_event_seq"), queue_event_type="manager_queued",
            **_context_fields(context),
            **_coalesced_fields(context),
        )


def _manager_busy_result(space: str, event: str, context: dict | None = None) -> dict:
    delivery = transcript_state(space)
    context = context or _latest_context(space, delivery.get("last_event_seq"))
    source_event_seq = _as_int(context.get("source_event_seq")) or delivery.get("last_event_seq")
    claim_result = manager_claim.mark_redrive(space, event, source_event_seq, context)
    claim = claim_result.get("claim") or {}
    if claim_result.get("marked"):
        _append_activity(space, {
            "상태": "manager_claim_busy", "시각": now_iso(), "actor": "공간관리",
            "label": "공간관리 실행 중 · 재처리 예약",
            "detail": "이미 유효한 manager claim이 있어 새 tick은 redrive로 수렴",
            **_claim_fields(claim),
        })
        _write_state(
            space, "manager_running", event=event, actor="공간관리",
            label="공간관리 실행 중 · 재처리 예약",
            read_until_event_seq=delivery.get("last_event_seq"),
            queue_event_type="manager_redrive_required",
            **_context_fields(context),
            **_coalesced_fields(context),
            **_claim_fields(claim),
        )
        event_type = "manager_redrive_required"
    else:
        event_type = "manager_tick_busy"
    return {"ok": True, "claim_busy": True, "events": [{
        "type": event_type,
        "claim_token": claim.get("claim_token", ""),
    }]}


def _redrive_event_from(result: dict) -> dict:
    for event in reversed(result.get("events") or []):
        if event.get("type") == "manager_redrive_required":
            return {
                "event": event.get("event") or "새 입력 재처리 필요",
                "context": event.get("context") or {},
                "redrive_events": event.get("redrive_events") or [],
                "coalesced_pending_inputs": event.get("coalesced_pending_inputs") or [],
            }
    return {}


def _has_orphaned_redrive(space: str) -> bool:
    """released claim에 redrive_required가 남았고 아직 매니저가 안 읽은 입력이 있나(고아 redrive).

    이 체인이 claim을 쥔 동안 새 입력이 들어오면 release 시점에만 redrive로 마킹되는데, 이 체인의
    `while next_redrive` 검사는 그 전에 이미 지났고(자동연속은 '그 입력의 tick이 재계획'을 가정하지만
    그 입력의 tick은 claim_busy로 이미 포기) → 아무도 처리하지 않아 방이 멈춘다. 그 상태를 탐지한다.
    """
    try:
        claim = manager_claim.snapshot(space)
    except Exception:
        return False
    if not claim.get("manager_redrive_required"):
        return False
    if str(claim.get("state") or "") != "released":
        return False                                   # running=다른 체인이 처리 중 → 건드리지 않음
    last = _as_int(transcript_state(space).get("last_event_seq"))
    read_until = _as_int(claim.get("read_until_event_seq")) or 0
    return last is not None and last > read_until


def _handback_marker_path(space: str) -> Path:
    return SPACES / space / "representative_handback.json"


def _mark_representative_handback(space: str, *, reason: str = "") -> None:
    """자동 연속이 매니저 stop으로 끝나 대표에게 턴을 넘길 때, 대표가 확인할 최신
    에이전트 말풍선을 강조 대상으로 기록한다. 대시보드가 이 message_id를 하이라이트한다."""
    target = ""
    try:
        for row in reversed(read(space, 50)):
            if row.get("역할") == "assistant":
                target = row.get("message_id", "")
                break
    except Exception:
        target = ""
    marker = {
        "schema": "RepresentativeHandback.v1",
        "needs_representative": True,
        "highlight_message_id": target,
        "reason": str(reason or "")[:240],
        "at": now_iso(),
    }
    try:
        _handback_marker_path(space).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _clear_representative_handback(space: str) -> None:
    """대표가 다시 발언하면 핸드백 강조를 해제한다."""
    try:
        path = _handback_marker_path(space)
        if path.exists():
            path.unlink()
    except Exception:
        pass


def read_representative_handback(space: str) -> dict:
    try:
        path = _handback_marker_path(space)
        if not path.exists():
            return {"needs_representative": False}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"needs_representative": False}
    except Exception:
        return {"needs_representative": False}


def _latest_user_event_seq(space: str) -> int:
    """매니저 처리를 요청하는 가장 최근 대표 발언의 event_seq.

    자동 연속(에이전트끼리 진행) 도중 대표가 새 요청을 끼워넣었는지 판단하는 기준.
    시작 시점 baseline보다 커지면 새 입력이 들어온 것이므로 자동 연속을 멈추고 양보한다 —
    그래야 매니저가 새 요청을 반영해 재계획(진행 중 작업 취소·재지시·진행보고 또는 다른
    멤버 깨우기)할 수 있다.
    """
    latest = 0
    try:
        for row in read(space, None):
            if row.get("역할") != "user" or row.get("run_manager_requested") is False:
                continue
            latest = max(latest, _as_int(row.get("event_seq")))
    except Exception:
        return latest
    return latest


def _auto_continue_after_pass(result: dict) -> bool:
    """직전 tick이 '에이전트를 깨워 응답이 정상 공개된 pass'였는지 — 자동 연속 가능 여부."""
    if not result or not result.get("ok"):
        return False
    if result.get("stale") or result.get("claim_busy") or result.get("claim_corrupt") or result.get("generation_stale"):
        return False
    decision = result.get("decision") or {}
    if decision.get("action") != "pass":
        return False
    blocking = {
        "wake_failed", "wake_skipped", "manager_failed",
        "manager_stale_result", "manager_generation_stale_result",
    }
    for ev in result.get("events") or []:
        if ev.get("type") in blocking:
            return False
        if ev.get("type") == "chat_turn_dispatched":
            # 응답이 아직 백그라운드에서 생성 중 — 이 체인에서 이어봤자 볼 게 없다.
            # 연속은 자식(run_chat_turn)의 publish 후 tick이 잇는다.
            return False
    return True


def _has_pending_candidates(space: str) -> bool:
    """병렬 후보 큐에 아직 공개(합성/선택)되지 않은 pending 후보가 남아 있는지."""
    try:
        return int(candidate_queue.snapshot(space).get("pending_count") or 0) > 0
    except Exception:
        return False


def _live_work_worker_tokens(space: str) -> set[str]:
    """살아 일하는(하트비트 신선 또는 콜드스타트 grace) 백그라운드 작업을 쥔 작업자 토큰 집합.

    갓 디스패치된 작업은 첫 heartbeat 전이라 missing→stale로 뜨지만 실제로는 '시작 중=살아있음'이다.
    이 콜드스타트 창(startup_grace)도 live로 센다(디스패치 직후 창을 놓치면 auto_continue가 뚫려
    작업자를 또 깨우고 '생각 중'이 깜빡인다 — 대표 신고)."""
    tokens: set[str] = set()
    try:
        for a in (task_registry.snapshot(space).get("active_items") or []):
            if not a.get("heartbeat_stale") or a.get("heartbeat_startup_grace"):
                w = a.get("worker_agent")
                if w:
                    tokens.add(str(w))
    except Exception:
        pass
    return tokens


def _has_live_work_task(space: str) -> bool:
    """방에 하트비트 신선한(살아 일하는) 백그라운드 작업이 있는지."""
    return bool(_live_work_worker_tokens(space))


def _free_dialogue_members(space: str) -> set[str]:
    """지금 대화를 이어받을 수 있는 '자유' 멤버(= live 작업을 쥐지 않은 멤버) 토큰.
    작업이 도는 동안 다자대화를 이 멤버들로 이어간다(바쁜 작업자 재-wake 없이)."""
    try:
        return _member_tokens(space) - _live_work_worker_tokens(space)
    except Exception:
        return set()


def _should_auto_continue(space: str, result: dict) -> bool:
    """자동 연속을 한 턴 더 돌릴지 — (1) 정상 공개된 단일 pass의 다음 협업 단계,
    또는 (2) parallel_pass 등으로 수집됐지만 아직 방에 공개되지 않은 pending 후보 정리.

    (2)가 없으면 병렬 위임 후 후보가 큐에 남은 채 방이 조용히 멈춘다 — 관리자가 다시 떠서
    select/synthesize로 공개하도록 이어줘야 한다. 후보가 모두 정리되면 pending_count가 0이 되어
    자연히 멈추고, 실패/stale 후보도 다음 턴의 합성·supersede로 self-correct된다."""
    if not result or not result.get("ok"):
        return False
    if result.get("stale") or result.get("claim_busy") or result.get("claim_corrupt") or result.get("generation_stale"):
        return False
    # 라이브 백그라운드 작업이 있을 때: 예전엔 무조건 멈춰, 작업이 도는 동안 방이 작업자 외엔
    # 침묵(1인 독백)했다(대표 신고: "에이전트끼리 대화를 안 한다"). 이제는 **바쁜 작업자 말고
    # 대화를 이어받을 자유 멤버가 있으면 계속**한다 — 작업은 뒤에서 async로 돌고, 그 사이 검토가·pm
    # 등이 다자대화를 진행한다(목표: 대화는 작업에 막히지 않는다). 아래 정상 연속 사유가 있을 때만
    # 잇고, 바쁜 작업자 재-wake는 자유멤버용 이벤트 안내 + _run_tick_chain의 하드가드가 막는다.
    # (자유 멤버가 없으면 = 얘기할 상대가 작업자뿐 → 예전처럼 멈춰 완료를 기다린다.)
    if _has_live_work_task(space) and not _free_dialogue_members(space):
        return False
    if _auto_continue_after_pass(result):
        return True
    if _decision_is_self_growth(result):       # 규칙/스킬/지식 반영 후 → 그 규칙대로 재작업할 기회를 준다
        return True
    if _decision_is_publish_each(result):      # 다자 의견 공개 후 → 토론 '반응 라운드'를 이을 기회를 준다(매니저가 판단)
        return True
    if _decision_is_update_summary(result):    # 누적요약 갱신(내부 유지보수) 후 → 본 턴(대표 발언 등)을 이어서 처리
        return True
    if _decision_is_launch_app(result):        # 앱 실행(작업의 전제) 성공 후 → 남은 요청(팀구성·방지침·조작 위임 등)을 이어서 처리
        return True
    return _has_pending_candidates(space)


def _decision_is_self_growth(result: dict) -> bool:
    """직전 결정이 자기성장(규칙/스킬/지식 반영)이었나 — 반영 후 재작업 턴을 잇기 위한 판정."""
    return bool((result or {}).get("ok")) and (result.get("decision") or {}).get("action") in SELF_GROWTH_ACTIONS


def _decision_is_publish_each(result: dict) -> bool:
    """직전 결정이 다자 공개(publish_each)였나 — 토론 반응 라운드를 잇기 위한 판정.
    이후 자동연속은 AUTO_CONTINUE_MAX_TURNS로 상한되며, 토론이 아니면 매니저가 stop한다."""
    return bool((result or {}).get("ok")) and (result.get("decision") or {}).get("action") == "publish_each"


def _decision_is_update_summary(result: dict) -> bool:
    """직전 결정이 롤링 누적요약 갱신이었나 — 요약은 내부 유지보수라 갱신 후 본 턴을 이어서 처리한다."""
    return bool((result or {}).get("ok")) and (result.get("decision") or {}).get("action") == "update_summary"


def _decision_is_launch_app(result: dict) -> bool:
    """직전 결정이 앱 실행(launch_app)이고 성공했나 — 앱 실행은 대개 '요청의 전부'가 아니라
    '작업의 전제'(예: "revit을 실행해서 거기서 작업하려고 팀을 구성하자")라, 실행 성공 후 본 턴을
    이어 매니저가 남은 요청(팀 구성 위임·방지침 update_guide·앱 조작 pass 등)을 처리하게 한다.
    실행이 곧 요청 전부인 단순 실행이면, 이어진 턴에서 매니저가 stop을 택해 그 경로가 응답의무를
    manager_closed로 닫는다(회귀안전). 실패한 실행은 이어붙이지 않는다(앱 오탐 재시도 루프 방지 —
    실패 노트로 대표 확인을 기다린다)."""
    if not (result or {}).get("ok"):
        return False
    if (result.get("decision") or {}).get("action") != "launch_app":
        return False
    for ev in result.get("events") or []:
        if ev.get("type") == "app_launched":
            return bool(ev.get("ok"))
    return False


def _pending_candidate_count(space: str) -> int:
    try:
        return int(candidate_queue.snapshot(space).get("pending_count") or 0)
    except Exception:
        return 0


def _candidate_drain_event(pending: int, drain: int) -> str:
    return (
        f"미공개 후보 정리(잔류 방지 drain {drain}/{MAX_CANDIDATE_DRAINS}): "
        f"CandidateQueue에 아직 방에 공개되지 않은 pending 후보 {pending}개가 남아 있다. "
        "자동 연속 턴을 다 써서 못 비웠거나 멈춘 상태다. **새 토론 라운드(parallel_pass)를 시작하지 말고**, "
        "이번 턴은 남은 후보를 정리해 비우는 데만 쓴다 — 여러 멤버가 각자 한마디씩 한 다자 대화면 "
        "publish_each(candidate_ids)로 각 후보를 그 멤버 말풍선으로 공개, 그대로 쓸 후보 하나면 select_candidate, "
        "여러 관점을 한 답으로 합칠 때만 synthesize_candidates, 쓰지 않을 후보는 discard_candidate. "
        "정리할 후보가 없으면 stop."
    )


def _open_user_obligations(space: str) -> list[dict]:
    """아직 답이 안 나간 대표 입력의 응답의무 — state='open' + '고아 assigned'(기한 초과).

    assigned/delegated는 원칙적으로 in-flight라 제외하지만, assigned로 넘긴 턴이 발행 없이 죽으면
    (엔진 실패가 wake_failed 경로를 안 타거나, 후보 전원 실패/전량 폐기) 의무가 assigned로 영구 고아가
    된다 — 실증(레빗_bcd7): assigned 11시간+ 방치, 대표 입력 무응답. overdue(assigned 기한 5분 초과)인
    assigned는 open으로 되돌려 sweep이 재구동하게 한다. delegated는 task/release 상태가 정본이라
    여기서 되살리지 않는다(reflow 백스톱 소관).
    target_actor=space_manager(=open_for_message가 다는 값)인 대표 입력으로 한정한다.
    """
    try:
        snap = response_obligation.snapshot(space)
    except Exception:
        return []
    if snap.get("ledger_corrupt"):
        return []
    out = []
    for item in snap.get("open_items") or []:
        if item.get("target_actor") not in ("space_manager", "space", ""):
            continue
        state = item.get("state")
        if state == "assigned" and item.get("overdue"):
            reopened = _safe_obligation(
                space,
                "reopen_orphaned_assigned",
                lambda it=item: response_obligation.reopen_for_context(
                    space,
                    {
                        "source_event_seq": it.get("source_event_seq"),
                        "source_message_id": it.get("source_message_id", ""),
                        "intent_id": it.get("intent_id", ""),
                        "conversation_thread_id": it.get("conversation_thread_id", ""),
                        "room_generation": it.get("room_generation"),
                    },
                    actor="시스템",
                    reason=f"assigned {item.get('age_ms', 0)}ms 경과·발행 없음 — 고아 의무 자동 재개",
                ),
            )
            if reopened and reopened.get("ok") and not reopened.get("terminal"):
                out.append({**item, "state": "open"})
            continue
        if state != "open":
            continue
        out.append(item)
    out.sort(key=lambda it: _as_int(it.get("source_event_seq")))
    return out


def _obligation_sweep_event(target: dict, remaining: int, sweep: int) -> str:
    # 한 sweep = 한 미응답 입력(가장 오래된 것)만 처리한다. 응답의무는 context 1건당 1건이 닫히므로
    # 여러 건을 한 번에 답하라고 하면 나머지가 open으로 남아 stuck/중복이 된다 — 오래된 순으로 하나씩.
    seq = target.get("source_event_seq")
    text = str(target.get("source_text_preview") or "").replace("\n", " ").strip()[:200]
    who = target.get("source_speaker") or "대표"
    queued = (
        f" (대기 중인 미응답 입력 {remaining - 1}건이 더 있다 — 그건 다음 턴에 차례로 처리되니 지금은 건드리지 마라.)"
        if remaining > 1 else ""
    )
    return (
        f"미응답 대표 입력 처리(빠른 연속 입력 누락 방지 sweep {sweep}/{MAX_OBLIGATION_SWEEPS}): "
        "빠르게 연속으로 들어온 아래 대표 입력이 아직 아무 답도 받지 못한 채(open 응답의무) 남아 있다. "
        "직전 턴들에서 다른 후보 정리/먼저 온 입력에만 답하느라 이 입력을 빠뜨렸다. "
        "이번 턴은 바로 이 입력 하나에만 답한다 — 의도를 보고 가장 적합한 멤버에게 pass해 답하게 하라"
        "(질문·잡담이면 채팅 즉답, 작업이면 그 흐름). 이미 끝난 다른 일은 건드리지 마라."
        f"{queued}\n"
        f"## 처리할 미응답 대표 입력 (event_seq={seq})\n{who}: {text}"
    )


def _obligation_sweep_context(target: dict) -> dict:
    return {
        "intent_id": target.get("intent_id", ""),
        "conversation_thread_id": target.get("conversation_thread_id", ""),
        "room_generation": target.get("room_generation"),
        "source_event_seq": target.get("source_event_seq"),
        "source_message_id": target.get("source_message_id", ""),
        "coalesced_pending_inputs": [{
            "event_seq": target.get("source_event_seq"),
            "message_id": target.get("source_message_id", ""),
            "intent_id": target.get("intent_id", ""),
            "conversation_thread_id": target.get("conversation_thread_id", ""),
            "room_generation": target.get("room_generation"),
            "text_preview": target.get("source_text_preview", ""),
            "recorded_at": target.get("updated_at", ""),
        }],
    }


def _run_tick_chain(space: str, event: str, context: dict | None = None, *, auto_continue: bool = False) -> dict:
    # 진입 전 flush: detached 채팅 턴이 남긴 공개 대기 후보(claim 경합으로 deferred됐던 것 포함)를
    # 결정적으로 먼저 공개한다 — 이 체인의 매니저가 최신 대화 위에서 판단하게 된다(LLM 비용 0).
    try:
        publish_ready_chat_candidates(space)
    except Exception:
        pass
    result = _tick_unlocked(space, event, context)
    combined = dict(result)
    combined["events"] = list(result.get("events") or [])
    if result.get("claim_busy") or result.get("stale") or result.get("claim_corrupt"):
        combined["redrive_runs"] = 0
        return combined
    redrive_runs = 0
    next_redrive = _redrive_event_from(result)
    while next_redrive and redrive_runs < MAX_REDRIVE_CHAIN:
        redrive_runs += 1
        next_event = next_redrive.get("event") or "새 입력 재처리 필요"
        next_context = next_redrive.get("context") or None
        if next_context is not None:
            next_context = {
                **next_context,
                "coalesced_redrive_events": next_redrive.get("redrive_events") or [],
                "coalesced_pending_inputs": next_redrive.get("coalesced_pending_inputs") or [],
            }
        combined["events"].append({
            "type": "manager_redrive_started",
            "event": next_event,
            "redrive_run": redrive_runs,
            "coalesced_pending_count": len(next_redrive.get("coalesced_pending_inputs") or []),
        })
        result = _tick_unlocked(space, next_event, next_context)
        combined["events"].extend(result.get("events") or [])
        if "decision" in result:
            combined["decision"] = result["decision"]
        combined["ok"] = bool(combined.get("ok", True) and result.get("ok", False))
        if result.get("claim_busy") or result.get("stale"):
            break
        next_redrive = _redrive_event_from(result)
    combined["redrive_runs"] = redrive_runs
    if next_redrive and redrive_runs >= MAX_REDRIVE_CHAIN:
        combined["events"].append({
            "type": "manager_redrive_limit_reached",
            "event": next_redrive.get("event") or "새 입력 재처리 필요",
            "redrive_runs": redrive_runs,
        })

    # 자동 연속: 에이전트 pass 응답 후 대표 입력 없이도 매니저가 다음 턴을 이어간다.
    # 매니저가 stop하면(승인·최종보고 필요 등) 즉시 멈춰 대표에게 핸드백한다.
    # auto_continue=False면(기본·테스트·수동 tick) 한 tick=한 턴 계약을 유지한다.
    auto_turns = 0
    baseline_user_seq = _latest_user_event_seq(space)
    while (
        auto_continue
        and auto_turns < AUTO_CONTINUE_MAX_TURNS
        and _should_auto_continue(space, result)
        and _latest_user_event_seq(space) <= baseline_user_seq
    ):
        auto_turns += 1
        growth_just_applied = _decision_is_self_growth(result)
        if _has_pending_candidates(space):
            cont_event = (
                f"병렬 후보 정리 자동 연속(turn {auto_turns}/{AUTO_CONTINUE_MAX_TURNS}): "
                "RoomStatusSnapshot.candidate_queue에 아직 방에 공개되지 않은 pending 후보가 있다. "
                "새 멤버를 깨우기 전에 후보부터 정리해라 — 여러 멤버가 각자 한마디씩 한 캐주얼 단톡이면 "
                "publish_each(candidate_ids)로 각 후보를 그 멤버 말풍선으로 따로 공개(다자 대화·사회자 침묵), "
                "그대로 공개할 후보 하나면 select_candidate, 여러 관점을 한 답으로 합칠 때만 synthesize_candidates, "
                "쓰지 않을 후보는 discard_candidate. 정리할 후보가 없으면 stop으로 대표에게 턴을 넘긴다."
            )
        elif growth_just_applied:
            cont_event = (
                f"규칙/스킬/지식 반영 후 자동 연속(turn {auto_turns}/{AUTO_CONTINUE_MAX_TURNS}): "
                "방금 대표 피드백을 스킬·지식·방지침에 반영했다. **그 반영된 규칙대로 '다시 해야 할 작업'이 있으면** "
                "(예: 직전에 형식·내용이 틀렸던 산출물 — 미리보기 빠짐 등) 담당 멤버에게 pass해 **업데이트된 스킬·지식대로 재작업**시켜라. "
                "재작업할 산출물이 없는 단순 사실/규칙 기록이면 stop으로 대표에게 턴을 넘긴다."
            )
        elif _decision_is_publish_each(result):
            cont_event = (
                f"다자 의견 공개 후 자동 연속(turn {auto_turns}/{AUTO_CONTINUE_MAX_TURNS}): "
                "방금 멤버들의 의견을 각자 말풍선으로 공개했다. **대표가 '토론'을 요청했고 멤버들이 아직 서로의 의견에 반응하지 않았다면**, "
                "한 '반응 라운드'를 이어라: parallel_pass로 각 멤버에게 **다른 멤버들이 방금 공개한 의견을 읽고 반박하거나 보강하라**고 시킨다"
                "(message에 무엇에 반응할지 명시 — 단순 의견 반복 금지, 동의·반론·추가근거로 토론을 진전시켜라). "
                "반응 라운드는 보통 1~2회면 충분하다. **토론이 무르익었으면 synthesize_candidates로 결론을 한 답으로 정리**하고, "
                "대표가 토론을 요청한 게 아니거나(단순 인사·잡담) 더 보탤 게 없으면 stop으로 대표에게 턴을 넘긴다."
            )
        elif _has_live_work_task(space):
            busy = ", ".join(sorted(_live_work_worker_tokens(space))) or "작업자"
            cont_event = (
                f"백그라운드 작업 진행 중 다자대화 자동 연속(turn {auto_turns}/{AUTO_CONTINUE_MAX_TURNS}): "
                f"지금 [{busy}]가 작업을 백그라운드로 수행 중이다(heartbeat 살아있음, 결과는 완료 시 돌아온다). "
                "**그 작업자를 다시 깨우지 마라** — 재호출은 '생각 중'만 헛깜빡이고 진전이 없다. "
                "대신 **작업을 안 쥔 다른 멤버로 대화를 진행**시켜 협업을 앞세워라: 예) 검토가에게 완성물 판정 기준을 "
                "미리 맞추게 하거나(pass), 여러 관점이 필요하면 parallel_pass로 검토가·pm을 함께, "
                "다음 단계·리스크를 정리하게 한다. 진행 확인이 꼭 필요하면 request_progress를 한 번만. "
                "지금 다른 멤버가 실질적으로 보탤 게 없으면 stop으로 대표에게 턴을 넘긴다(헛돌지 않는다)."
            )
        else:
            cont_event = (
                f"에이전트 응답 후 자동 연속(turn {auto_turns}/{AUTO_CONTINUE_MAX_TURNS}): "
                "협업 단계를 '진행'시켜라(예: 기획 끝났으면 구현, 구현 끝났으면 검수). "
                "직전에 답하거나 방금 작업을 맡은 멤버를 의미 없이 다시 깨우지 마라 — "
                "RoomStatusSnapshot.tasks에서 진행/완료 상태를 보고, 같은 일을 중복 위임하지 않는다. "
                "다음 단계가 분명하면 그 멤버에게 pass하고, 대표 승인·최종 보고가 필요하거나 협업이 일단락돼 더 할 일이 없으면 "
                "stop으로 멈춰 대표에게 턴을 넘긴다(핸드백)."
            )
        live_before = _live_work_worker_tokens(space)
        combined["events"].append({"type": "manager_auto_continue", "auto_turn": auto_turns})
        result = _tick_unlocked(space, cont_event, None)
        combined["events"].extend(result.get("events") or [])
        if "decision" in result:
            combined["decision"] = result["decision"]
        combined["ok"] = bool(combined.get("ok", True) and result.get("ok", False))
        if result.get("claim_busy") or result.get("stale"):
            break
        # 하드가드: 이번 자동연속이 '바쁜 작업자'를 다시 깨웠으면 더 잇지 않고 멈춘다. 안내대로라면
        # 자유 멤버로 갔어야 하는데 작업자를 재-wake했다면, 반복되면 '생각 중'이 깜빡이므로 여기서 끊는다.
        _dec = result.get("decision") or {}
        _rewoke_busy = (
            (_dec.get("action") == "pass" and _dec.get("wake") in live_before)
            or (_dec.get("action") == "parallel_pass"
                and any((t or {}).get("wake") in live_before for t in (_dec.get("targets") or [])))
        )
        if _rewoke_busy:
            combined["events"].append({
                "type": "manager_auto_continue_yielded",
                "reason": "would_rewake_busy_worker",
            })
            break
        # 자동 연속 중 새 대표 입력 coalesce가 끼면 그 redrive를 우선 처리한다.
        nr = _redrive_event_from(result)
        if nr and redrive_runs < MAX_REDRIVE_CHAIN:
            break
    combined["auto_continue_turns"] = auto_turns
    if (
        auto_continue
        and _should_auto_continue(space, result)
        and auto_turns < AUTO_CONTINUE_MAX_TURNS
        and _latest_user_event_seq(space) > baseline_user_seq
    ):
        # 새 대표 입력에 양보하고 자동 연속을 멈춤 — 그 입력의 tick이 재계획한다.
        combined["events"].append({
            "type": "manager_auto_continue_yielded",
            "reason": "pending_representative_input",
        })
    # 고아 redrive 배수(stuck 방지): 체인이 claim을 쥔 동안 들어온 입력이 release 때만 redrive로
    # 마킹돼 아무도 처리 못 하는 경우, 체인 종료 전에 직접 한 번 더 구동해 닫는다(bounded).
    drain_runs = 0
    while drain_runs < MAX_REDRIVE_CHAIN and _has_orphaned_redrive(space):
        drain_runs += 1
        combined["events"].append({"type": "manager_redrive_drained", "drain_run": drain_runs})
        result = _tick_unlocked(space, "release 후 미처리 입력 재구동(orphan redrive drain)", None)
        combined["events"].extend(result.get("events") or [])
        if "decision" in result:
            combined["decision"] = result["decision"]
        combined["ok"] = bool(combined.get("ok", True) and result.get("ok", False))
        if result.get("claim_busy") or result.get("stale"):
            break
    if drain_runs:
        combined["redrive_drains"] = drain_runs
    # 후보 잔류 방지: 체인이 끝나는데 아직 공개 안 된 pending 후보가 남아 있으면(자동연속이 토론 턴을 다
    # 써서 못 비웠거나 멈춤) 공개/선택/합성/폐기로 비운다. pending 수가 안 줄면 멈춤(무한루프 방지).
    # 의무 sweep보다 먼저 — 후보 공개가 응답의무를 닫아 sweep이 중복 응답하는 일을 줄인다.
    candidate_drains = 0
    prev_pending = -1
    while (
        auto_continue
        and candidate_drains < MAX_CANDIDATE_DRAINS
        and not (result.get("claim_busy") or result.get("stale") or result.get("claim_corrupt"))
    ):
        pending = _pending_candidate_count(space)
        if pending == 0:
            break
        if pending == prev_pending:
            break                                      # 직전 drain으로도 안 줄음 → 진전 없음, 멈춤
        prev_pending = pending
        candidate_drains += 1
        combined["events"].append({
            "type": "manager_candidate_drained",
            "drain": candidate_drains,
            "pending_count": pending,
        })
        result = _tick_unlocked(space, _candidate_drain_event(pending, candidate_drains), None)
        combined["events"].extend(result.get("events") or [])
        if "decision" in result:
            combined["decision"] = result["decision"]
        combined["ok"] = bool(combined.get("ok", True) and result.get("ok", False))
        if result.get("claim_busy") or result.get("stale"):
            break
    if candidate_drains:
        combined["candidate_drains"] = candidate_drains
    # 빠른 연속 입력 누락 방지(최종 안전망): redrive/자동연속/drain까지 끝났는데도 아직 답이 시작도
    # 안 된 대표 입력(open 응답의무)이 남아 있으면 — 매니저가 후보 정리나 한 입력에만 답하느라 빠르게
    # 온 나머지를 '읽기만' 하고 빠뜨린 경우 — 그 입력들을 명시해 한 번 더 구동해 닫는다. open 의무
    # 집합이 직전과 같으면(줄지 않으면) 멈춘다(무한루프 방지). bounded by MAX_OBLIGATION_SWEEPS.
    obligation_sweeps = 0
    prev_open_key: tuple | None = None
    while (
        auto_continue
        and obligation_sweeps < MAX_OBLIGATION_SWEEPS
        and not (result.get("claim_busy") or result.get("stale") or result.get("claim_corrupt"))
    ):
        open_items = _open_user_obligations(space)
        if not open_items:
            break
        open_key = tuple(sorted(str(it.get("obligation_id") or "") for it in open_items))
        if open_key == prev_open_key:
            break                                      # 직전 sweep으로도 안 줄음 → 진전 없음, 멈춤
        prev_open_key = open_key
        obligation_sweeps += 1
        target = open_items[0]                          # 가장 오래된 미응답 입력부터 하나씩
        combined["events"].append({
            "type": "manager_obligation_sweep",
            "sweep": obligation_sweeps,
            "open_count": len(open_items),
            "target_event_seq": target.get("source_event_seq"),
        })
        result = _tick_unlocked(
            space,
            _obligation_sweep_event(target, len(open_items), obligation_sweeps),
            _obligation_sweep_context(target),
        )
        combined["events"].extend(result.get("events") or [])
        if "decision" in result:
            combined["decision"] = result["decision"]
        combined["ok"] = bool(combined.get("ok", True) and result.get("ok", False))
        if result.get("claim_busy") or result.get("stale"):
            break
    if obligation_sweeps:
        combined["obligation_sweeps"] = obligation_sweeps
    last_decision = combined.get("decision") or {}
    if auto_turns > 0 and last_decision.get("action") == "stop":
        # 자동 연속이 매니저 stop으로 끝났다 = 대표에게 턴을 넘김(핸드백). 대시보드가 강조할 수 있게 알린다.
        combined["events"].append({
            "type": "manager_handback_to_representative",
            "reason": last_decision.get("reason", ""),
            "auto_continue_turns": auto_turns,
        })
        _mark_representative_handback(space, reason=last_decision.get("reason", ""))
    return combined


def _recent_lines(space: str, limit: int = 30) -> str:
    rows = read(space, limit)
    lines = []
    for r in rows:
        speaker = r.get("화자", "?")
        content = str(r.get("내용", "")).replace("\n", " ").strip()
        lines.append(f"- {speaker}: {content[:800]}")
    return "\n".join(lines) if lines else "(아직 대화 없음)"


MAX_MEMBER_PROFILES = 30
MAX_MEMBER_ROLE_CHARS = 800


def _read_excerpt_status(path: Path, limit: int = MAX_MEMBER_ROLE_CHARS) -> dict:
    if not path.exists():
        return {"status": "missing", "text": "", "error": ""}
    try:
        with path.open("r", encoding="utf-8") as f:
            text = f.read(limit + 1)
    except Exception as exc:
        return {"status": "read_error", "text": "", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {
        "status": "ok_truncated" if len(text) > limit else "ok",
        "text": text[:limit],
        "error": "",
    }


def _member_profiles(space: str, members: list[dict]) -> list[dict]:
    profiles = []
    for member in members[:MAX_MEMBER_PROFILES]:
        if not isinstance(member, dict):
            continue
        token = str(member.get("토큰") or "").strip()
        if not token:
            continue
        person_dir = PEOPLE / token
        seat = person_dir / "공간" / space
        role_path = person_dir / "role.md"
        role = _read_excerpt_status(role_path)
        runtime_dir = seat if seat.exists() else person_dir
        rt = runtime.read_runtime(runtime_dir)
        root_rt = runtime.read_runtime(person_dir)
        profiles.append({
            "이름": member.get("이름", ""),
            "코드": member.get("코드", ""),
            "토큰": token,
            "joined": seat.exists(),
            "role_path": str(role_path.relative_to(PEOPLE.parent)) if role_path.exists() else "",
            "role_status": role["status"],
            "role_error": role["error"],
            "role_excerpt": role["text"],
            "seat_runtime": {
                "engine": rt.get("engine", ""),
                "model": rt.get("model", ""),
                "updated": rt.get("updated", ""),
                "source": rt.get("source", ""),
                "runtime_source": rt.get("runtime_source", ""),
                "runtime_error": rt.get("runtime_error", ""),
            },
            "default_runtime": {
                "engine": root_rt.get("engine", ""),
                "model": root_rt.get("model", ""),
                "updated": root_rt.get("updated", ""),
                "source": root_rt.get("source", ""),
                "runtime_source": root_rt.get("runtime_source", ""),
                "runtime_error": root_rt.get("runtime_error", ""),
            },
            "work_capable": (seat / "작업").exists(),
        })
    if len(members) > MAX_MEMBER_PROFILES:
        profiles.append({
            "truncated": True,
            "omitted_member_count": len(members) - MAX_MEMBER_PROFILES,
            "reason": "member_profile_budget",
        })
    return profiles


def _slim_rows(rows: list[dict], keys: list[str], limit: int) -> list[dict]:
    out = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        out.append({key: row.get(key, "") for key in keys if row.get(key, "") not in (None, "")})
    return out


def _source_health(space: str, *, guide_status: dict | None = None, summary_status: dict | None = None, members_status: dict | None = None) -> dict:
    guide_status = guide_status or _read_text_status(SPACES / space / "공간지침.md")
    summary_status = summary_status or _read_text_status(SPACES / space / "요약.md")
    members_status = members_status or _members_status(space)
    return {
        "schema": "RoomSourceHealth.prompt.v1",
        "space_guide": {
            "status": guide_status.get("status", ""),
            "error": guide_status.get("error", ""),
            "char_count": len(guide_status.get("text", "")),
        },
        "space_summary": {
            "status": summary_status.get("status", ""),
            "error": summary_status.get("error", ""),
            "char_count": len(summary_status.get("text", "")),
        },
        "members_json": {
            "status": members_status.get("status", ""),
            "error": members_status.get("error", ""),
            "member_count": len(members_status.get("data") or []),
        },
    }


def _prompt_room_status_snapshot(space: str) -> dict:
    try:
        st = status(space)
    except Exception as exc:
        return {
            "schema": "RoomStatusSnapshot.prompt.v1",
            "snapshot_status": "read_error",
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
    tasks = st.get("tasks") or {}
    release = st.get("release_queue") or {}
    publish = st.get("publish_ledger") or {}
    candidates = st.get("candidate_queue") or {}
    context_packs = st.get("context_packs") or {}
    latest_turn_handoff = context_packs.get("latest_turn_handoff") or {}
    latest_return_contract = latest_turn_handoff.get("return_contract") or {}
    learning = st.get("learning") or {}
    claim = st.get("manager_claim") or {}
    rapid = st.get("rapid_input") or {}
    obligations = st.get("response_obligations") or {}
    memory = st.get("space_memory") or {}
    # 결재대기(pending_approval)·승인후 미실행(approved) 작업계획 — 종전 스냅샷엔 work_plans가 없어 사회자가
    # '이미 결재 대기 중인 계획'을 못 봤고, 같은 worker를 계속 깨워 중복 요청이 쌓였다(실증 2026-06-29:
    # 게이트된 3단계를 9분간 6번 재요청 → pending plan 6개·재선언 헛돎). 이걸 노출해 사회자가 재디스패치
    # 대신 '대표 결재 대기'를 인식·존중하게 한다(목표.md: 헛돎·폭주 금지).
    try:
        _unstarted_plans = work_plan.list_plans(space, states={work_plan.PENDING, work_plan.APPROVED})
    except Exception:
        _unstarted_plans = []
    _pending_plans = [p for p in _unstarted_plans if p.get("state") == work_plan.PENDING]
    return {
        "schema": "RoomStatusSnapshot.prompt.v1",
        "snapshot_status": "ok",
        "state": st.get("상태", ""),
        "label": st.get("label", ""),
        "last_action": st.get("last_action", ""),
        "status_stale": bool(st.get("status_stale")),
        "manager_read_lag": st.get("manager_read_lag", 0),
        "projection_lag": st.get("projection_lag", 0),
        "work_plans": {
            "pending_approval_count": len(_pending_plans),
            "approved_unstarted_count": len(_unstarted_plans) - len(_pending_plans),
            "pending_items": [
                {
                    "plan_id": p.get("plan_id", ""),
                    "worker": p.get("worker", ""),
                    "requesting_agent": p.get("requesting_agent", ""),
                    "objective_preview": str(p.get("objective", ""))[:140],
                    "state": p.get("state", ""),
                }
                for p in _pending_plans[:6]
            ],
            "note": (
                "결재 대기 중인 작업계획이 있다. 이미 대표 결재를 기다리는 중이니, **같은 worker에게 같은 일을 "
                "다시 시키지 마라(action=work/pass로 재디스패치 금지 — 중복 plan·재선언 헛돎을 만든다).** "
                "대표 결재가 나야 실행된다 — 결재가 필요하면 stop으로 대표에게 턴을 넘겨 승인/반려를 받아라. "
                "그 작업이 구조적으로 막혔거나(예: 런타임 권한·자격증명 부재) 더 진행 불가하면 stop으로 대표에게 "
                "사실과 필요한 것을 보고하라(같은 작업을 반복 기동하지 마라)."
                if _pending_plans else ""
            ),
        },
        "space_memory": {
            "memory_source": memory.get("memory_source", ""),
            "projection_available": bool(memory.get("projection_available")),
            "projection_state": memory.get("projection_state", ""),
            "projection_id": memory.get("projection_id", ""),
            "projection_version": memory.get("projection_version", 0),
            "source": memory.get("source", ""),
            "projection_method": memory.get("projection_method", {}),
            "applied_event_seq": memory.get("applied_event_seq", 0),
            "latest_event_seq": memory.get("latest_event_seq", 0),
            "projection_lag": memory.get("projection_lag", 0),
            "active_context_summary": memory.get("active_context_summary", ""),
            "representative_requests": _slim_rows(memory.get("representative_requests") or [], [
                "event_seq", "message_id", "speaker", "content_preview",
                "intent_id", "conversation_thread_id", "room_generation",
            ], 5),
            "user_directive_items": _slim_rows(memory.get("user_directive_items") or [], [
                "event_seq", "message_id", "speaker", "content_preview",
                "thread_id", "precedence_rank", "precedence_hint",
                "intent_id", "conversation_thread_id", "room_generation",
            ], 6),
            "active_topic_threads": _slim_rows(memory.get("active_topic_threads") or [], [
                "thread_id", "status", "latest_event_seq", "event_gap",
                "message_count", "latest_user_request", "latest_assistant_reply",
            ], 4),
            "dormant_topic_threads": _slim_rows(memory.get("dormant_topic_threads") or [], [
                "thread_id", "status", "latest_event_seq", "event_gap",
                "message_count", "latest_user_request",
            ], 4),
            "precedence_policy": memory.get("precedence_policy", {}),
            "conflict_hints": memory.get("conflict_hints", {}),
            "source_refs": _slim_rows(memory.get("source_refs") or [], [
                "event_seq", "message_id", "speaker", "role",
                "intent_id", "conversation_thread_id", "room_generation",
            ], 8),
            "projection_corrupt": bool(memory.get("projection_corrupt")),
            "projection_errors": memory.get("projection_errors", []),
        },
        "rapid_input": {
            "pending_input_count": rapid.get("pending_input_count", 0),
            "unread_event_count": rapid.get("unread_event_count", 0),
            "read_until_event_seq": rapid.get("read_until_event_seq", 0),
            "latest_event_seq": rapid.get("latest_event_seq", 0),
            "latest_pending_event_seq": rapid.get("latest_pending_event_seq", 0),
            "pending_items": _slim_rows(rapid.get("pending_items") or [], [
                "event_seq", "message_id", "client_message_id", "intent_id",
                "conversation_thread_id", "room_generation", "text_preview",
            ], 6),
        },
        "active_wakes": _slim_rows(st.get("active_wakes") or [], [
            "type", "actor", "state", "event", "read_until_event_seq",
            "lease_expires_at_utc", "intent_id", "conversation_thread_id",
            "room_generation", "context_pack_id", "wake_id", "turn_handoff_id",
            "wake_pack_manifest_id",
        ], 8),
        "stale_wakes": _slim_rows(st.get("stale_wakes") or [], [
            "type", "actor", "state", "event", "reason", "intent_id",
            "conversation_thread_id", "room_generation",
        ], 8),
        "member_projection_lag": _slim_rows(st.get("projection_lag_by_member") or [], [
            "token", "tail_lag", "missing_count", "last_event_seq", "seat_missing",
            "projection_baseline_event_seq", "projection_required_event_count", "late_join_baseline",
        ], 12),
        "seat_projection_baselines": _slim_rows(st.get("seat_projection_baselines") or [], [
            "token", "projection_baseline_event_seq", "projection_required_event_count",
            "last_event_seq", "tail_lag", "missing_count", "seat_missing", "late_join_baseline",
        ], 12),
        "tasks": {
            "task_count": tasks.get("task_count", 0),
            "state_counts": tasks.get("state_counts", {}),
            "latest_task_id": tasks.get("latest_task_id", ""),
            "latest_state": tasks.get("latest_state", ""),
            "latest_worker": tasks.get("latest_worker", ""),
            "running_count": tasks.get("running_count", 0),
            "cancel_requested_count": tasks.get("cancel_requested_count", 0),
            "active_count": tasks.get("active_count", 0),
            "stale_task_count": tasks.get("stale_task_count", 0),
            "pending_steering_count": tasks.get("pending_steering_count", 0),
            "heartbeat_stale_threshold_ms": tasks.get("heartbeat_stale_threshold_ms", 0),
            "progress_report_due_count": tasks.get("progress_report_due_count", 0),
            "progress_report_requested_count": tasks.get("progress_report_requested_count", 0),
            "progress_report_due_threshold_ms": tasks.get("progress_report_due_threshold_ms", 0),
            "steering_runtime_count": tasks.get("steering_runtime_count", 0),
            "steering_runtime_counts": tasks.get("steering_runtime_counts", {}),
            "runtime_activity_count": tasks.get("runtime_activity_count", 0),
            "runtime_activity_items": _slim_rows(tasks.get("runtime_activity_items") or [], [
                "type", "event", "state", "label", "detail", "at",
                "task_id", "worker_agent", "steering_action", "steering_seq",
                "steering_requested_at", "latest_steering_requested_at",
                "steering_reason_code", "last_heartbeat_at", "heartbeat_phase",
                "cancel_requested_at", "intent_id",
                "conversation_thread_id", "room_generation", "source_event_seq",
            ], 8),
            "active_items": _slim_rows(tasks.get("active_items") or [], [
                "task_id", "worker_agent", "state", "cancel_requested",
                "cancellation_request_id", "cancellation_reason",
                "last_heartbeat_at", "heartbeat_phase", "heartbeat_note",
                "heartbeat_age_ms", "heartbeat_stale", "heartbeat_stale_threshold_ms",
                "runner_timeout_sec", "heartbeat_interval_sec",
                "latest_steering_seq", "latest_steering_action", "latest_steering_instruction",
                "latest_steering_reason_code", "pending_steering_ack",
                "pending_ack_steering_seq", "pending_ack_steering_action",
                "pending_ack_steering_instruction", "pending_ack_steering_event_id",
                "latest_steering_control_request_source_event_seq",
                "latest_steering_control_request_room_generation",
                "progress_report_due", "progress_report_due_reason",
                "progress_report_requested_since_heartbeat", "progress_report_due_threshold_ms",
                "work_settings_source", "work_settings_source_chain",
                "steering_runtime_state", "steering_runtime_label",
                "intent_id", "conversation_thread_id", "room_generation",
            ], 8),
            "hold_task_count": tasks.get("hold_task_count", 0),
            "latest_hold_error": tasks.get("latest_hold_error", ""),
            "latest_release_queue_state": tasks.get("latest_release_queue_state", ""),
            "release_followup_missing_count": tasks.get("release_followup_missing_count", 0),
            "release_followup_missing_items": _slim_rows(tasks.get("release_followup_missing_items") or [], [
                "task_id", "worker_agent", "task_pack_id", "finalized_at",
                "intent_id", "conversation_thread_id", "room_generation", "source_event_seq",
            ], 5),
            "release_enqueue_failed_count": tasks.get("release_enqueue_failed_count", 0),
            "ledger_corrupt": bool(tasks.get("ledger_corrupt")),
            "ledger_errors": tasks.get("ledger_errors", []),
        },
        "release_queue": {
            "release_count": release.get("release_count", 0),
            "pending_count": release.get("pending_count", 0),
            "latest_release_id": release.get("latest_release_id", ""),
            "latest_source_task_id": release.get("latest_source_task_id", ""),
            "latest_state": release.get("latest_state", ""),
            "latest_approval_state": release.get("latest_approval_state", ""),
            "pending_items": _slim_rows(release.get("pending_items") or [], [
                "release_id", "source_task_id", "state", "approval_state",
                "public_summary", "intent_id", "conversation_thread_id", "room_generation",
            ], 5),
            "approved_items": _slim_rows(release.get("approved_items") or [], [
                "release_id", "source_task_id", "state", "approval_state",
                "public_summary", "intent_id", "conversation_thread_id", "room_generation",
            ], 5),
            "ledger_corrupt": bool(release.get("ledger_corrupt")),
            "ledger_errors": release.get("ledger_errors", []),
        },
        "publish_ledger": {
            "counts": publish.get("counts", {}),
            "ledger_corrupt": bool(publish.get("ledger_corrupt", False)),
        },
        "candidate_queue": {
            "candidate_count": candidates.get("candidate_count", 0),
            "pending_count": candidates.get("pending_count", 0),
            "error_count": candidates.get("error_count", 0),
            "state_counts": candidates.get("state_counts", {}),
            "latest_candidate_id": candidates.get("latest_candidate_id", ""),
            "latest_turn_id": candidates.get("latest_turn_id", ""),
            "latest_target_agent": candidates.get("latest_target_agent", ""),
            "latest_state": candidates.get("latest_state", ""),
            "pending_items": _slim_rows(candidates.get("pending_items") or [], [
                "candidate_id", "turn_id", "target_agent", "state",
                "reply_preview", "structured_action", "presentation_mode",
                "intent_id", "conversation_thread_id", "room_generation",
            ], 8),
            "error_items": _slim_rows(candidates.get("error_items") or [], [
                "candidate_id", "turn_id", "target_agent", "state",
                "error", "join_policy", "presentation_mode",
                "intent_id", "conversation_thread_id", "room_generation",
            ], 8),
            "prompt_items": _slim_rows(candidates.get("prompt_items") or [], [
                "candidate_id", "turn_id", "target_agent", "state",
                "reply", "structured_action", "structured_public_reply",
                "manager_message", "reason", "intent_id",
                "conversation_thread_id", "room_generation",
            ], 6),
            "ledger_corrupt": bool(candidates.get("ledger_corrupt")),
            "ledger_errors": candidates.get("ledger_errors", []),
        },
        "response_obligations": {
            "obligation_count": obligations.get("obligation_count", 0),
            "open_count": obligations.get("open_count", 0),
            "overdue_open_count": obligations.get("overdue_open_count", 0),
            "oldest_open_age_ms": obligations.get("oldest_open_age_ms", 0),
            "next_deadline_at": obligations.get("next_deadline_at", ""),
            "state_counts": obligations.get("state_counts", {}),
            "open_items": _slim_rows(obligations.get("open_items") or [], [
                "obligation_id", "state", "target_actor", "assigned_to",
                "source_event_seq", "source_message_id", "source_text_preview",
                "intent_id", "conversation_thread_id", "room_generation",
                "age_ms", "deadline_at", "overdue", "policy_reason", "policy_blockers",
            ], 8),
            "overdue_items": _slim_rows(obligations.get("overdue_items") or [], [
                "obligation_id", "state", "assigned_to", "source_event_seq",
                "source_message_id", "age_ms", "deadline_at", "policy_reason",
            ], 8),
            "ledger_corrupt": bool(obligations.get("ledger_corrupt")),
            "ledger_errors": obligations.get("ledger_errors", []),
        },
        "context_packs": {
            "wake_manifest_count": context_packs.get("wake_manifest_count", 0),
            "turn_handoff_count": context_packs.get("turn_handoff_count", 0),
            "latest_manifest_id": context_packs.get("latest_manifest_id", ""),
            "latest_manifest_state": context_packs.get("latest_manifest_state", ""),
            "latest_delivered_at": context_packs.get("latest_delivered_at", ""),
            "latest_recipient": context_packs.get("latest_recipient", ""),
            "latest_delivery_type": context_packs.get("latest_delivery_type", ""),
            "latest_lesson_pack_status": context_packs.get("latest_lesson_pack_status", ""),
            "latest_memory_source": context_packs.get("latest_memory_source", ""),
            "latest_memory_projection_id": context_packs.get("latest_memory_projection_id", ""),
            "latest_memory_projection_version": context_packs.get("latest_memory_projection_version", 0),
            "latest_memory_applied_event_seq": context_packs.get("latest_memory_applied_event_seq", 0),
            "latest_memory_projection_lag": context_packs.get("latest_memory_projection_lag", 0),
            "latest_turn_handoff": {
                "target_agent": latest_turn_handoff.get("target_agent", ""),
                "delivery_type": latest_turn_handoff.get("delivery_type", ""),
                "wake_id": latest_turn_handoff.get("wake_id", ""),
                "turn_handoff_id": latest_turn_handoff.get("turn_handoff_id", ""),
                "context_pack_id": latest_turn_handoff.get("context_pack_id", ""),
                "source_event_seq": latest_turn_handoff.get("source_event_seq"),
                "response_target": latest_turn_handoff.get("response_target") or {},
                "why_you": latest_turn_handoff.get("why_you", ""),
                "manager_message_preview": latest_turn_handoff.get("manager_message_preview", ""),
                "return_contract": {
                    "kind": latest_return_contract.get("kind", ""),
                    "published_by": latest_return_contract.get("published_by", ""),
                    "request_work_route": latest_return_contract.get("request_work_route", ""),
                    "structured_request_schema": latest_return_contract.get("structured_request_schema", ""),
                },
                "lesson_pack_status": latest_turn_handoff.get("lesson_pack_status", ""),
                "must_apply_lesson_count": latest_turn_handoff.get("must_apply_lesson_count", 0),
            },
            "ledger_corrupt": bool(context_packs.get("ledger_corrupt")),
            "ledger_errors": context_packs.get("ledger_errors", []),
        },
        "learning": {
            "lesson_count": learning.get("lesson_count", 0),
            "post_interaction_evaluation_count": learning.get("post_interaction_evaluation_count", 0),
            "post_task_evaluation_count": learning.get("post_task_evaluation_count", 0),
            "promotion_candidate_count": learning.get("promotion_candidate_count", 0),
            "promotion_pending_count": learning.get("promotion_pending_count", 0),
            "promotion_approved_count": learning.get("promotion_approved_count", 0),
            "promotion_rejected_count": learning.get("promotion_rejected_count", 0),
            "promotion_review_required": bool(learning.get("promotion_review_required")),
            "promotion_apply_pending_count": learning.get("promotion_apply_pending_count", 0),
            "promotion_apply_blocked_count": learning.get("promotion_apply_blocked_count", 0),
            "promotion_pending_items": _slim_rows(learning.get("promotion_pending_items") or [], [
                "promotion_id", "lesson_id", "target_kind", "state",
                "title", "instruction_preview", "target_path_suggestion",
            ], 5),
            "promotion_apply_pending_items": _slim_rows(learning.get("promotion_apply_pending_items") or [], [
                "promotion_id", "lesson_id", "target_kind", "state",
                "apply_state", "title", "target_path_suggestion",
            ], 5),
            "resource_apply_count": learning.get("resource_apply_count", 0),
            "resource_apply_applied_count": learning.get("resource_apply_applied_count", 0),
            "resource_apply_blocked_count": learning.get("resource_apply_blocked_count", 0),
            "resource_apply_state_counts": learning.get("resource_apply_state_counts", {}),
            "resource_apply_items": _slim_rows(learning.get("resource_apply_items") or [], [
                "apply_id", "promotion_id", "lesson_id", "target_kind",
                "state", "target_path", "detail",
            ], 5),
            "growth_gap_count": learning.get("growth_gap_count", 0),
            "growth_gap_open_count": learning.get("growth_gap_open_count", 0),
            "growth_gap_review_required": bool(learning.get("growth_gap_review_required")),
            "growth_gap_state_counts": learning.get("growth_gap_state_counts", {}),
            "growth_gap_target_counts": learning.get("growth_gap_target_counts", {}),
            "growth_gap_open_items": _slim_rows(learning.get("growth_gap_open_items") or [], [
                "gap_id", "state", "evaluation_id", "target_kind",
                "promotion_id", "recommended_next_action", "reason",
            ], 5),
            "ledger_corrupt": bool(learning.get("ledger_corrupt")),
            "ledger_errors": learning.get("ledger_errors", []),
        },
        "manager_claim": {
            "active": bool(claim.get("active")),
            "claim_seq": claim.get("claim_seq", 0),
            "lease_expires_at_utc": claim.get("lease_expires_at_utc", ""),
            "manager_redrive_required": bool(claim.get("manager_redrive_required")),
            "claim_file_corrupt": bool(claim.get("claim_file_corrupt")),
        },
        "failures": _slim_rows(st.get("failures") or [], [
            "상태", "actor", "target", "label", "detail", "task_id",
            "task_pack_id", "release_queue_state",
        ], MAX_PROMPT_STATUS_FAILURES),
        "recovery_actions": [str(item) for item in (st.get("recovery_actions") or [])[:MAX_PROMPT_STATUS_RECOVERY]],
    }


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    try:
        obj, end = decoder.raw_decode(text)
    except Exception:
        return {}
    if text[end:].strip():
        return {}
    return obj if isinstance(obj, dict) else {}


def _publish_manager_note(space: str, content: str, context: dict | None, claim: dict | None) -> str:
    """공간관리가 방에 짧은 확인/안내 메시지를 남긴다(자기성장 반영 알림 등) — 대표가 '반영됐다/안 멈췄다'를 본다.

    설계상 공간관리는 내용 참여(의견)는 안 하지만, 지시 반영 확인은 synthesize처럼 공간관리 명의로 허용.
    effect_id(intent+내용)로 멱등 — redrive 중복 발행 방지. 실패해도 디스패치를 끊지 않는다(best-effort)."""
    content = str(content or "").strip()
    if not content:
        return ""
    ctx = context or {}
    effect_id = orchestration.effect_id("manager_note", space, ctx.get("intent_id", ""),
                                        ctx.get("source_event_seq", ""), content[:100])
    try:
        pc = publish_ledger.claim_publish(
            space, publish_effect_id=effect_id,
            manager_claim_token=(claim or {}).get("claim_token", ""), manager_claim_context=claim,
            context=ctx, publisher="space_manager", speaker="공간관리")
        if pc.get("already_committed"):
            return pc.get("published_message_id", "")
        res = publish_ledger.append_public_message(
            space, publish_effect_id=effect_id,
            publish_ledger_claim=pc.get("publish_ledger_claim", ""),
            manager_claim_token=(claim or {}).get("claim_token", ""), manager_claim_context=claim,
            published_message_id=pc.get("published_message_id", ""), intent_stale_guard_passed=True,
            speaker_name="공간관리", speaker_code="manager", role="assistant", content=content,
            context=ctx, extra={"kind": "manager_note"})
        return (res.get("record") or {}).get("message_id", "")
    except Exception:
        return ""


def _dispatch_skill_authoring(space: str, *, skill_name: str, desc: str, cond: str, instr: str,
                             is_new: bool, context: dict | None, claim: dict | None) -> bool:
    """스킬 본문 작성/고도화를 doer(작업에이전트)에게 위임 — skill-creator 스킬 기준으로 제대로 짓게(설계 §4).

    크루드 인라인 본문 대신, doer가 skill-creator를 응용해 본문을 작성/개선(고도화)한다.
    저위험 자동승인→비동기 디스패치(tick 안 막음). 멤버(doer) 없으면 위임 생략(케이스는 이미 반영됨)."""
    try:
        members = json.loads((SPACES / space / "멤버.json").read_text(encoding="utf-8"))
    except Exception:
        members = []
    doer = next((str((m or {}).get("토큰") or "").strip()
                 for m in (members if isinstance(members, list) else []) if str((m or {}).get("토큰") or "").strip()), "")
    if not doer:
        return False
    verb = "새로 작성" if is_new else "개선(고도화)"
    objective = (
        f"[스킬 저작 — skill-creator 기준] 스킬 '{skill_name}'을 {verb}하라. 크루드 템플릿 금지, 제대로 만든다.\n"
        f"1. 발견기로 'skill-creator' 스킬을 찾아 그 절차·기준(언제 쓰나·핵심 원칙·절차·안티패턴 구조 + 발견 최적화 description)을 따른다.\n"
        f"2. 대상: 스킬 '{skill_name}' — 이미 있으면 그 SKILL.md 본문을 개선, 없으면 스킬/추가/{skill_name}/에 신규 작성.\n"
        f"3. 담을 핵심 교훈: 조건='{cond}', 지시='{instr}'. (케이스는 cases.jsonl에 이미 반영됨 — 본문엔 보편 절차로 녹여라.)\n"
        f"4. description(발견용): {desc}\n"
        f"5. 작성 후 부를 표현 3개로 발견기 검색해 top-3에 뜨는지 확인, 안 뜨면 description 보강.\n"
        f"6. 결과.md에 무엇을 어떻게 개선했는지 요약."
    )
    assessment = {"needs_approval": False, "approval_mode": "auto_manager", "system_level": "low",
                  "system_signals": [], "approval_reason": "스킬 저작(저위험 내부 작업)",
                  "agent_needs_approval": False, "agent_level": "low", "agent_reason": ""}
    try:
        registered = work_plan.register(space, requesting_agent="공간관리", worker=doer,
                                        objective=objective, plan_steps=[f"skill-creator로 '{skill_name}' {verb}"],
                                        assessment=assessment, context=context)
        plan_id = registered["record"]["plan_id"]
        work_plan.approve(space, plan_id, actor="공간관리", mode="auto_manager", reason="스킬 저작 자동승인(저위험)")
        effect_id = orchestration.effect_id("skill_authoring", space, skill_name, (context or {}).get("intent_id", ""))
        _dispatch_work_plan(space, plan_id=plan_id, wake="공간관리", worker=doer,
                            objective_for_work=objective, effect_id=effect_id, context=context, claim=claim,
                            handoff_context_pack={}, turn_handoff_pack={})
        _publish_manager_note(
            space, f"📌 '{skill_name}' 스킬을 skill-creator 기준으로 {verb}하는 작업을 {doer.rsplit('_', 1)[0]}에게 맡겼어요(고도화).",
            context, claim)
        return True
    except Exception as exc:
        _append_activity(space, {"상태": "skill_authoring_dispatch_failed", "시각": now_iso(), "actor": "공간관리",
                                 "target": skill_name, "label": "스킬 저작 위임 실패", "detail": str(exc)[:160],
                                 **_context_fields(context), **_claim_fields(claim)})
        return False


_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.DOTALL)


def _extract_decision_json(text: str) -> dict:
    """매니저 결정 파싱: (1) 통째 코드펜스만 벗긴 엄격 파싱 우선,
    (2) 실패하면 응답 안에서 'action'을 가진 결정 JSON을 구제(salvage)한다.

    Claude는 'JSON만' 잘 지키지만 Gemini(Antigravity) 등은 습관적으로 설명 산문을 덧붙여
    엄격 파싱이 매번 거부→3회 재시도 소진→manager_failed로 막혔다. 산문이 섞여도 결정 JSON
    한 덩이를 살려 사회자가 Gemini에서도 동작하게 한다. 여러 개면 **마지막** 것(실제 결정은
    보통 끝에 온다). 'action'이 없는 JSON·완전 비-JSON은 여전히 {} → 재시도(계약 유지).
    """
    raw = (text or "").strip()
    m = _CODE_FENCE_RE.match(raw)
    strict = _extract_json(m.group(1).strip() if m else raw)
    if strict.get("action"):
        return strict
    # salvage: 응답 전체에서 top-level JSON 객체를 훑어 'action'을 가진 마지막 것을 고른다.
    decoder = json.JSONDecoder()
    inner = m.group(1) if m else raw
    found = None
    for start in (i for i, ch in enumerate(inner) if ch == "{"):
        try:
            obj, _ = decoder.raw_decode(inner[start:])
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("action"):
            found = obj
    return found if found is not None else strict


def _extract_json_lenient(text: str) -> dict:
    """LLM 출력에서 JSON 한 덩이를 관대하게 추출(코드펜스·앞뒤 산문 허용). 실패 시 {}."""
    obj = _extract_json(text)
    if obj:
        return obj
    t = (text or "").replace("```json", "").replace("```", "")
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            parsed = json.loads(t[i:j + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


# 신규 스킬 중복 방지(설계 §4): 어휘는 후보만 추리고, **의미는 LLM이 판정**한다.
# 어휘 유사도는 false-positive가 심해('환영카드'↔'미리보기' 오매치) 차단 근거로 못 쓴다 — 후보 게이트만.
SKILL_DEDUP_CANDIDATE_MIN_SIGNAL = 2.0   # 이 미만이면 LLM 호출 안 함(명백히 무관 → 신규 생성)


def _semantic_resource_duplicate(space: str, kind_label: str, name: str, desc: str, detail: str,
                                 candidates: list[dict]) -> str | None:
    """새 자원(스킬/지식)이 기존 후보 중 하나와 **본질적으로 같은 목적**이면 그 기존 이름, 아니면 None.

    LLM 한 번으로 의미 비교(설계: 어휘 아닌 임베딩/LLM — 어휘는 false-positive). 실패·불명확 = None
    (fail-open → 신규 생성: 교훈 잃기보다 중복 한 번이 덜 위험). 반환은 후보 목록 안의 이름만(환각 차단).
    """
    if not candidates:
        return None
    names = {c.get("name") for c in candidates if c.get("name")}
    listing = "\n".join(f"- {c.get('name')}: {str(c.get('description') or '')[:200]}" for c in candidates)
    prompt = (
        f"{kind_label} 중복 판정. 아래 '새 {kind_label}'이 '기존 {kind_label} 목록' 중 하나와 "
        f"**본질적으로 같은 목적·역할**이면 그 기존 이름을, 아니면 빈 문자열을 답하라. "
        f"**단어가 겹쳐도 목적이 다르면 빈 문자열**이다(예: '환영카드 색상'과 '말풍선 미리보기'는 다른 것).\n\n"
        f"[새 {kind_label}]\n이름: {name}\n설명: {desc}\n참고: {detail}\n\n"
        f"[기존 {kind_label} 목록]\n{listing}\n\n"
        '반드시 JSON 한 줄로만 답하라: {"same_as":"<기존 이름 또는 빈 문자열>","reason":"한 줄 근거"}'
    )
    try:
        manager_dir = SPACES / space / MANAGER_DIRNAME
        raw = engine.run_engine(manager_dir, prompt, timeout=180)
        same = str((_extract_json_lenient(raw) or {}).get("same_as") or "").strip()
        return same if same in names else None
    except Exception:
        return None


def _parallel_target_specs(obj: dict) -> list[dict]:
    raw_targets = obj.get("targets")
    if not isinstance(raw_targets, list):
        return []
    specs = []
    fallback_message = str(obj.get("message") or "").strip()
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        wake = str(item.get("wake") or item.get("target_agent") or "").strip()
        message = str(item.get("message") or fallback_message).strip()
        reason = str(item.get("reason") or obj.get("reason") or "").strip()
        specs.append({"wake": wake, "message": message, "reason": reason})
    return specs


def _normalize_decision(space: str, decision: dict, member_tokens: set[str]) -> dict:
    """약한 모델(Gemini) 구제 정규화 — 검증 직전에 한 번.
    - parallel_pass targets의 wake를 멤버 별칭(표시이름/코드)으로 줘도 토큰으로 해석한다.
    - parallel_pass인데 유효 target이 1개뿐이면 단일 pass로 강등한다(토론 1명이라도 진행 > 실패).
    """
    if not isinstance(decision, dict):
        return decision
    if str(decision.get("action") or "").strip() != "parallel_pass":
        return decision
    aliases = _member_aliases(space)

    def _resolve(wake: str) -> str:
        wake = str(wake or "").strip()
        if wake and wake not in member_tokens:
            resolved = chat_result.resolve_worker(wake, member_tokens, aliases)
            if resolved:
                return resolved
        return wake

    raw_targets = decision.get("targets")
    if not isinstance(raw_targets, list):
        raw_targets = []
    fixed = []
    seen_wakes = set()
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        wake = _resolve(item.get("wake") or item.get("target_agent"))
        if wake in seen_wakes:
            continue  # 같은 멤버 중복 지정 — 첫 항목만 유지(검증 거부 대신 구제)
        seen_wakes.add(wake)
        fixed.append({**item, "wake": wake})
    decision = {**decision, "targets": fixed}
    top_message = str(decision.get("message") or "").strip()
    if not fixed:
        # 실방 반복 위반(하루 6 tick 재요청): targets 리스트 없이 top-level wake/message로 낸
        # parallel_pass. 재요청으로 같은 실수를 반복시키는 대신 단일 pass로 강등해 진행시킨다.
        top_wake = _resolve(decision.get("wake"))
        if top_wake in member_tokens and top_message:
            return {**decision, "action": "pass", "wake": top_wake, "message": top_message}
        return decision
    valid = []
    for t in fixed:
        if t.get("wake") not in member_tokens:
            continue
        message = str(t.get("message") or "").strip() or top_message
        if message:
            valid.append({**t, "message": message})
    if len(valid) == 1:
        t = valid[0]
        return {**decision, "action": "pass", "wake": t["wake"],
                "message": str(t.get("message") or ""), "targets": fixed}
    if len(valid) == len(fixed) and any(
        str(t.get("message") or "").strip() != str(v.get("message") or "")
        for t, v in zip(fixed, valid)
    ):
        # 개별 message가 비어 top-level message로 채워진 경우 반영
        return {**decision, "targets": valid}
    return decision


def _candidate_ids_from_decision(obj: dict) -> list[str]:
    ids = []
    raw_ids = obj.get("candidate_ids")
    if isinstance(raw_ids, list):
        ids.extend(str(item or "").strip() for item in raw_ids)
    raw_candidates = obj.get("candidates")
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if isinstance(item, dict):
                ids.append(str(item.get("candidate_id") or "").strip())
            else:
                ids.append(str(item or "").strip())
    single = str(obj.get("candidate_id") or "").strip()
    if single:
        ids.insert(0, single)
    out = []
    seen = set()
    for item in ids:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _task_ids_from_decision(obj: dict) -> list[str]:
    ids = []
    raw_ids = obj.get("task_ids")
    if isinstance(raw_ids, list):
        ids.extend(str(item or "").strip() for item in raw_ids)
    raw_tasks = obj.get("tasks")
    if isinstance(raw_tasks, list):
        for item in raw_tasks:
            if isinstance(item, dict):
                ids.append(str(item.get("task_id") or "").strip())
            else:
                ids.append(str(item or "").strip())
    single = str(obj.get("task_id") or "").strip()
    if single:
        ids.insert(0, single)
    out = []
    seen = set()
    for item in ids:
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _task_instruction_from_decision(obj: dict) -> str:
    return str(obj.get("instruction") or obj.get("message") or "").strip()


def _decision_error(obj: dict, member_tokens: set[str] | None = None) -> str:
    # action만 구조적 필수. wake/message/reason은 키가 없어도 ""로 본다 — 액션마다 의미가 달라
    # (parallel_pass엔 top-level wake/message가 무의미) 키 존재를 강제하면 Gemini가 매번 걸린다.
    # 실제로 필요한 값은 아래 액션별 규칙이 잡는다(pass=wake+message, parallel_pass=targets 등).
    if not isinstance(obj, dict) or not str(obj.get("action") or "").strip():
        return "action 필드가 필요함(pass/parallel_pass/stop 등)"
    action = str(obj.get("action") or "").strip()
    wake = str(obj.get("wake") or "").strip()
    message = str(obj.get("message") or "").strip()
    if action not in MANAGER_ACTIONS:
        return "action은 pass, parallel_pass, select_candidate, synthesize_candidates, publish_each, discard_candidate, cancel_task, revise_task, request_progress, propose_case, propose_skill, update_guide, propose_knowledge, stop 중 하나여야 함"
    if action == "stop" and (wake or message):
        return "stop이면 wake와 message는 빈 문자열이어야 함"
    if action in TASK_CONTROL_ACTIONS and wake:
        return "작업 제어 action이면 wake는 빈 문자열이어야 함"
    if action == "pass":
        if not wake or not message:
            return "pass이면 wake와 message가 비어 있으면 안 됨"
        if member_tokens is not None and wake not in member_tokens:
            return "pass의 wake가 멤버 토큰이 아님"
    if action == "parallel_pass":
        specs = _parallel_target_specs(obj)
        if len(specs) < 2:
            return "parallel_pass이면 targets가 2개 이상이어야 함"
        if len(specs) > MAX_PARALLEL_PASS_TARGETS:
            return f"parallel_pass targets는 최대 {MAX_PARALLEL_PASS_TARGETS}개"
        seen = set()
        for spec in specs:
            target = spec.get("wake", "")
            if not target or not spec.get("message", ""):
                return "parallel_pass targets의 wake와 message가 비어 있으면 안 됨"
            if member_tokens is not None and target not in member_tokens:
                return "parallel_pass targets의 wake가 멤버 토큰이 아님"
            if target in seen:
                return "parallel_pass targets에 같은 멤버를 중복 지정할 수 없음"
            seen.add(target)
        join_policy = str(obj.get("join_policy") or "timeout_then_partial").strip()
        if join_policy not in PARALLEL_JOIN_POLICIES:
            return "parallel_pass join_policy가 지원되지 않음"
        presentation_mode = str(obj.get("presentation_mode") or "silent_reference").strip()
        if presentation_mode not in PARALLEL_PRESENTATION_MODES:
            return "parallel_pass presentation_mode이 지원되지 않음"
    if action == "select_candidate":
        if wake:
            return "select_candidate이면 wake는 빈 문자열이어야 함"
        ids = _candidate_ids_from_decision(obj)
        if len(ids) != 1:
            return "select_candidate는 candidate_id 하나가 필요함"
    if action == "synthesize_candidates":
        if wake:
            return "synthesize_candidates이면 wake는 빈 문자열이어야 함"
        ids = _candidate_ids_from_decision(obj)
        if len(ids) < 2:
            return "synthesize_candidates는 candidate_ids 2개 이상이 필요함"
        if not message:
            return "synthesize_candidates는 message에 공개할 합성문이 필요함"
    if action == "publish_each":
        if wake:
            return "publish_each이면 wake는 빈 문자열이어야 함"
        ids = _candidate_ids_from_decision(obj)
        if len(ids) < 1:
            return "publish_each는 candidate_ids가 1개 이상 필요함"
    if action == "update_guide":
        if wake:
            return "update_guide이면 wake는 빈 문자열이어야 함"
        if not message:
            return "update_guide는 message에 방지침에 기록할 규칙이 필요함"
    if action == "update_summary":
        if wake:
            return "update_summary이면 wake는 빈 문자열이어야 함"
        if not message:
            return "update_summary는 message에 갱신된 누적요약 전문이 필요함"
    if action == "launch_app":
        if wake:
            return "launch_app이면 wake는 빈 문자열이어야 함(시스템이 실행 대행)"
        if not str(obj.get("app") or "").strip():
            return "launch_app은 app 필드에 앱 이름 또는 앱 폴더경로가 필요함"
    if action == "propose_knowledge":
        if wake:
            return "propose_knowledge이면 wake는 빈 문자열이어야 함"
        if not message:
            return "propose_knowledge는 message에 기록할 사실/기준이 필요함"
        if not str(obj.get("knowledge") or "").strip():
            return "propose_knowledge는 knowledge(지식 주제 이름 — 전역 자원으로 졸업)가 필요함"
        if "/" in str(obj.get("knowledge") or "") or "\\" in str(obj.get("knowledge") or ""):
            return "propose_knowledge knowledge 이름에 경로 구분자(/ \\) 금지"
        if not str(obj.get("description") or "").strip():
            return "propose_knowledge는 description(발견용 설명 — 무엇을·언제·표현·핵심용어)이 필요함"
    if action == "discard_candidate":
        if wake:
            return "discard_candidate이면 wake는 빈 문자열이어야 함"
        ids = _candidate_ids_from_decision(obj)
        if not ids:
            return "discard_candidate는 candidate_id 또는 candidate_ids가 필요함"
    if action == "cancel_task":
        ids = _task_ids_from_decision(obj)
        if not ids:
            return "cancel_task는 task_id 또는 task_ids가 필요함"
        if len(ids) > MAX_TASK_CONTROL_TARGETS:
            return f"cancel_task task_ids는 최대 {MAX_TASK_CONTROL_TARGETS}개"
    if action == "revise_task":
        ids = _task_ids_from_decision(obj)
        if len(ids) != 1:
            return "revise_task는 task_id 하나가 필요함"
        if not _task_instruction_from_decision(obj):
            return "revise_task는 instruction 또는 message가 필요함"
    if action == "request_progress":
        ids = _task_ids_from_decision(obj)
        if not ids:
            return "request_progress는 task_id 또는 task_ids가 필요함"
        if len(ids) > MAX_TASK_CONTROL_TARGETS:
            return f"request_progress task_ids는 최대 {MAX_TASK_CONTROL_TARGETS}개"
    if action == "propose_case":
        if wake:
            return "propose_case이면 wake는 빈 문자열이어야 함"
        skill = str(obj.get("skill") or "").strip()
        if not skill:
            return "propose_case는 skill(스킬 이름)이 필요함"
        cand = obj.get("candidate")
        if not isinstance(cand, dict):
            return "propose_case는 candidate 객체가 필요함"
        missing = [
            f for f in ("condition", "instruction", "polarity", "action", "routing_kind", "judgment_rationale", "source_quote")
            if not str(cand.get(f) or "").strip()
        ]
        if missing:
            return f"propose_case candidate 필드 누락: {', '.join(missing)} (에이전트 판단 필수)"
    if action == "propose_skill":
        if wake:
            return "propose_skill이면 wake는 빈 문자열이어야 함"
        name = str(obj.get("skill") or "").strip()
        if not name:
            return "propose_skill은 skill(새 스킬 이름)이 필요함"
        if "/" in name or "\\" in name:
            return "propose_skill 스킬 이름에 경로 구분자(/ \\) 금지"
        if not str(obj.get("description") or "").strip():
            return "propose_skill은 description(발견용 설명 — 무엇을·언제·표현·핵심용어)이 필요함"
        cand = obj.get("candidate")
        if not isinstance(cand, dict):
            return "propose_skill은 candidate(새 스킬의 첫 케이스) 객체가 필요함"
        missing = [
            f for f in ("condition", "instruction", "polarity", "routing_kind", "judgment_rationale", "source_quote")
            if not str(cand.get(f) or "").strip()
        ]
        if missing:
            return f"propose_skill candidate 필드 누락: {', '.join(missing)} (에이전트 판단 필수)"
    return ""


def _valid_decision(obj: dict) -> bool:
    return not _decision_error(obj)


# 2단계(스마트) 대상 — 만능 스키마에서 '전용 페이로드'를 따로 받는 게 약한 모델(Gemini)에 쉬운 복잡 액션.
PHASE2_ACTIONS = {"parallel_pass", "pass", "select_candidate", "synthesize_candidates", "publish_each", "discard_candidate"}


def _phase2_prompt(space: str, action: str, decision: dict, member_tokens: set[str]) -> str | None:
    """2단계(스마트): 1단계에서 고른 action의 '전용 필드'만, 선택지를 나열해 받는다.
    복잡한 만능 스키마를 한 번에 채우는 대신 작고 명확한 과제로 좁혀 JSON 준수율을 높인다.
    대상 액션이 아니면 None(→ 일반 재시도)."""
    if action not in PHASE2_ACTIONS:
        return None
    reason_json = json.dumps(str(decision.get("reason") or "").strip(), ensure_ascii=False)
    members = sorted(t for t in (member_tokens or set()) if t)
    head = (
        "## 2단계 — 고른 행동의 세부만 채워라\n"
        f'직전에 너는 action="{action}"을(를) 골랐다. 이제 이 행동에 필요한 필드만 정확히 채운 '
        "유효 JSON 한 덩이만 내라. 다른 행동으로 바꾸지 말고, JSON 밖 설명도 붙이지 마라.\n\n"
    )
    if action == "parallel_pass":
        return head + (
            f"- wake는 반드시 이 멤버 토큰 중에서만 고른다: {members}\n"
            "- 2명 이상에게 각자 무엇을 시킬지 targets를 채운다. message는 절대 비우지 마라.\n"
            '{"action":"parallel_pass","wake":"","message":"","reason":' + reason_json +
            ',"targets":[{"wake":"<멤버토큰>","message":"<시킬 내용>","reason":"<왜>"},'
            '{"wake":"<다른 멤버토큰>","message":"<시킬 내용>","reason":"<왜>"}]}'
        )
    if action == "pass":
        return head + (
            f"- wake는 반드시 이 멤버 토큰 중 하나: {members}\n"
            '{"action":"pass","wake":"<멤버토큰>","message":"<시킬 내용/전달 메시지>","reason":' + reason_json + "}"
        )
    # 후보 정리 계열 — pending 후보 id를 나열해 그 안에서만 고르게 한다.
    items = candidate_queue.snapshot(space).get("pending_items", [])
    ids = [it.get("candidate_id") for it in items if it.get("candidate_id")]
    listing = "\n".join(
        f"  · {it.get('candidate_id')} ({it.get('target_agent')}): {str(it.get('reply_preview') or '')[:50]}"
        for it in items
    ) or "  (대기 후보 없음)"
    ids_json = json.dumps(ids, ensure_ascii=False)
    if action == "select_candidate":
        return head + (
            f"- 그대로 공개할 후보 1개를 아래에서 고른다:\n{listing}\n"
            '{"action":"select_candidate","candidate_id":"<위 id 중 하나>","wake":"","message":"","reason":' + reason_json + "}"
        )
    if action == "discard_candidate":
        return head + (
            f"- 버릴 후보를 아래에서 고른다:\n{listing}\n"
            '{"action":"discard_candidate","candidate_ids":["<버릴 id들>"],"wake":"","message":"","reason":' + reason_json + "}"
        )
    # synthesize_candidates / publish_each — 보통 모든 후보를 대상으로
    return head + (
        f"- 대상 후보들(candidate_ids에 아래 id를 넣어라 — 보통 전부):\n{listing}\n"
        f'{{"action":"{action}","candidate_ids":{ids_json},"wake":"",'
        '"message":"<합성문/안내 또는 빈칸>","reason":' + reason_json + "}"
    )


def _retry_prompt(base_prompt: str, raw: str, error: str, attempt: int) -> str:
    # 점진적 단순화: 마지막 시도에선 복잡한 형식(parallel_pass/targets/후보합성)을 버리고
    # 가장 단순한 유효 결정으로 빠지게 한다. 같은 실수 반복으로 manager_failed→대표가 다시 채팅해야
    # 하던 막힘을, 한 멤버 pass(대화 진행) 또는 stop(깔끔한 핸드백)으로 자가복구시킨다.
    simplify = ""
    if attempt >= MAX_DECISION_ATTEMPTS:
        simplify = (
            "\n## ⚠️ 마지막 시도 — 가장 단순하게\n"
            "- 복잡한 형식(parallel_pass·targets·synthesize_candidates)이 자꾸 실패하면 **포기하고 단순한 결정**을 내라.\n"
            "- 누군가 답·작업을 해야 하면: action=\"pass\", wake=\"멤버 토큰 하나\", message=\"시킬 내용\"(둘 다 비우지 마라).\n"
            "- 더 시킬 게 없거나 대표 차례면: action=\"stop\", wake=\"\", message=\"\".\n"
            "- targets/candidate_ids 같은 배열 필드는 이번엔 쓰지 마라.\n"
        )
    return (
        f"{base_prompt}\n\n"
        "## 이전 응답 형식 오류\n"
        f"- 재시도: {attempt}/{MAX_DECISION_ATTEMPTS}\n"
        f"- 오류: {error}\n"
        "- 아래 이전 응답은 시스템이 처리할 수 없었다.\n"
        f"```text\n{str(raw)[-2000:]}\n```\n"
        f"{simplify}\n"
        "다른 설명 없이 마지막 줄에 아래 네 필드를 모두 가진 유효 JSON 한 덩어리만 다시 내라.\n"
        '{"action":"pass 또는 parallel_pass 또는 select_candidate 또는 synthesize_candidates 또는 discard_candidate 또는 cancel_task 또는 revise_task 또는 request_progress 또는 propose_case 또는 propose_skill 또는 update_guide 또는 propose_knowledge 또는 stop","wake":"멤버 토큰 또는 빈 문자열","message":"전달/합성/재지시 메시지 또는 빈 문자열","reason":"한 줄 이유","candidate_id":"후보 id","candidate_ids":["후보 id"],"task_id":"작업 id","task_ids":["작업 id"],"instruction":"작업 재지시 또는 진행 보고 요청","targets":[{"wake":"멤버 토큰","message":"전달 메시지","reason":"왜 깨우는지"}],"skill":"propose_case/propose_skill일 때 스킬 이름","knowledge":"propose_knowledge일 때 지식 주제 이름(전역 자원으로 졸업)","description":"propose_skill/propose_knowledge일 때 발견용 설명","candidate":{"condition":"","instruction":"","polarity":"worked|failed","action":"add_case|supersede","routing_kind":"procedural","judgment_rationale":"","source_quote":""}}'
    )


def _space_context(space: str, event: str, context: dict | None = None, manager_context_pack: dict | None = None) -> str:
    sdir = SPACES / space
    guide_status = _read_text_status(sdir / "공간지침.md")
    summary_status = _read_text_status(sdir / "요약.md")
    members_result = _members_status(space)
    guide = guide_status.get("text", "")
    summary = summary_status.get("text", "")
    members = members_result.get("data") or []
    member_profiles = _member_profiles(space, members if isinstance(members, list) else [])
    context = context or {}
    pack_prompt = context_pack.render_manager_context_prompt(manager_context_pack) if manager_context_pack else ""
    source_health = _source_health(
        space,
        guide_status=guide_status,
        summary_status=summary_status,
        members_status=members_result,
    )
    room_status = _prompt_room_status_snapshot(space)
    coalesced_pending_inputs = context.get("coalesced_pending_inputs") or []
    coalesced_prompt = (
        "## 빠른 연속 입력 묶음\n"
        "아래 입력들은 현재 manager 실행 중 들어와 redrive로 합쳐진 대표 입력이다. "
        "이번 판단에서 누락하지 말고 함께 고려하라.\n"
        f"{json.dumps(coalesced_pending_inputs[-12:], ensure_ascii=False, indent=2)}\n\n"
        if coalesced_pending_inputs else ""
    )
    mid_flight_guidance = (
        "## 대표 중간 입력 판단 (중요)\n"
        "진행 중인 작업/대화가 있는데 대표가 새로 말했다면, 그 말의 의도를 먼저 판단한다. "
        "대표의 새 발언이 무조건 '하던 일 전부 중단'을 뜻하지 않는다.\n"
        "- 별개·무관한 이야기거나 단순 코멘트·질문이면: 진행 중 작업(task)을 건드리지 말고, 그 입력만 별도로 처리(답변/적합한 멤버 pass)한 뒤 원래 작업은 그대로 이어가게 둔다.\n"
        "- 추가 요청이면: 기존 작업은 두고 새 작업 흐름을 더한다.\n"
        "- 방향 전환·정정·재지시 의도가 분명하면: RoomStatusSnapshot.tasks에서 대상 task_id를 특정해 revise_task(재지시) 또는 cancel_task(취소)를 낸다.\n"
        "- 진행 상황을 묻는 것이면(예: '진행중이야?', '어디까지 됐어?'): **답을 미루지 말고 즉시 답한다.** "
        "RoomStatusSnapshot.tasks의 실행 상태(작업 진행 중 여부·heartbeat·단계)를 근거로, **작업 중이 아닌 멤버(채팅에이전트, 예: pm)에게 pass**해 '지금 무엇을 백그라운드로 하고 있고 어디까지 왔는지'를 대표에게 바로 알린다. "
        "작업은 별도 세션에서 백그라운드로 계속 도니, request_progress로 대표 답변을 대신하지 마라 — request_progress는 대표 즉답과 별개로, 작업이 오래 정체돼(heartbeat stale) 실제 체크포인트가 필요할 때만 쓴다. 대표는 항상 '지금 작업 중이다'를 바로 확인받아야 한다.\n"
        "- 승인·확인·답변이면: 그에 맞춰 보류 중이던 공개/다음 단계를 진행한다.\n"
        "근거 없이 진행 중 작업을 멈추거나, 반대로 분명한 재지시를 무시하지 않는다. 애매하면 작업을 보존하고 대표 의도를 좁히는 쪽으로 판단한다.\n\n"
    )
    return (
        "# 공간관리 훅\n\n"
        "너는 이 공간의 흐름을 관리한다. 채팅 참여자처럼 답하지 말고, 턴을 넘길지 멈출지만 결정하라.\n\n"
        f"{MANAGER_DECISION_JSON_CONTRACT}\n"
        f"## 이번 이벤트\n{event}\n\n"
        "## 실행 컨텍스트\n"
        f"- space_id: {space}\n"
        f"- intent_id: {context.get('intent_id', '')}\n"
        f"- conversation_thread_id: {context.get('conversation_thread_id', '')}\n"
        f"- room_generation: {context.get('room_generation')}\n"
        f"- source_event_seq: {context.get('source_event_seq')}\n"
        f"- source_message_id: {context.get('source_message_id', '')}\n\n"
        f"## 공간 지침\n{guide}\n\n"
        f"## 공간 요약\n{summary}\n\n"
        f"## 멤버\n{json.dumps(members, ensure_ascii=False, indent=2)}\n\n"
        "## 멤버 프로필과 런타임\n"
        "아래 정보를 보고 누가 이번 턴에 가장 적합한지 판단하라. "
        "role_excerpt는 역할/전문성, seat_runtime은 실제 이 공간 좌석에서 깨어날 엔진·모델이다.\n"
        f"{json.dumps(member_profiles, ensure_ascii=False, indent=2)}\n\n"
        "## 소스/원장 상태\n"
        "read_error, invalid_shape, ledger_corrupt가 있으면 무리하게 pass하지 말고 복구 가능한 판단만 하라.\n"
        f"{json.dumps(source_health, ensure_ascii=False, indent=2)}\n\n"
        "## 오케스트레이션 상태 스냅샷\n"
        "현재 실행 중인 wake, 지연, 작업, 공개 대기열, 실패와 복구 액션을 보고 다음 턴을 결정하라.\n"
        f"{json.dumps(room_status, ensure_ascii=False, indent=2)}\n\n"
        f"{coalesced_prompt}"
        f"{mid_flight_guidance}"
        f"## 최근 대화\n{_recent_lines(space)}\n\n"
        f"{pack_prompt}\n"
        "## 운영 원칙\n"
        "- ★ 대표 응답 최우선(무엇보다 먼저): 아직 처리 안 된 대표 입력(질문·확인·중단·방향전환)이 있으면, 자동 구간 진행이나 다른 어떤 작업보다 그것부터 처리한다. 진행 중 작업이 있어도 대표 응답을 미루지 마라 — 작업은 백그라운드로 두고 대표에게 먼저 응답한다. 절대 대표를 기다리게 두지 마라.\n"
        "  · 질문·확인·짧은 대화면 담당 에이전트에게 pass하되 message에 '대표 질문/요청에 지금 빠르게 채팅으로만 답하라(새 작업·release 만들지 말 것)'을 명시한다. → 에이전트가 즉답한다.\n"
        "  · '중단/멈춰/그만/하지마/취소' 의도면 즉시 cancel_task로 진행 중 작업(task_id 확인)을 멈춘 뒤, 담당 에이전트에게 pass해 '대표 지시로 중단했음'을 짧게 알리게 한다.\n"
        "  · '방향을 이렇게 바꿔라'면 revise_task로 진행 작업에 반영하거나, 새 방향이면 cancel 후 재위임한다.\n"
        "  · 보완·수정·다시 만들기 등 '작업'이 필요한 요청이면, 담당 에이전트에게 pass하되 message에 '먼저 한 줄로 접수 답(public_reply)을 하고, 무거운 수정은 이 채팅 턴에서 직접 하지 말고 request_work로 작업을 만들어 진행하라'를 명시한다(채팅 턴에서 무거운 작업을 직접 하면 시간초과로 접수 답조차 사라진다).\n"
        "- 공간관리는 공개 말풍선으로 대표에게 직접 답하지 않는다. 반드시 action으로 턴을 넘기거나 멈춘다(대표 응답은 에이전트가 한다).\n"
        "- room_status.rapid_input.pending_input_count가 1보다 크면 빠른 연속 입력이다. 이번 이벤트 하나만 보지 말고 "
        "rapid_input.pending_items와 최근 대화를 함께 읽어 누락된 대표 입력이 없게 하라. **단, '서로 다른' 질문 여러 개를 "
        "parallel_pass 하나로 묶지 마라** — parallel_pass는 '한 주제에 여러 의견'이 필요할 때만 쓴다. 빠르게 온 게 서로 다른 "
        "질문/요청이면 각각을 개별 pass로 따로 처리해 입력 하나가 응답 하나로 깔끔히 닫히게 한다(한 턴에 한 입력만 답해도 된다 — "
        "남은 입력은 시스템이 다음 턴으로 자동 이어 처리한다). 그래야 형제 입력이 안 닫힌 채 중복 응답·잔류로 남지 않는다.\n"
        "- 대표의 발언이 일상 대화, 인사, 짧은 질문, 능력 확인처럼 방 안 대화로 자연스럽게 이어질 수 있으면 "
        "공간 지침과 멤버 역할을 보고 가장 적합한 참여 에이전트에게 pass한다.\n"
        "- 대표의 발언이 구현, 파일 수정, 조사, 검토, 장기 작업, 여러 관점 비교, 결재가 필요한 결과 공개에 가깝다면 "
        "단순 답변으로 끝내지 말고 ChatAgentResult/request_work, parallel_pass, 후보 정리, stop 중 적합한 흐름을 고른다.\n"
        "- 대표가 진행 중인 작업의 취소, 재지시, 진행 보고를 요구하면 텍스트 자체를 시스템이 해석하지 않는다. "
        "네가 오케스트레이션 상태의 task_id와 맥락을 확인한 뒤 필요할 때만 cancel_task, revise_task, request_progress를 명시한다.\n"
        "- 멤버가 여러 명이면 이름이 보인다는 이유만으로 단일 pass하지 말고, 대표 의도와 방 목표상 단일 답변인지 "
        "병렬 의견/작업 분배가 필요한지 판단한다.\n"
        "- room_status.space_memory는 요약 정본 projection이다. memory_source가 legacy_summary이거나 projection_lag가 있으면 "
        "요약만 근거로 정책 반영, 작업 생성, 자원 변경을 확정하지 말고 source_refs/최근 대화/상태를 함께 확인한다.\n"
        "- room_status.response_obligations.open_items가 있으면 아직 닫히지 않은 응답 의무다. 새 턴을 만들기 전에 답변, 작업 위임, 공개 대기, manager_closed 중 무엇으로 닫을지 고려한다.\n"
        "- 대표 피드백이나 작업 결과에서 '다음에 이 스킬을 이렇게 써야 한다'는 절차적 교훈을 네가 읽고 판단했다면 propose_case로 그 스킬의 케이스(경우의 수)를 발의한다. "
        "기계적으로 키워드만 보고 넣지 말고, 긍정/부정을 이해해 condition·instruction·polarity·근거(judgment_rationale·source_quote)를 직접 채운다. 개인/회사 식별정보는 일반화하고, 사실/선호(절차 아님)는 케이스 대상이 아니다. "
        "마땅한 스킬이 없으면 propose_skill로 새 스킬을 만든다(skill=새 이름·description=발견용 설명·candidate=첫 케이스). 방 무관 일반 규칙은 update_guide(방 한정)가 아니라 propose_case/propose_skill(global)로 보내야 다른 단톡방에도 전파된다.\n"
        "마지막 줄에는 반드시 law_space.md의 JSON 형식만 유효하게 남겨라. "
        "action은 pass, parallel_pass, select_candidate, synthesize_candidates, discard_candidate, cancel_task, revise_task, request_progress, propose_case, propose_skill, update_guide, propose_knowledge, stop 중 하나다. "
        "parallel_pass는 여러 독립 의견이 필요할 때만 쓰고, targets 2~4명을 지정한다. "
        "cancel_task는 task_id 또는 task_ids를 지정하고, revise_task는 task_id 하나와 instruction 또는 message를 지정한다. "
        "request_progress는 task_id 또는 task_ids를 지정한다. "
        "parallel_pass 결과는 방에 바로 공개되지 않고 CandidateQueue에 저장된다. "
        "timeout_then_partial은 제한 시간 안에 끝난 후보만 저장하고 늦은 후보는 취소/오류 후보로 남긴다. "
        "반드시 모두 필요할 때만 wait_all을 써라. "
        "candidate_queue.prompt_items에 pending 후보가 있으면 새 멤버를 깨우기 전에 "
        "select_candidate, synthesize_candidates, discard_candidate 중 하나로 후보를 정리할 수 있다. "
        "synthesize_candidates에서 같은 병렬 턴의 일부 후보만 쓰면 남은 pending 후보는 자동 폐기되므로 reason에 제외 이유를 남겨라."
    )


def post(
    space: str,
    text: str,
    requester: str = "대표",
    run_manager: bool = True,
    client_message_id: str | None = None,
    manager_requested: bool | None = None,
) -> dict:
    if not (SPACES / space).exists():
        raise ValueError(f"공간 없음: {space}")
    clean = (text or "").strip()
    if not clean:
        raise ValueError("내용이 비었음")
    existing = _existing_client_message(space, client_message_id)
    ingress = (
        orchestration.context_from_message(existing, space)
        if existing
        else orchestration.prepare_ingress(space, clean, requester, client_message_id)
    )
    rec = record(
        space,
        {
            "시각": now_iso(), "공간": space, "화자": requester, "코드": "boss",
            "역할": "user", "내용": clean, "client_message_id": client_message_id or "",
            "run_manager_requested": run_manager if manager_requested is None else bool(manager_requested),
            "ingress_type": ingress.get("ingress_type", "message"),
            "cancel_replan_fence": bool(ingress.get("cancel_replan_fence")),
            "effect_id": ingress.get("effect_id", ""),
            **_context_fields(ingress),
        },
        dedupe_client_message_id=bool(client_message_id),
        dedupe_effect_id=True,
    )
    stored = rec["record"]
    if not rec.get("duplicate"):
        # 대표가 다시 발언했다 → 이전 핸드백 강조 해제.
        _clear_representative_handback(space)
    context = orchestration.context_from_message(stored, space)
    ack = {
        "message_id": stored.get("message_id", ""),
        "client_message_id": stored.get("client_message_id", client_message_id or ""),
        "event_seq": stored.get("event_seq"),
        "intent_id": stored.get("intent_id", ""),
        "conversation_thread_id": stored.get("conversation_thread_id", ""),
        "room_generation": stored.get("room_generation"),
        "effect_id": stored.get("effect_id", ""),
        "ingress_type": stored.get("ingress_type", "message"),
        "duplicate": bool(rec.get("duplicate")),
        "duplicate_by": rec.get("duplicate_by", ""),
    }
    if not ack["duplicate"]:
        orchestration.record_intent(space, stored)
        orchestration.append_effect(space, {
            "effect_id": stored.get("effect_id", ""),
            "effect_type": "ingress_public_append",
            "requester": requester,
            "message_id": ack["message_id"],
            "event_seq": ack["event_seq"],
            **_context_fields(context),
        })
        _append_activity(space, {
            "상태": "posted", "시각": now_iso(), "actor": requester,
            "label": f"{requester} 메시지 도착",
            "detail": clean[:120], "message_id": ack["message_id"], "client_message_id": ack["client_message_id"],
            "event_seq": ack["event_seq"], "duplicate": False,
            **_context_fields(context),
        })
        if stored.get("run_manager_requested") is not False:
            _safe_obligation(
                space,
                "open",
                lambda: response_obligation.open_for_message(
                    space,
                    stored,
                    target_actor="space_manager",
                    reason="space_manager_required_for_user_input",
                ),
            )
    events = [{"type": "post", "speaker": requester, "content": clean, "ack": ack}]
    manager_recovery_needed = (
        ack["duplicate"]
        and bool(stored.get("run_manager_requested"))
        and not _manager_has_seen_event(space, ack.get("event_seq"))
    )
    recovery_event = (
        f"{requester} 메시지 중복 재시도에서 manager 미처리 감지"
        f"(event_seq={ack['event_seq']}, message_id={ack['message_id']}): {clean}"
        if manager_recovery_needed else ""
    )
    if manager_recovery_needed:
        events.append({"type": "manager_recovery_needed", "event": recovery_event})
    if run_manager and not ack["duplicate"]:
        event = (
            f"{requester}가 방에 메시지를 남김"
            f"(event_seq={ack['event_seq']}, message_id={ack['message_id']}, "
            f"intent_id={ack['intent_id']}, room_generation={ack['room_generation']}): {clean}"
        )
        events.extend(tick(space, event, context).get("events", []))
    elif run_manager and manager_recovery_needed:
        events.extend(tick(space, recovery_event, context).get("events", []))
    return {
        "ok": True,
        "ack": ack,
        "events": events,
        "manager_recovery_needed": manager_recovery_needed,
        "orchestration": context,
    }


def tick(space: str, event: str = "방 진행 필요", context: dict | None = None, *, auto_continue: bool = False) -> dict:
    sdir = SPACES / space
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    manager = sdir / MANAGER_DIRNAME
    if not manager.exists():
        raise ValueError(f"공간관리 자리 없음: {space}")
    return _run_tick_chain(space, event, context, auto_continue=auto_continue)


REFLOW_MAX_PER_CALL = 5   # 한 번 reflow에서 공개할 결과 상한(오래된 것부터, 폭주 방지)


def reflow(space: str) -> dict:
    """완료된 (비동기 디스패치) 작업 결과를 대화로 회수·공개한다 (설계_대화작업분리 Phase B).

    외부 폴러가 주기적으로 호출(`POST /reflow`). 비동기 디스패치는 tick을 막지 않는 대신, 작업이
    끝나도 결과를 방에 올릴 주체가 없다 — reflow가 그 주체다. 작업은 이미 plan-gated이므로
    pending release를 **세대일치 확인 후 오래된 것부터 한 건씩 자동 공개**한다(늦은/취소=세대 불일치는 건너뜀).
    """
    sdir = SPACES / space
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    events: list[dict] = []
    try:
        snap = release_queue.snapshot(space)
    except Exception as exc:
        return {"ok": False, "error": _public_error_summary(exc), "published": 0, "events": events}
    pending = snap.get("pending_items") or []
    # approve는 성공했는데 publish가 실패한(claim 경합·프로세스 킬) release는 pending_items에서
    # 빠져 종전 reflow가 영원히 재시도하지 않았다 — 승인됐지만 방에 못 뜬 결과가 approved(granted)
    # 상태로 영구 고아(감사 확정 경로). granted인데 미발행인 것을 재시도 대상에 합류시킨다.
    approved_unpublished = [
        item for item in (snap.get("approved_items") or [])
        if item.get("state") not in ("published", "rejected")
    ]
    current_gen = orchestration.current_generation(space)
    published = 0
    for item in sorted(pending, key=lambda r: _as_int(r.get("source_event_seq"))) + sorted(
        approved_unpublished, key=lambda r: _as_int(r.get("source_event_seq"))
    ):
        if published >= REFLOW_MAX_PER_CALL:
            break
        release_id = item.get("release_id") or item.get("release_queue_id")
        if not release_id:
            continue
        rg = item.get("room_generation")
        # 세대 펜스: 무효화된(늦은/취소된) 결과는 공개하지 않는다(공개와 메모리반영을 같은 게이트로).
        if rg is not None and _as_int(rg) != current_gen:
            events.append({"type": "reflow_stale_skipped", "release_id": release_id, "room_generation": rg})
            continue
        # 위험은 작업 *시작*(work_plan)에서 게이트된다 — 결과를 방(대표의 사적 공간)에 올리는 것 자체는
        # 본질적으로 저위험이다(내부 메시지). 결과 텍스트를 위험 스캔하면 보고서가 'law.md를 읽었다'고
        # 언급만 해도 오분류돼 거의 모든 결과가 막힌다(실증 2026-06-29). 그래서 결과는 그대로 자동 공개한다.
        try:
            approve_release(space, release_id, actor="공간관리", reason="reflow 자동 공개(작업계획 승인 완료분)")
            publish_release(space, release_id, actor="공간관리")
            published += 1
            events.append({"type": "reflow_published", "release_id": release_id})
        except Exception as exc:
            events.append({"type": "reflow_publish_failed", "release_id": release_id, "error": _public_error_summary(exc)})
    if published:
        _append_activity(space, {
            "상태": "reflow_published", "시각": now_iso(), "actor": "공간관리",
            "label": "작업 결과 회수·공개", "detail": f"{published}건", "room_generation": current_gen,
        })
    # 승인 후 미착수 plan 회수(상한 보류·run_work 사망) — 같은 백스톱 주기에 태워 자동 재기동한다.
    try:
        events.extend(redispatch_deferred_plans(space))
    except Exception:
        pass
    # detached 채팅 턴 백스톱 — 자식이 못 올린 공개 대기 후보 회수 + 죽은 자식의 디스패치 잔재 정리.
    try:
        chat_pub = publish_ready_chat_candidates(space)
        events.extend(chat_pub.get("events") or [])
        published += int(chat_pub.get("published") or 0)
    except Exception:
        pass
    try:
        events.extend(_cleanup_stale_chat_dispatch(space))
    except Exception:
        pass
    # 자기성장 백스톱 — 승격 대상 레슨을 자동으로 승격후보에 올린다(멱등 — already_exists 스킵).
    # 종전엔 이 스캔의 유일한 트리거가 대시보드 수동 버튼이라 promotion_candidates 파일이 전 공간
    # 통틀어 생성된 적이 없었다(성장루프 공회전 원인 2). 승인·적용은 설계대로 대표 게이트 유지.
    try:
        scan = lesson_ledger.generate_promotion_candidates(space, actor="시스템자동", limit=10)
        if int(scan.get("created_count") or 0) > 0:
            events.append({"type": "promotion_candidates_created", "count": scan.get("created_count")})
            _append_activity(space, {
                "상태": "promotion_candidates_created", "시각": now_iso(), "actor": "시스템",
                "label": "레슨 승격후보 자동 생성(대표 승인 대기)",
                "detail": f"{scan.get('created_count')}건 — 대시보드 학습 패널에서 승인/반려",
            })
    except Exception:
        pass
    try:
        lesson_ledger.expire_stale_growth_gaps(space)
    except Exception:
        pass
    return {"ok": True, "published": published, "pending_remaining": max(0, len(pending) - published), "events": events}


def reflow_safe(space: str) -> dict:
    """예외를 절대 밖으로 던지지 않는 reflow — 백그라운드 태스크 체인(post)에서 뒤 tick을 안 끊게."""
    try:
        return reflow(space)
    except Exception as exc:
        return {"ok": False, "published": 0, "error": _public_error_summary(exc), "events": []}


# 서버가 진행 중 tick을 끊고 재시작되면(또는 tick 프로세스가 죽으면) 남는 상태들.
# 이 상태로 멈춘 공간은 외부 트리거 없이는 재개되지 않으므로 부팅 시 복구한다.
RECOVERABLE_STATES = {"manager_queued", "manager_running", "manager_retrying", "agent_running"}


def recover_space(space: str) -> dict:
    """한 공간의 중단된 진행을 복구한다 — 죽은 프로세스 claim을 만료시키고 매니저를 재구동."""
    sdir = SPACES / space
    if not (sdir / MANAGER_DIRNAME).exists():
        return {"space": space, "recovered": False, "reason": "no_manager_seat"}
    state = str(_load_json(_state_path(space), {}).get("상태") or "")
    if state not in RECOVERABLE_STATES:
        return {"space": space, "recovered": False, "reason": f"state_ok:{state or 'idle'}"}
    # 살아있는 작업 보호: 최근 하트비트가 신선한 running 작업이 있으면 소유 프로세스가 죽지 않고
    # 일하는 중이다(긴 다중-윈도우 작업 등). 그 claim을 foreign으로 뺏으면(다른 boot가 복구 시도)
    # 작업은 끝나도 결과 발행이 stale claim으로 거부된다. → 신선 작업이 있으면 복구하지 않는다.
    try:
        snap = task_registry.snapshot(space)
        live = [a for a in (snap.get("active_items") or []) if not a.get("heartbeat_stale")]
    except Exception:
        live = []
    if live:
        return {
            "space": space, "recovered": False,
            "reason": "owner_alive_fresh_heartbeat",
            "live_tasks": [a.get("task_id", "") for a in live],
        }
    manager_claim.expire_foreign_boot_claim(space)
    res = tick(space, "부팅 복구: 중단된 진행 재개", None, auto_continue=True)
    return {"space": space, "recovered": True, "prior_state": state, "ok": res.get("ok")}


def recover_stalled_spaces() -> list[dict]:
    """모든 공간을 훑어 중단된 진행을 복구한다(부팅 시 1회). 한 공간 실패가 전체를 막지 않는다."""
    results = []
    if not SPACES.exists():
        return results
    for space_dir in sorted(SPACES.iterdir()):
        if not space_dir.is_dir():
            continue
        try:
            results.append(recover_space(space_dir.name))
        except Exception as exc:
            results.append({"space": space_dir.name, "recovered": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    return results


def reflow_all_spaces() -> list[dict]:
    """모든 공간의 완료·미게시 작업 결과를 방으로 회수·공개한다(설계_대화작업분리 Phase B 백스톱).

    reflow는 본래 (a)대표 메시지(spaces.py post) · (b)작업완료(run_work finally)에만 트리거된다.
    그런데 작업완료 시점의 reflow는 매니저 claim 경합('manager claim busy')이나 프로세스 하드킬로
    조용히 실패할 수 있고(실증 2026-06-29: win 이식 완료 후 ~11분 침묵, 대표가 직접 prod해야 공개됨),
    매니저 tick 경로엔 reflow가 없다. 그래서 그 두 트리거가 모두 빗나가면 완료 결과가 release_queue에
    pending으로 남아 '대표의 다음 발화'까지 방에 안 뜬다 — 목표(작업 결과가 대화로 돌아온다)의 직접 위반.

    이 함수는 외부 트리거 없이 주기적으로 호출되는 백스톱이다(대시보드 서버의 reflow-backstop 스레드).
    reflow_safe는 멱등(세대펜스 + published 제외 + already_committed)·예외안전이라, 빈 큐에서는 사실상
    no-op이고 claim 경합 시엔 다음 주기에 재시도되어 이중공개나 장애를 만들지 않는다.
    """
    results = []
    if not SPACES.exists():
        return results
    for space_dir in sorted(SPACES.iterdir()):
        if not space_dir.is_dir():
            continue
        if not (space_dir / MANAGER_DIRNAME).exists():
            continue
        try:
            res = reflow_safe(space_dir.name)
            if res.get("published"):
                results.append({"space": space_dir.name, "published": res.get("published")})
        except Exception:
            # reflow_safe는 본래 예외를 던지지 않지만, 한 공간 실패가 다른 공간 공개를 막지 않게 이중 방어.
            pass
    return results


def reap_stale_tasks_all_spaces() -> list[dict]:
    """모든 공간에서 heartbeat가 끊긴 비종결 작업을 강제 finalize하는 자동복구 reaper(주기 백스톱).

    엔진 타임아웃/워커 하드킬로 '완료했는데 finalize 안 돼 active에 박제된' 작업을 풀어준다
    (task_registry.reap_stale_tasks 참조). 박제가 풀리면 (1)산출물이 release→reflow로 공개·자동배포되고
    (2)사회자가 더는 '작업 중'으로 오인하지 않아 다음 작업으로 진행할 수 있다. 한 공간 실패가 다른 공간을
    막지 않게 공간별로 가둔다. 신선 heartbeat(살아 일하는) 작업은 reap_stale_tasks가 절대 건드리지 않는다."""
    results: list[dict] = []
    if not SPACES.exists():
        return results
    for space_dir in sorted(SPACES.iterdir()):
        if not space_dir.is_dir():
            continue
        if not (space_dir / MANAGER_DIRNAME).exists():
            continue
        try:
            reaped = task_registry.reap_stale_tasks(space_dir.name)
            if reaped:
                results.extend(reaped)
        except Exception:
            # reap_stale_tasks는 예외안전이지만, 한 공간 실패가 다른 공간 복구를 막지 않게 이중 방어.
            pass
        # 원장 압축 백스톱 — finalize 시점 압축을 놓친 방(서버 재시작·기존 원장 마이그레이션)을 회수.
        # compact 자체가 임계 미만이면 no-op·예외안전이라 30초 주기에 얹어도 무해하다.
        task_registry.compact_closed_task_events(space_dir.name)
    return results


def _tick_unlocked(space: str, event: str = "방 진행 필요", context: dict | None = None) -> dict:
    sdir = SPACES / space
    manager = sdir / MANAGER_DIRNAME

    members_result = _members_status(space)
    members = members_result.get("data") or []
    member_tokens = {
        str(m.get("토큰") or "").strip()
        for m in members
        if isinstance(m, dict) and str(m.get("토큰") or "").strip()
    }
    delivery = transcript_state(space)
    context = context or _latest_context(space, delivery.get("last_event_seq"))
    claim_result = manager_claim.acquire(space, event, delivery.get("last_event_seq"), context)
    claim = claim_result.get("claim") or {}
    if claim_result.get("corrupt"):
        _append_activity(space, {
            "상태": "manager_claim_corrupt", "시각": now_iso(), "actor": "공간관리",
            "label": "manager claim 파일 손상",
            "detail": "자동 실행을 중단하고 수동 복구가 필요함",
            **_context_fields(context),
        })
        _write_state(
            space, "idle", event=event, actor="공간관리",
            last_action="manager_claim_corrupt",
            label="manager claim 복구 필요",
            read_until_event_seq=delivery.get("last_event_seq"),
            **_context_fields(context),
        )
        return {"ok": False, "claim_corrupt": True, "events": [{
            "type": "manager_claim_corrupt",
            "claim_token": claim.get("claim_token", ""),
        }]}
    if not claim_result.get("acquired"):
        redrive_events = claim.get("redrive_events") or []
        coalesced_pending_inputs = _input_items_from_redrive_events(space, redrive_events)
        _append_activity(space, {
            "상태": "manager_claim_busy", "시각": now_iso(), "actor": "공간관리",
            "label": "공간관리 실행 중 · 재처리 예약",
            "detail": "이미 유효한 manager claim이 있어 새 tick은 redrive로 수렴",
            **_context_fields(context),
            **_claim_fields(claim),
        })
        _write_state(
            space, "manager_running", event=event, actor="공간관리",
            label="공간관리 실행 중 · 재처리 예약",
            read_until_event_seq=delivery.get("last_event_seq"),
            queue_event_type="manager_redrive_required",
            **_context_fields(context),
            **_coalesced_fields(context),
            **_claim_fields(claim),
        )
        return {"ok": True, "claim_busy": True, "events": [{
            "type": "manager_redrive_required",
            "claim_token": claim.get("claim_token", ""),
            "redrive_events": redrive_events,
            "coalesced_pending_inputs": coalesced_pending_inputs,
        }]}

    if orchestration.is_context_stale(space, context):
        _append_activity(space, {
            "상태": "manager_generation_stale", "시각": now_iso(), "actor": "공간관리",
            "label": "room_generation 변경으로 오래된 공간관리 실행 차단",
            "detail": "오래된 tick context이므로 진행 보고 steering과 턴 전달을 만들지 않음",
            **_context_fields(context),
            **_claim_fields(claim),
        })
        release, release_events = _release_redrive(space, claim, "stale_generation")
        if release.get("released") and not release.get("redrive_required"):
            _write_state(space, "idle", last_action="stale_generation",
                         label="오래된 공간관리 실행 차단",
                         reason="room_generation 변경",
                         **_context_fields(context), **_claim_fields(claim))
        _safe_record_interaction_evaluation(
            space,
            outcome="superseded",
            context=context,
            source_event="manager_generation_stale",
            actor="공간관리",
            target="space",
            what_worked=["room_generation fence blocked stale manager context before side effects"],
            what_failed=["manager tick arrived after generation changed"],
            lesson_candidate_needed=True,
            no_lesson_reason="generation_fence_worked_no_new_lesson_v0",
        )
        return {"ok": False, "generation_stale": True, "events": [{
            "type": "manager_generation_stale_result",
            "claim_token": claim.get("claim_token", ""),
            "redrive_required": bool(release.get("redrive_required")),
        }, *release_events]}

    preflight_events = _request_due_task_progress_reports(space, claim=claim, context=context)
    manager_context_pack = context_pack.build_context_pack(
        space, mode="manager", event=event, context=context, target_agent="space_manager"
    )
    manager_pack_manifest = context_pack.record_pack_delivery(
        space,
        recipient="space_manager",
        delivery_type="manager_tick",
        context_pack=manager_context_pack,
        manager_claim_context=claim,
    )
    base_prompt = _space_context(space, event, context, manager_context_pack)
    prompt = base_prompt
    raw = ""
    decision = {}
    attempts = []
    error = ""
    manager_failed = False
    manager_engine_failed = False
    engine_retry_used = False
    for attempt in range(1, MAX_DECISION_ATTEMPTS + 1):
        _write_state(
            space, "manager_running", event=event, actor="공간관리",
            label=f"공간관리 읽고 판단 중 ({attempt}/{MAX_DECISION_ATTEMPTS})",
            read_until_event_seq=delivery.get("last_event_seq"),
            context_pack_id=manager_context_pack.get("context_pack_id", ""),
            wake_pack_manifest_id=manager_pack_manifest.get("manifest_id", ""),
            **_context_fields(context),
            **_coalesced_fields(context),
            **_claim_fields(claim),
        )
        try:
            raw = engine.run_engine(manager, prompt, timeout=300)
        except Exception as exc:
            raw = _public_error_summary(exc)
            manager_failed = True
            manager_engine_failed = True
            decision = {}
            error = raw
        else:
            if _engine_failure_text(raw):
                manager_failed = True
                manager_engine_failed = True
                decision = {}
                error = _public_error_summary(raw)
            else:
                # 코드펜스(```json)는 흡수하되 JSON 밖 산문은 거부→재시도(계약 유지).
                # 펜스를 빈 dict로 처리해 '필수 필드 빠짐'→stop으로 매니저가 멈추던 버그 수정.
                decision = _normalize_decision(space, _extract_decision_json(raw), member_tokens)
                error = _decision_error(decision, member_tokens)
        attempts.append({"attempt": attempt, "raw": raw, "decision": decision, "error": error})
        if not manager_failed and not error:
            break
        if manager_failed:
            # 엔진 일시 실패(타임아웃/모델 오류)는 1회 재시도한다. 종전엔 첫 실패에서 즉시 포기해
            # 그 tick이 통째로 죽었다(실증 레빗_bcd7: 매니저 타임아웃 2연속 → 대표 응답 22분 지연).
            # JSON 형식 재시도(아래)와 별개로, 엔진 레벨 실패에도 최소 한 번의 기회를 준다.
            if attempt < MAX_DECISION_ATTEMPTS and not engine_retry_used:
                engine_retry_used = True
                manager_failed = False
                manager_engine_failed = False
                error = ""
                _write_state(
                    space, "manager_retrying", event=event, actor="공간관리",
                    label=f"엔진 실패 · 재시도 ({attempt + 1}/{MAX_DECISION_ATTEMPTS})",
                    reason=str(raw)[:200],
                    context_pack_id=manager_context_pack.get("context_pack_id", ""),
                    wake_pack_manifest_id=manager_pack_manifest.get("manifest_id", ""),
                    **_context_fields(context),
                    **_coalesced_fields(context),
                    **_claim_fields(claim),
                )
                prompt = base_prompt
                continue
            break
        if attempt < MAX_DECISION_ATTEMPTS:
            next_attempt = attempt + 1
            # 2단계(스마트): 유효 action은 골랐는데 '전용 필드'만 빠졌으면, 마지막 직전까지는
            # 선택지를 나열한 초점 프롬프트로 그 필드만 받는다(복잡 액션 준수율↑). 마지막 시도는
            # _retry_prompt의 단순화(pass/stop 유도)로 자가복구.
            action_choice = str(decision.get("action") or "").strip()
            phase2 = (_phase2_prompt(space, action_choice, decision, member_tokens)
                      if next_attempt < MAX_DECISION_ATTEMPTS else None)
            retry_label = (f"세부 보완 · 2단계 재요청 ({next_attempt}/{MAX_DECISION_ATTEMPTS})"
                           if phase2 else f"JSON 형식 오류 · 재요청 ({next_attempt}/{MAX_DECISION_ATTEMPTS})")
            _write_state(
                space, "manager_retrying", event=event, actor="공간관리",
                label=retry_label,
                reason=error,
                context_pack_id=manager_context_pack.get("context_pack_id", ""),
                wake_pack_manifest_id=manager_pack_manifest.get("manifest_id", ""),
                **_context_fields(context),
                **_coalesced_fields(context),
                **_claim_fields(claim),
            )
            prompt = phase2 or _retry_prompt(base_prompt, raw, error, next_attempt)

    if not manager_claim.is_current(space, claim):
        return {"ok": False, "stale": True, "events": [*preflight_events, {
            "type": "manager_stale_result",
            "claim_token": claim.get("claim_token", ""),
        }]}

    if orchestration.is_context_stale(space, context):
        _append_activity(space, {
            "상태": "manager_generation_stale", "시각": now_iso(), "actor": "공간관리",
            "label": "room_generation 변경으로 오래된 공간관리 결과 차단",
            "detail": "실행 중 취소/재계획 등으로 room_generation이 바뀌어 공개/턴 전달을 하지 않음",
            **_context_fields(context),
            **_claim_fields(claim),
        })
        release, release_events = _release_redrive(space, claim, "stale_generation")
        if release.get("released") and not release.get("redrive_required"):
            _write_state(space, "idle", last_action="stale_generation",
                         label="오래된 공간관리 결과 차단",
                         reason="room_generation 변경",
                         **_context_fields(context), **_claim_fields(claim))
        _safe_record_interaction_evaluation(
            space,
            outcome="superseded",
            context=context,
            source_event="manager_generation_stale",
            actor="공간관리",
            target="space",
            what_worked=["room_generation fence blocked stale manager result"],
            what_failed=["manager result arrived after generation changed"],
            lesson_candidate_needed=True,
            no_lesson_reason="generation_fence_worked_no_new_lesson_v0",
        )
        return {"ok": False, "generation_stale": True, "events": [*preflight_events, {
            "type": "manager_generation_stale_result",
            "claim_token": claim.get("claim_token", ""),
            "redrive_required": bool(release.get("redrive_required")),
        }, *release_events]}

    if error:
        manager_failed = True
        reason = (
            f"공간관리 엔진 실행 실패: {error}"
            if manager_engine_failed
            else f"공간관리 엔진이 {MAX_DECISION_ATTEMPTS}회 재시도 후에도 유효한 JSON 결정을 반환하지 않음: {error}"
        )
        decision = {
            "action": "stop",
            "wake": "",
            "message": "",
            "reason": reason,
        }
        _append_activity(space, {
            "상태": "manager_failed", "시각": now_iso(), "actor": "공간관리",
            "label": "공간관리 실패", "detail": _public_error_summary(raw) if _engine_failure_text(raw) else error,
            "recovery_action": "공간관리 엔진/모델 또는 JSON 출력 형식을 확인한 뒤 수동 진행",
            **_context_fields(context),
            **_claim_fields(claim),
        })
        _safe_record_interaction_evaluation(
            space,
            outcome="failed",
            context=context,
            source_event="manager_failed",
            actor="공간관리",
            target="space",
            what_failed=[reason],
            lesson_candidate_needed=True,
            no_lesson_reason="manager_engine_or_schema_failure_requires_manual_review_v0",
        )
    _append_activity(space, {
        "상태": "manager_decision", "시각": now_iso(), "actor": "공간관리",
        "label": "공간관리 결정", "action": decision.get("action", "stop"),
        "target": decision.get("wake", ""), "detail": decision.get("reason", ""),
        **_context_fields(context),
        **_claim_fields(claim),
    })
    log_data = {
        "시각": now_iso(), "event": event, "raw": raw, "decision": decision, "attempts": attempts,
        "manager_claim_token": claim.get("claim_token", ""),
        "manager_fencing_token": claim.get("fencing_token", ""),
        "context": context,
        "context_pack_id": manager_context_pack.get("context_pack_id", ""),
        "context_pack_checksum": manager_context_pack.get("context_pack_checksum", ""),
        "wake_pack_manifest_id": manager_pack_manifest.get("manifest_id", ""),
    }
    _append_jsonl(manager / "진행기록.jsonl", log_data)

    events = [*preflight_events, {
        "type": "manager_failed" if manager_failed else "manager_decision",
        "attempts": len(attempts),
        "action": decision.get("action", "stop"),
        "error": (_public_error_summary(raw) if manager_failed and _engine_failure_text(raw) else (error if manager_failed else "")),
    }]
    action = str(decision.get("action") or ("pass" if decision.get("wake") else "stop")).strip()
    wake = str(decision.get("wake") or "").strip()
    message = str(decision.get("message") or "").strip()
    if action == "cancel_task":
        task_ids = _task_ids_from_decision(decision)
        reason = message or str(decision.get("reason") or "").strip() or "공간관리 작업 취소 판단"
        cancelled = []
        errors = []
        for task_id in task_ids:
            try:
                result = request_task_cancel(space, task_id, actor="공간관리", reason=reason, control_context=context)
            except Exception as exc:
                err = _public_error_summary(exc)
                errors.append({"task_id": task_id, "error": err})
                _append_activity(space, {
                    "상태": "task_cancel_request_failed",
                    "시각": now_iso(),
                    "actor": "공간관리",
                    "target": task_id,
                    "label": "공간관리 작업 취소 실패",
                    "detail": err,
                    "task_id": task_id,
                    **_context_fields(context),
                    **_claim_fields(claim),
                })
                continue
            cancelled.append({
                "task_id": task_id,
                "duplicate": bool(result.get("duplicate")),
                "generation_advanced": bool(result.get("generation_advanced")),
                "cancellation_request_id": result.get("cancellation_request_id", ""),
            })
            events.append({
                "type": "task_cancel_requested",
                "task_id": task_id,
                "duplicate": bool(result.get("duplicate")),
                "generation_advanced": bool(result.get("generation_advanced")),
                "cancellation_request_id": result.get("cancellation_request_id", ""),
            })
        if errors:
            events.append({"type": "task_control_failed", "action": action, "errors": errors})
        state_context = {**context, "room_generation": orchestration.current_generation(space)}
        if cancelled:
            _safe_record_interaction_evaluation(
                space,
                outcome="success" if not errors else "partial",
                context=state_context,
                source_event="manager_cancel_task",
                actor="공간관리",
                target="task_registry",
                what_worked=["space manager selected explicit cancel_task action"],
                what_failed=[item["error"] for item in errors],
                lesson_candidate_needed=bool(errors),
                no_lesson_reason="manager_task_cancel_action_recorded",
            )
            _safe_obligation(
                space,
                "closed_by_cancel_task",
                lambda: response_obligation.close_for_context(
                    space,
                    state_context,
                    outcome="manager_closed",
                    actor="공간관리",
                    reason=f"cancel_task 처리 {len(cancelled)}건",
                ),
            )
        _write_state(
            space,
            "idle",
            last_action="cancel_task" if cancelled else "task_control_failed",
            label=f"작업 취소 요청 {len(cancelled)}건" if cancelled else "작업 취소 실패",
            reason=reason,
            task_ids=task_ids,
            task_control_errors=errors,
            read_until_event_seq=transcript_state(space).get("last_event_seq"),
            **_context_fields(state_context),
            **_claim_fields(claim),
        )
    elif action in {"revise_task", "request_progress"}:
        task_ids = _task_ids_from_decision(decision)
        instruction = _task_instruction_from_decision(decision)
        if action == "request_progress" and not instruction:
            instruction = str(decision.get("reason") or "").strip() or "현재 진행 상황과 막힌 점을 보고해줘."
        applied = []
        errors = []
        for task_id in task_ids:
            try:
                result = request_task_steering(
                    space,
                    task_id,
                    action=action,
                    instruction=instruction,
                    actor="공간관리",
                    control_context=context,
                )
            except Exception as exc:
                err = _public_error_summary(exc)
                errors.append({"task_id": task_id, "error": err})
                _append_activity(space, {
                    "상태": f"{action}_failed",
                    "시각": now_iso(),
                    "actor": "공간관리",
                    "target": task_id,
                    "label": "공간관리 작업 제어 실패",
                    "detail": err,
                    "task_id": task_id,
                    **_context_fields(context),
                    **_claim_fields(claim),
                })
                continue
            applied.append({
                "task_id": task_id,
                "duplicate": bool(result.get("duplicate")),
                "steering_seq": result.get("steering_seq", 0),
            })
            events.append({
                "type": action,
                "task_id": task_id,
                "duplicate": bool(result.get("duplicate")),
                "steering_seq": result.get("steering_seq", 0),
            })
        if errors:
            events.append({"type": "task_control_failed", "action": action, "errors": errors})
        _safe_record_interaction_evaluation(
            space,
            outcome="success" if applied and not errors else "partial" if applied else "failed",
            context=context,
            source_event=f"manager_{action}",
            actor="공간관리",
            target="task_registry",
            what_worked=[f"space manager selected explicit {action} action"] if applied else [],
            what_failed=[item["error"] for item in errors],
            lesson_candidate_needed=bool(errors),
            no_lesson_reason="manager_task_steering_action_recorded" if applied else "manager_task_steering_failed",
        )
        if applied:
            _safe_obligation(
                space,
                f"closed_by_{action}",
                lambda: response_obligation.close_for_context(
                    space,
                    context,
                    outcome="manager_closed",
                    actor="공간관리",
                    reason=f"{action} 처리 {len(applied)}건",
                ),
            )
        _write_state(
            space,
            "idle",
            last_action=action if applied else "task_control_failed",
            label=f"작업 제어 {len(applied)}건" if applied else "작업 제어 실패",
            reason=decision.get("reason", ""),
            task_ids=task_ids,
            task_control_errors=errors,
            read_until_event_seq=transcript_state(space).get("last_event_seq"),
            **_context_fields(context),
            **_claim_fields(claim),
        )
    elif action == "select_candidate":
        candidate_ids = _candidate_ids_from_decision(decision)
        candidate_id = candidate_ids[0] if candidate_ids else ""
        candidate = {}
        try:
            candidate = candidate_queue.get_candidate(space, candidate_id)
            content = str(candidate.get("structured_public_reply") or candidate.get("reply") or "").strip()
            publish_result = _publish_candidate_message(
                space,
                claim=claim,
                candidate=candidate,
                candidates=[candidate],
                content=content,
                mode="select",
                reason=decision.get("reason", ""),
            )
            selected = candidate_queue.mark_selected(
                space,
                candidate_id,
                actor="공간관리",
                reason=decision.get("reason", ""),
                publish_effect_id=publish_result["publish_effect_id"],
                published_message_id=publish_result["published_message_id"],
                event_seq=publish_result.get("event_seq"),
                manager_claim_context=claim,
            )
            orchestration.append_effect(space, {
                "effect_id": publish_result["publish_effect_id"],
                "effect_type": "candidate_selected_public_append",
                "candidate_id": candidate_id,
                "candidate_turn_id": candidate.get("turn_id", ""),
                "published_message_id": publish_result["published_message_id"],
                "publish_ledger_claim": publish_result.get("publish_ledger_claim", ""),
                **_context_fields(publish_result.get("context") or {}),
                **_claim_fields(claim),
            })
            _append_activity(space, {
                "상태": "candidate_selected",
                "시각": now_iso(),
                "actor": "공간관리",
                "target": candidate.get("target_agent", ""),
                "label": "병렬 후보 선택 공개",
                "detail": content[:160],
                "candidate_id": candidate_id,
                "published_message_id": publish_result["published_message_id"],
                **_context_fields(publish_result.get("context") or {}),
                **_claim_fields(claim),
            })
            _safe_obligation(
                space,
                "answered_by_candidate_select",
                lambda: response_obligation.close_for_context(
                    space,
                    publish_result.get("context") or {},
                    outcome="answered",
                    actor="공간관리",
                    reason=decision.get("reason", "") or "병렬 후보 선택 공개",
                    published_message_id=publish_result["published_message_id"],
                    responder=candidate.get("target_agent", ""),
                ),
            )
            _safe_record_interaction_evaluation(
                space,
                outcome="success",
                context=publish_result.get("context") or {},
                source_event="candidate_selected",
                actor="공간관리",
                target="candidate_queue",
                publish_effect_id=publish_result["publish_effect_id"],
                published_message_id=publish_result["published_message_id"],
                what_worked=["candidate was selected and published through publish ledger"],
                lesson_candidate_needed=False,
                no_lesson_reason="candidate_select_publish_success",
            )
            events.append({
                "type": "candidate_selected",
                "candidate_id": candidate_id,
                "published_message_id": publish_result["published_message_id"],
                "discarded_peer_count": len(selected.get("peer_events") or []),
            })
            _write_state(space, "idle", last_action="select_candidate", last_target=candidate.get("target_agent", ""),
                         label="병렬 후보 선택 공개", candidate_id=candidate_id,
                         published_message_id=publish_result["published_message_id"],
                         **_context_fields(publish_result.get("context") or {}), **_claim_fields(claim))
        except orchestration.OrchestrationStaleError as exc:
            err = _public_error_summary(exc)
            stale_ids = list(candidate_ids)
            try:
                stale_ids = candidate_queue.pending_ids_for_turn(space, candidate.get("turn_id", "")) or stale_ids
            except Exception:
                pass
            try:
                candidate_queue.supersede_candidates(space, stale_ids, actor="공간관리", reason=err, manager_claim_context=claim)
            except Exception:
                pass
            events.append({"type": "candidate_stale", "candidate_id": candidate_id, "candidate_ids": stale_ids, "error": err})
            _write_state(space, "idle", last_action="candidate_stale", reason=err,
                         label="오래된 후보 공개 차단", **_context_fields(context), **_claim_fields(claim))
        except Exception as exc:
            err = _public_error_summary(exc)
            events.append({"type": "candidate_select_failed", "candidate_id": candidate_id, "error": err})
            _append_activity(space, {
                "상태": "candidate_select_failed",
                "시각": now_iso(),
                "actor": "공간관리",
                "target": candidate_id,
                "label": "병렬 후보 선택 실패",
                "detail": err,
                "candidate_id": candidate_id,
                **_context_fields(context),
                **_claim_fields(claim),
            })
            _write_state(space, "idle", last_action="candidate_select_failed", reason=err,
                         label="병렬 후보 선택 실패", candidate_id=candidate_id,
                         **_context_fields(context), **_claim_fields(claim))
    elif action == "publish_each":
        # 캐주얼 단톡: 각 후보를 그 멤버 말풍선으로 '따로' 공개(다자 대화·사회자 침묵). 동료 폐기 안 함.
        candidate_ids = _candidate_ids_from_decision(decision)
        published_ids = []
        last_ctx = context
        for candidate_id in candidate_ids:
            try:
                candidate = candidate_queue.get_candidate(space, candidate_id)
                content = str(candidate.get("structured_public_reply") or candidate.get("reply") or "").strip()
                if not content:
                    events.append({"type": "candidate_publish_each_skipped", "candidate_id": candidate_id, "reason": "empty"})
                    continue
                publish_result = _publish_candidate_message(
                    space, claim=claim, candidate=candidate, candidates=[candidate],
                    content=content, mode="select", reason=decision.get("reason", ""),
                )
                candidate_queue.mark_selected(
                    space, candidate_id, actor="공간관리", reason=decision.get("reason", ""),
                    publish_effect_id=publish_result["publish_effect_id"],
                    published_message_id=publish_result["published_message_id"],
                    event_seq=publish_result.get("event_seq"),
                    discard_turn_peers=False,   # ← 핵심: 동료 후보를 폐기하지 않는다(모두 공개)
                    manager_claim_context=claim,
                )
                last_ctx = publish_result.get("context") or last_ctx
                published_ids.append(candidate_id)
                orchestration.append_effect(space, {
                    "effect_id": publish_result["publish_effect_id"],
                    "effect_type": "candidate_publish_each_append",
                    "candidate_id": candidate_id,
                    "published_message_id": publish_result["published_message_id"],
                    **_context_fields(publish_result.get("context") or {}),
                })
                events.append({
                    "type": "candidate_published_each",
                    "candidate_id": candidate_id,
                    "target_agent": candidate.get("target_agent", ""),
                    "published_message_id": publish_result["published_message_id"],
                })
            except orchestration.OrchestrationStaleError as exc:
                events.append({"type": "candidate_stale", "candidate_id": candidate_id, "error": _public_error_summary(exc)})
            except Exception as exc:
                events.append({"type": "candidate_publish_each_failed", "candidate_id": candidate_id, "error": _public_error_summary(exc)})
        _append_activity(space, {
            "상태": "candidate_published_each", "시각": now_iso(), "actor": "공간관리",
            "label": "단톡 다자 공개", "detail": f"{len(published_ids)}명 각자 말풍선 공개",
            **_context_fields(last_ctx), **_claim_fields(claim),
        })
        if published_ids:
            _safe_obligation(
                space, "answered_by_publish_each",
                lambda: response_obligation.close_for_context(
                    space, last_ctx or {}, outcome="answered", actor="공간관리",
                    reason=decision.get("reason", "") or "단톡 다자 공개", responder="단톡",
                ),
            )
        _write_state(space, "idle", last_action="publish_each",
                     label=f"단톡 다자 공개 {len(published_ids)}명",
                     **_context_fields(last_ctx), **_claim_fields(claim))
    elif action == "synthesize_candidates":
        candidate_ids = _candidate_ids_from_decision(decision)
        candidates = []
        try:
            candidates = [candidate_queue.get_candidate(space, candidate_id) for candidate_id in candidate_ids]
            publish_result = _publish_candidate_message(
                space,
                claim=claim,
                candidate=candidates[0] if candidates else None,
                candidates=candidates,
                content=message,
                mode="synthesize",
                reason=decision.get("reason", ""),
            )
            synthesized = candidate_queue.mark_synthesized(
                space,
                candidate_ids,
                actor="공간관리",
                reason=decision.get("reason", ""),
                public_summary=message,
                publish_effect_id=publish_result["publish_effect_id"],
                published_message_id=publish_result["published_message_id"],
                event_seq=publish_result.get("event_seq"),
                manager_claim_context=claim,
            )
            orchestration.append_effect(space, {
                "effect_id": publish_result["publish_effect_id"],
                "effect_type": "candidate_synthesis_public_append",
                "candidate_ids": candidate_ids,
                "synthesis_id": synthesized.get("synthesis_id", ""),
                "published_message_id": publish_result["published_message_id"],
                "publish_ledger_claim": publish_result.get("publish_ledger_claim", ""),
                **_context_fields(publish_result.get("context") or {}),
                **_claim_fields(claim),
            })
            _append_activity(space, {
                "상태": "candidates_synthesized",
                "시각": now_iso(),
                "actor": "공간관리",
                "target": ",".join(candidate_ids),
                "label": "병렬 후보 합성 공개",
                "detail": message[:160],
                "synthesis_id": synthesized.get("synthesis_id", ""),
                "published_message_id": publish_result["published_message_id"],
                **_context_fields(publish_result.get("context") or {}),
                **_claim_fields(claim),
            })
            _safe_obligation(
                space,
                "answered_by_candidate_synthesis",
                lambda: response_obligation.close_for_context(
                    space,
                    publish_result.get("context") or {},
                    outcome="answered",
                    actor="공간관리",
                    reason=decision.get("reason", "") or "병렬 후보 합성 공개",
                    published_message_id=publish_result["published_message_id"],
                    responder="공간관리",
                ),
            )
            _safe_record_interaction_evaluation(
                space,
                outcome="success",
                context=publish_result.get("context") or {},
                source_event="candidates_synthesized",
                actor="공간관리",
                target="candidate_queue",
                publish_effect_id=publish_result["publish_effect_id"],
                published_message_id=publish_result["published_message_id"],
                what_worked=["candidates were synthesized and published through publish ledger"],
                lesson_candidate_needed=False,
                no_lesson_reason="candidate_synthesis_publish_success",
            )
            events.append({
                "type": "candidates_synthesized",
                "candidate_ids": candidate_ids,
                "synthesis_id": synthesized.get("synthesis_id", ""),
                "published_message_id": publish_result["published_message_id"],
                "discarded_peer_count": len(synthesized.get("peer_events") or []),
            })
            _write_state(space, "idle", last_action="synthesize_candidates",
                         label="병렬 후보 합성 공개", reason=decision.get("reason", ""),
                         synthesis_id=synthesized.get("synthesis_id", ""),
                         published_message_id=publish_result["published_message_id"],
                         **_context_fields(publish_result.get("context") or {}), **_claim_fields(claim))
        except orchestration.OrchestrationStaleError as exc:
            err = _public_error_summary(exc)
            stale_ids = list(candidate_ids)
            try:
                for item in candidates:
                    for stale_id in candidate_queue.pending_ids_for_turn(space, item.get("turn_id", "")):
                        if stale_id not in stale_ids:
                            stale_ids.append(stale_id)
            except Exception:
                pass
            try:
                candidate_queue.supersede_candidates(space, stale_ids, actor="공간관리", reason=err, manager_claim_context=claim)
            except Exception:
                pass
            events.append({"type": "candidate_stale", "candidate_ids": stale_ids, "error": err})
            _write_state(space, "idle", last_action="candidate_stale", reason=err,
                         label="오래된 후보 합성 차단", **_context_fields(context), **_claim_fields(claim))
        except Exception as exc:
            err = _public_error_summary(exc)
            events.append({"type": "candidate_synthesis_failed", "candidate_ids": candidate_ids, "error": err})
            _append_activity(space, {
                "상태": "candidate_synthesis_failed",
                "시각": now_iso(),
                "actor": "공간관리",
                "target": ",".join(candidate_ids),
                "label": "병렬 후보 합성 실패",
                "detail": err,
                **_context_fields(context),
                **_claim_fields(claim),
            })
            _write_state(space, "idle", last_action="candidate_synthesis_failed", reason=err,
                         label="병렬 후보 합성 실패", **_context_fields(context), **_claim_fields(claim))
    elif action == "discard_candidate":
        candidate_ids = _candidate_ids_from_decision(decision)
        candidates = []
        try:
            candidates = [candidate_queue.get_candidate(space, candidate_id) for candidate_id in candidate_ids]
            stale_ids = []
            for candidate in candidates:
                if orchestration.is_context_stale(space, _candidate_context(candidate)):
                    for stale_id in candidate_queue.pending_ids_for_turn(space, candidate.get("turn_id", "")) or [candidate.get("candidate_id", "")]:
                        if stale_id and stale_id not in stale_ids:
                            stale_ids.append(stale_id)
            if stale_ids:
                candidate_queue.supersede_candidates(
                    space,
                    stale_ids,
                    actor="공간관리",
                    reason=_public_error_summary(orchestration.OrchestrationStaleError("OrchestrationStaleError: candidate stale generation")),
                    manager_claim_context=claim,
                )
                _append_activity(space, {
                    "상태": "candidate_stale",
                    "시각": now_iso(),
                    "actor": "공간관리",
                    "target": ",".join(stale_ids),
                    "label": "오래된 후보 폐기 요청을 세대 차단으로 정리",
                    "detail": decision.get("reason", ""),
                    **_context_fields(context),
                    **_claim_fields(claim),
                })
                events.append({"type": "candidate_stale", "candidate_ids": stale_ids, "error": "OrchestrationStaleError: candidate stale generation"})
                _write_state(space, "idle", last_action="candidate_stale", label="오래된 후보 폐기 차단",
                             reason=decision.get("reason", ""), **_context_fields(context), **_claim_fields(claim))
                discarded = None
            else:
                discarded = candidate_queue.discard_candidates(
                    space,
                    candidate_ids,
                    actor="공간관리",
                    reason=decision.get("reason", ""),
                    manager_claim_context=claim,
                )
            if discarded is None:
                pass
            else:
                _append_activity(space, {
                    "상태": "candidate_discarded",
                    "시각": now_iso(),
                    "actor": "공간관리",
                    "target": ",".join(candidate_ids),
                    "label": "병렬 후보 폐기",
                    "detail": decision.get("reason", ""),
                    **_context_fields(context),
                    **_claim_fields(claim),
                })
                events.append({"type": "candidate_discarded", "candidate_ids": candidate_ids, "count": len(discarded.get("events") or [])})
                # 폐기로 그 대표 입력의 후보가 하나도 안 남으면, parallel_pass 진입 때 assigned로 잡아둔
                # 응답의무를 open으로 되돌린다. 안 되돌리면 발행할 후보도 없고 sweep(open 전용)도 못 잡아
                # 대표 입력이 영구 무응답이 된다(감사 확정 경로). 이미 답이 나간 의무는 terminal이라 no-op.
                for candidate in candidates:
                    cand_context = _candidate_context(candidate)
                    turn = str((candidate or {}).get("turn_id") or "")
                    try:
                        remaining = candidate_queue.pending_ids_for_turn(space, turn) if turn else []
                    except Exception:
                        remaining = []
                    if not remaining:
                        _safe_obligation(
                            space,
                            "reopen_after_discard_all_candidates",
                            lambda ctx=cand_context: response_obligation.reopen_for_context(
                                space, ctx, actor="공간관리",
                                reason="후보 전량 폐기 — 대표 입력 응답의무 재개",
                            ),
                        )
                _write_state(space, "idle", last_action="discard_candidate", label="병렬 후보 폐기",
                             reason=decision.get("reason", ""), **_context_fields(context), **_claim_fields(claim))
        except Exception as exc:
            err = _public_error_summary(exc)
            events.append({"type": "candidate_discard_failed", "candidate_ids": candidate_ids, "error": err})
            _write_state(space, "idle", last_action="candidate_discard_failed", reason=err,
                         label="병렬 후보 폐기 실패", **_context_fields(context), **_claim_fields(claim))
    elif action == "update_guide":
        # 자기성장(방지침): 대표 durable 피드백을 공간지침에 실제로 누적 기록(거짓 기록 금지).
        rule = message or str(decision.get("reason") or "").strip()
        res = _append_guide_rule(space, rule, source="대표 피드백")
        _append_activity(space, {
            "상태": "guide_updated" if res.get("appended") else "guide_update_noop",
            "시각": now_iso(), "actor": "공간관리", "target": "공간지침",
            "label": "방지침 학습 규칙 기록" if res.get("appended") else "방지침 기록 생략(중복/빈값)",
            "detail": str(rule)[:160],
            **_context_fields(context), **_claim_fields(claim),
        })
        events.append({"type": "guide_updated", "appended": bool(res.get("appended")), "duplicate": bool(res.get("duplicate"))})
        _publish_manager_note(space, f"📌 이 방 규칙에 반영했어요: {str(rule)[:120]}", context, claim)
        _safe_obligation(space, "answered_by_update_guide", lambda: response_obligation.close_for_context(
            space, context or {}, outcome="answered", actor="공간관리",
            reason="방지침에 규칙 기록", responder="공간관리"))
        _write_state(space, "idle", last_action="update_guide", label="방지침 기록",
                     read_until_event_seq=transcript_state(space).get("last_event_seq"),
                     **_context_fields(context), **_claim_fields(claim))
    elif action == "update_summary":
        # 롤링 누적요약: 요약.md를 갱신된 전문으로 대체(방에 공개 안 함·응답의무 안 닫음 — 내부 누적기록).
        # 갱신 후 auto-continue로 본 턴(대표 발언 등)을 이어서 처리한다.
        summary = message or str(decision.get("reason") or "").strip()
        res = _write_rolling_summary(space, summary)
        _append_activity(space, {
            "상태": "summary_updated" if res.get("written") else "summary_update_noop",
            "시각": now_iso(), "actor": "공간관리", "target": "요약",
            "label": "롤링 누적요약 갱신" if res.get("written") else "요약 갱신 생략(빈값)",
            "detail": str(summary)[:160],
            **_context_fields(context), **_claim_fields(claim),
        })
        events.append({"type": "summary_updated", "written": bool(res.get("written")), "char_count": res.get("char_count", 0)})
        _write_state(space, "idle", last_action="update_summary", label="롤링 누적요약 갱신",
                     read_until_event_seq=transcript_state(space).get("last_event_seq"),
                     **_context_fields(context), **_claim_fields(claim))
    elif action == "launch_app":
        # 인가된 앱 실행: 샌드박스 에이전트가 못 하는 외부효과(앱 실행)를 시스템이 대행한다.
        # 등록 앱(앱 탭)만 — run_app은 매니페스트의 run만 그 target에서 실행(임의 명령 차단)하고 pidfile을
        # 기록해 대시보드 앱 탭에 '● 실행 중'으로 반영한다.
        app_ref = str(decision.get("app") or message or "").strip()
        launch_ok = False
        info = _resolve_app(app_ref)
        if not info:
            note = f"⚠️ '{app_ref}' 앱을 앱 탭(레지스트리)에서 못 찾았어요. 등록된 앱 이름인지 확인이 필요해요."
        else:
            try:
                from . import apps as core_apps
                res = core_apps.run_app(info["dir"])
                launch_ok = bool(res.get("running") or res.get("ok"))
                if res.get("already"):
                    _insts = res.get("instances") or []
                    if res.get("detected_external") and _insts:
                        # 대시보드 밖에서 이미 켜져 있던 인스턴스 감지 → 중복 실행 안 함, PID들 보고(에이전트 타깃팅용).
                        _pid_lines = "; ".join(
                            f"PID {i.get('pid')}" + (f"({str(i.get('title'))[:36]})" if i.get('title') else "")
                            for i in _insts)
                        note = (f"📌 '{info['name']}'은 이미 실행 중이라 또 띄우지 않았어요 — 실행 중 인스턴스 "
                                f"{res.get('instance_count', len(_insts))}개: {_pid_lines}. 특정 인스턴스로 작업하려면 그 PID를 대상으로 지시하세요.")
                    else:
                        note = f"📌 '{info['name']}'은 이미 실행 중이에요 (앱 탭 '● 실행 중', pid {res.get('pid')})."
                else:
                    note = f"📌 '{info['name']}'을 실행했어요 — 앱 탭에 '● 실행 중'으로 떠요(대시보드 반영). pid {res.get('pid')}."
            except Exception as exc:
                note = f"⚠️ '{(info or {}).get('name', app_ref)}' 실행에 실패했어요: {_public_error_summary(exc)}"
        _append_activity(space, {
            "상태": "app_launched" if launch_ok else "app_launch_failed",
            "시각": now_iso(), "actor": "공간관리", "target": (info or {}).get("dir", app_ref),
            "label": "앱 실행(대시보드 반영)" if launch_ok else "앱 실행 실패",
            "detail": str(note)[:160], **_context_fields(context), **_claim_fields(claim),
        })
        events.append({"type": "app_launched", "ok": launch_ok, "app": app_ref, "dir": (info or {}).get("dir", "")})
        _publish_manager_note(space, note, context, claim)
        # 앱 실행은 '요청의 전부'가 아니라 대개 '작업의 전제'다(예: "revit을 실행해서 거기서 작업하려고
        # 팀을 구성하자"). 그래서 launch_app을 여기서 응답의무 answered로 '종결'하지 않는다 — 종결하면
        # 복합요청(실행+팀구성+방지침+조작)의 나머지가 방치돼 방이 idle+answered로 스트랜드된다(어떤
        # 백스톱도 idle 방은 안 잡는다). 대신 성공 시 _should_auto_continue가 본 턴을 한 번 더 이어
        # 매니저가 남은 요청을 처리(pass로 담당자 위임/방지침 update_guide 등)하게 한다. 단순 실행이면
        # 이어진 턴에서 매니저가 stop을 택해 그 경로가 의무를 manager_closed로 닫는다(회귀안전).
        _write_state(space, "idle", last_action="launch_app", label="앱 실행" if launch_ok else "앱 실행 실패",
                     read_until_event_seq=transcript_state(space).get("last_event_seq"),
                     **_context_fields(context), **_claim_fields(claim))
    elif action == "propose_knowledge":
        # 자기성장(지식): 대표가 알려준 사실/기준을 (1) 방 지식메모에 누적(감사) + (2) 전역 지식 자원으로
        # 졸업시켜 발견기가 '찾아서 참고'하게 한다. 기존 유사 지식이 있으면(LLM 의미판정) 거기 사실을 누적.
        claim_text = message or str(decision.get("reason") or "").strip()
        kname = str(decision.get("knowledge") or "").strip()
        kdesc = str(decision.get("description") or "").strip()
        _append_space_knowledge(space, claim_text, source="대표 피드백")   # 방 감사 기록(유지)
        try:
            similar = knowledge_ledger.find_similar_knowledge([kname, kdesc, claim_text], top=5)
            dup_candidates = [s for s in similar if s.get("name") and s.get("name") != kname
                              and float(s.get("signal") or 0) >= SKILL_DEDUP_CANDIDATE_MIN_SIGNAL][:3]
            dup_name = _semantic_resource_duplicate(space, "지식", kname, kdesc, claim_text, dup_candidates)
            target_name = dup_name or kname
            res = knowledge_ledger.create_knowledge(target_name, description=(kdesc or claim_text), claim=claim_text)
            gate = knowledge_ledger.check_knowledge_discoverable(target_name, [q for q in (target_name, kdesc, claim_text) if q])
            _append_activity(space, {
                "상태": "knowledge_graduated", "시각": now_iso(), "actor": "공간관리", "target": target_name,
                "label": ("기존 지식에 사실 추가" if dup_name else "전역 지식 자원 생성") + f" / discoverable={gate.get('discoverable')}",
                "detail": str(claim_text)[:160], **_context_fields(context), **_claim_fields(claim),
            })
            events.append({"type": "knowledge_graduated", "knowledge": target_name,
                           "created": bool(res.get("created")), "redirected": bool(dup_name),
                           "discoverable": bool(gate.get("discoverable"))})
            _verb = "기존 지식에 더했어요" if dup_name else "전역 지식으로 등록했어요"
            _publish_manager_note(space, f"📌 '{target_name}' 지식에 {_verb}: {str(claim_text)[:100]}", context, claim)
        except Exception as exc:
            events.append({"type": "knowledge_graduate_failed", "knowledge": kname, "error": str(exc)[:200]})
            _append_activity(space, {
                "상태": "knowledge_graduate_failed", "시각": now_iso(), "actor": "공간관리", "target": kname,
                "label": "전역 지식 졸업 실패", "detail": str(exc)[:160],
                **_context_fields(context), **_claim_fields(claim),
            })
        _safe_obligation(space, "answered_by_propose_knowledge", lambda: response_obligation.close_for_context(
            space, context or {}, outcome="answered", actor="공간관리",
            reason="지식 기록 + 전역 졸업", responder="공간관리"))
        _write_state(space, "idle", last_action="propose_knowledge", label="지식 기록(전역 졸업)",
                     read_until_event_seq=transcript_state(space).get("last_event_seq"),
                     **_context_fields(context), **_claim_fields(claim))
    elif action == "propose_case":
        # P-wire-B: 공간관리 판단으로 스킬 케이스를 '발의'(candidate만). 대표 즉시승인/promote는 P-wire-C.
        # 자원락(스킬 폴더)은 propose_case 내부에서만 잡으므로 여기서 공간락을 보유하지 않는다(데드락 가드).
        skill = str(decision.get("skill") or "").strip()
        cand = dict(decision.get("candidate") or {})
        # supersede인데 대상 case_id가 없으면 교훈을 통째로 버리지 말고 add_case로 강등한다.
        # (매니저는 기존 case_id 목록을 받지 못해 대상을 못 채우는 경우가 잦다 — 학습 보존 > supersede 의미.
        #  중복·모순은 add_case 경로의 §9.1 의미 dedup/자동격리가 처리한다.)
        _sup = cand.get("supersedes")
        if str(cand.get("action") or "").strip() == "supersede" and not (_sup if isinstance(_sup, list) else str(_sup or "").strip()):
            cand["action"] = "add_case"
            cand.pop("supersedes", None)
        try:
            record = case_ledger.propose_case(skill, cand, proposed_by="공간관리", from_daepyo=False)
            _append_activity(space, {
                "상태": "case_proposed", "시각": now_iso(), "actor": "공간관리",
                "target": skill, "label": "스킬 케이스 발의(candidate)",
                "detail": f"{record.get('case_id', '')}: {str(cand.get('condition', ''))[:60]}",
                **_context_fields(context), **_claim_fields(claim),
            })
            events.append({"type": "case_proposed", "skill": skill,
                           "case_id": record.get("case_id", ""), "status": record.get("status", "")})
            _publish_manager_note(space, f"📌 '{skill}' 스킬에 케이스로 반영했어요: {str(cand.get('condition',''))[:100]}", context, claim)
            # 케이스만 쌓지 말고 본문도 고도화: doer에게 skill-creator 기준 본문 개선을 위임(대표 '다듬어줘'=계속 고도화).
            _dispatch_skill_authoring(space, skill_name=skill,
                                      desc=f"기존 '{skill}' 스킬의 발견용 설명 유지·개선",
                                      cond=str(cand.get("condition", "")), instr=str(cand.get("instruction", "")),
                                      is_new=False, context=context, claim=claim)
            # 갭1 수정: 대표 피드백으로 케이스를 발의했으면 응답 의무를 닫아 대표 입장에서 '처리됨'이 보이게(가드됨).
            _safe_obligation(space, "closed_by_propose_case", lambda: response_obligation.close_for_context(
                space, {**context, "room_generation": orchestration.current_generation(space)},
                outcome="manager_closed", actor="공간관리",
                reason=f"스킬 케이스 발의로 처리: {record.get('case_id', '')}"))
            _write_state(space, "idle", last_action="propose_case",
                         label=f"스킬 케이스 발의: {skill}",
                         reason=decision.get("reason", ""), **_context_fields(context), **_claim_fields(claim))
        except Exception as exc:
            err = str(exc)[:200]
            events.append({"type": "case_propose_failed", "skill": skill, "error": err})
            _append_activity(space, {
                "상태": "case_propose_failed", "시각": now_iso(), "actor": "공간관리",
                "target": skill, "label": "스킬 케이스 발의 실패", "detail": err,
                **_context_fields(context), **_claim_fields(claim),
            })
            _write_state(space, "idle", last_action="case_propose_failed", reason=err,
                         label="스킬 케이스 발의 실패", **_context_fields(context), **_claim_fields(claim))
    elif action == "propose_skill":
        # 마땅한 스킬이 없을 때 새 스킬을 만들고, durable 교훈을 본문(상시 규칙)과 첫 케이스 양쪽에 담는다.
        # find_similar는 신호일 뿐(차단 아님 — 에이전트가 이미 발견으로 '없음'을 판단해 여기 옴). 유사 후보는 감사용으로 이벤트에 남긴다.
        # check_discoverable로 '찾아지는 스킬'인지 확인(대표 요구: 안 찾아지면 안 만든 것과 같다).
        name = str(decision.get("skill") or "").strip()
        desc = str(decision.get("description") or "").strip()
        cand = decision.get("candidate") or {}
        cond = str(cand.get("condition") or "").strip()
        instr = str(cand.get("instruction") or "").strip()
        try:
            similar = skill_smith.find_similar_skills([name, desc, cond, instr], top=5)
            # 신규 남발 방지: 어휘로 후보만 추리고(게이트), 의미는 LLM이 판정해 기존과 같으면 거기 케이스로 업데이트.
            dup_candidates = [s for s in similar
                              if s.get("name") and s.get("name") != name
                              and float(s.get("signal") or 0) >= SKILL_DEDUP_CANDIDATE_MIN_SIGNAL][:3]
            dup_name = _semantic_resource_duplicate(space, "스킬", name, desc, f"{cond} → {instr}", dup_candidates)
            if dup_name:
                seed = {**cand, "action": "add_case", "routing_kind": str(cand.get("routing_kind") or "procedural")}
                record = case_ledger.propose_case(dup_name, seed, proposed_by="공간관리", from_daepyo=False)
                _append_activity(space, {
                    "상태": "skill_create_redirected", "시각": now_iso(), "actor": "공간관리",
                    "target": dup_name, "label": "신규 대신 기존 스킬 케이스로(의미 중복 방지)",
                    "detail": f"요청={name} → 기존={dup_name}(LLM 의미판정) / case {record.get('case_id', '')}",
                    **_context_fields(context), **_claim_fields(claim),
                })
                events.append({"type": "skill_create_redirected", "requested_skill": name,
                               "existing_skill": dup_name, "case_id": record.get("case_id", ""),
                               "candidates": [c.get("name") for c in dup_candidates]})
                _publish_manager_note(space, f"📌 '{dup_name}' 스킬에 반영했어요(이미 있는 스킬을 업데이트): {str(cond)[:90]}", context, claim)
                _dispatch_skill_authoring(space, skill_name=dup_name, desc=desc, cond=cond, instr=instr,
                                          is_new=False, context=context, claim=claim)
                _safe_obligation(space, "closed_by_propose_skill_redirect", lambda: response_obligation.close_for_context(
                    space, {**context, "room_generation": orchestration.current_generation(space)},
                    outcome="manager_closed", actor="공간관리",
                    reason=f"기존 스킬 업데이트로 처리(의미 중복): {dup_name}"))
                _write_state(space, "idle", last_action="propose_skill_redirected",
                             label=f"기존 스킬 케이스 추가: {dup_name}",
                             reason=decision.get("reason", ""), **_context_fields(context), **_claim_fields(claim))
            else:
                body = (
                    f"# {name}\n\n"
                    f"## 언제 쓰나\n- {desc}\n\n"
                    f"## 핵심 규칙 (대표 지시로 신설)\n- {cond} → {instr}\n\n"
                    "## 절차\n1. 위 핵심 규칙을 지금 상황에 맞게 적용한다. 누적되는 경우의 수는 케이스(cases.jsonl)로 쌓인다.\n"
                )
                created = skill_smith.create_skill(name, description=desc, body=body, grade="추가")
                seed = {**cand, "action": "add_case", "routing_kind": str(cand.get("routing_kind") or "procedural")}
                record = case_ledger.propose_case(name, seed, proposed_by="공간관리", from_daepyo=False)
                gate = skill_smith.check_discoverable(name, [q for q in (name, desc, cond) if q], top=3)
                _append_activity(space, {
                    "상태": "skill_created", "시각": now_iso(), "actor": "공간관리",
                    "target": name, "label": "새 스킬 생성 + 첫 케이스",
                    "detail": f"{created.get('skill_id', '')} / case {record.get('case_id', '')} / discoverable={gate.get('discoverable')}",
                    **_context_fields(context), **_claim_fields(claim),
                })
                events.append({"type": "skill_created", "skill": name,
                               "skill_id": created.get("skill_id", ""),
                               "case_id": record.get("case_id", ""),
                               "discoverable": bool(gate.get("discoverable")),
                               "similar": [s.get("name") for s in similar]})
                _publish_manager_note(space, f"📌 새 스킬 '{name}'을 만들어 반영했어요: {str(cond)[:90]}", context, claim)
                _dispatch_skill_authoring(space, skill_name=name, desc=desc, cond=cond, instr=instr,
                                          is_new=True, context=context, claim=claim)
                _safe_obligation(space, "closed_by_propose_skill", lambda: response_obligation.close_for_context(
                    space, {**context, "room_generation": orchestration.current_generation(space)},
                    outcome="manager_closed", actor="공간관리",
                    reason=f"새 스킬 생성으로 처리: {name}"))
                _write_state(space, "idle", last_action="propose_skill",
                             label=f"새 스킬 생성: {name}",
                             reason=decision.get("reason", ""), **_context_fields(context), **_claim_fields(claim))
        except Exception as exc:
            err = str(exc)[:200]
            events.append({"type": "skill_create_failed", "skill": name, "error": err})
            _append_activity(space, {
                "상태": "skill_create_failed", "시각": now_iso(), "actor": "공간관리",
                "target": name, "label": "새 스킬 생성 실패", "detail": err,
                **_context_fields(context), **_claim_fields(claim),
            })
            _write_state(space, "idle", last_action="skill_create_failed", reason=err,
                         label="새 스킬 생성 실패", **_context_fields(context), **_claim_fields(claim))
    elif action == "parallel_pass":
        specs = _parallel_target_specs(decision)
        join_policy = str(decision.get("join_policy") or "timeout_then_partial").strip()
        presentation_mode = str(decision.get("presentation_mode") or "silent_reference").strip()
        turn_id = orchestration.effect_id(
            "parallel_turn",
            space,
            context.get("intent_id", ""),
            context.get("source_event_seq"),
            claim.get("claim_token", ""),
            json.dumps(specs, ensure_ascii=False, sort_keys=True),
        )
        _write_state(
            space,
            "agent_running",
            current="parallel_pass",
            target=",".join(spec["wake"] for spec in specs),
            reason=decision.get("reason", ""),
            label=f"병렬 후보 수집 중 · {len(specs)}명",
            turn_id=turn_id,
            join_policy=join_policy,
            presentation_mode=presentation_mode,
            **_context_fields(context),
            **_claim_fields(claim),
        )
        _append_activity(space, {
            "상태": "parallel_candidate_running",
            "시각": now_iso(),
            "actor": "공간관리",
            "target": ",".join(spec["wake"] for spec in specs),
            "label": "병렬 후보 수집 시작",
            "detail": decision.get("reason", ""),
            "turn_id": turn_id,
            "join_policy": join_policy,
            "presentation_mode": presentation_mode,
            **_context_fields(context),
            **_claim_fields(claim),
        })
        _safe_obligation(
            space,
            "assigned_to_parallel_pass",
            lambda: response_obligation.assign_for_context(
                space,
                context,
                assignee="parallel_pass:" + ",".join(spec["wake"] for spec in specs),
                actor="공간관리",
                reason=decision.get("reason", ""),
            ),
        )
        candidate_results = []
        candidate_errors = []
        cancel_events: dict[object, threading.Event] = {}
        executor = ThreadPoolExecutor(max_workers=len(specs))
        future_map = {}

        def record_candidate_exception(spec: dict, exc: BaseException, *, event_type: str = "parallel_candidate_failed"):
            target = spec["wake"]
            err = _public_error_summary(exc)
            if event_type == "parallel_candidate_timeout" and "TimeoutExpired" not in err:
                err = "TimeoutExpired: parallel candidate join timeout"
            candidate_errors.append({"person": target, "error": err})
            if isinstance(exc, StaleManagerClaim) or _is_stale_publish_error(exc):
                _append_activity(space, {
                    "상태": "parallel_candidate_stale",
                    "시각": now_iso(),
                    "actor": target,
                    "target": "CandidateQueue",
                    "label": "오래된 병렬 후보 차단",
                    "detail": err,
                    "turn_id": turn_id,
                    **_context_fields(context),
                    **_claim_fields(claim),
                })
                _safe_record_interaction_evaluation(
                    space,
                    outcome="superseded",
                    context=context,
                    source_event="parallel_candidate_stale",
                    actor=target,
                    target="space",
                    what_worked=["room_generation or manager claim fence blocked stale parallel candidate"],
                    what_failed=[err],
                    lesson_candidate_needed=True,
                    no_lesson_reason="parallel_candidate_stale_fence_worked_no_new_lesson_v0",
                )
            else:
                try:
                    candidate_queue.record_candidate_error(
                        space,
                        turn_id=turn_id,
                        target_agent=target,
                        manager_message=spec.get("message", ""),
                        error=err,
                        context=context,
                        manager_claim_context=claim,
                        reason=spec.get("reason") or decision.get("reason", ""),
                        join_policy=join_policy,
                        presentation_mode=presentation_mode,
                    )
                except Exception:
                    pass
                _append_activity(space, {
                    "상태": "parallel_candidate_timeout" if event_type == "parallel_candidate_timeout" else "parallel_candidate_failed",
                    "시각": now_iso(),
                    "actor": target,
                    "target": "CandidateQueue",
                    "label": "병렬 후보 시간 초과" if event_type == "parallel_candidate_timeout" else "병렬 후보 실패",
                    "detail": err,
                    "turn_id": turn_id,
                    "join_policy": join_policy,
                    **_context_fields(context),
                    **_claim_fields(claim),
                })
                _safe_record_interaction_evaluation(
                    space,
                    outcome="failed",
                    context=context,
                    source_event=event_type,
                    actor=target,
                    target="space",
                    what_failed=[err],
                    lesson_candidate_needed=True,
                    no_lesson_reason="parallel_candidate_timeout_cancelled_v0" if event_type == "parallel_candidate_timeout" else "parallel_candidate_failure_requires_manual_review_v0",
                )
            events.append({"type": event_type, "person": target, "error": err, "turn_id": turn_id})

        try:
            for spec in specs:
                cancel_event = threading.Event()
                future = executor.submit(
                    _run_agent_candidate,
                    space,
                    spec["wake"],
                    spec["message"],
                    claim,
                    context,
                    turn_id=turn_id,
                    join_policy=join_policy,
                    presentation_mode=presentation_mode,
                    reason=spec.get("reason") or decision.get("reason", ""),
                    cancel_event=cancel_event,
                )
                future_map[future] = spec
                cancel_events[future] = cancel_event

            all_futures = set(future_map)
            timed_out_futures = set()
            timeout_candidate_futures = set()
            join_timeout = _parallel_join_timeout(len(specs))
            if join_policy == "timeout_then_partial":
                done_futures, pending_futures = wait(
                    all_futures,
                    timeout=join_timeout,
                )
                if pending_futures:
                    for future in pending_futures:
                        cancel_events[future].set()
                        future.cancel()
                    _append_activity(space, {
                        "상태": "parallel_candidate_partial_timeout",
                        "시각": now_iso(),
                        "actor": "공간관리",
                        "target": ",".join(future_map[future]["wake"] for future in pending_futures),
                        "label": "병렬 후보 일부 시간 초과",
                        "detail": f"{len(done_futures)} 완료 · {len(pending_futures)} 취소 요청",
                        "turn_id": turn_id,
                        "join_policy": join_policy,
                        "join_timeout_sec": join_timeout,
                        **_context_fields(context),
                        **_claim_fields(claim),
                    })
                    wait(pending_futures, timeout=PARALLEL_CANDIDATE_CANCEL_DRAIN_SECONDS)
                timeout_candidate_futures = set(pending_futures)
                process_futures = done_futures | {future for future in pending_futures if future.done()}
                timed_out_futures = {future for future in pending_futures if not future.done()}
            else:
                process_futures = all_futures

            for future in as_completed(process_futures):
                spec = future_map[future]
                target = spec["wake"]
                try:
                    item = future.result()
                except Exception as exc:
                    event_type = "parallel_candidate_timeout" if future in timeout_candidate_futures or "parallel candidate join timeout" in str(exc) else "parallel_candidate_failed"
                    record_candidate_exception(spec, exc, event_type=event_type)
                else:
                    candidate_results.append(item)
                    events.append({
                        "type": "parallel_candidate",
                        "person": target,
                        "candidate_id": (item.get("candidate") or {}).get("candidate_id", ""),
                        "turn_id": turn_id,
                        "context_pack_id": item.get("context_pack_id", ""),
                        "wake_id": item.get("wake_id", ""),
                    })

            for future in timed_out_futures:
                spec = future_map[future]
                record_candidate_exception(
                    spec,
                    TimeoutError("TimeoutExpired: parallel candidate join timeout"),
                    event_type="parallel_candidate_timeout",
                )
        finally:
            executor.shutdown(wait=join_policy == "wait_all", cancel_futures=True)
        if not candidate_results:
            # 후보 전원 실패/타임아웃 — 이 턴은 assigned 로 잡아둔 응답의무를 되돌려야 한다.
            # 안 되돌리면 pending 후보 0 이라 candidate drain 도, open 전용 sweep 도 못 잡아
            # 대표 입력이 영구 무응답으로 스트랜드된다(감사 확정 경로).
            _safe_obligation(
                space,
                "reopen_after_all_candidates_failed",
                lambda: response_obligation.reopen_for_context(
                    space,
                    context,
                    actor="공간관리",
                    reason=f"parallel_pass 후보 {len(specs)}명 전원 실패/타임아웃 — 의무 재개",
                ),
            )
        orchestration.append_effect(space, {
            "effect_id": orchestration.effect_id(
                "parallel_pass",
                space,
                turn_id,
                context.get("intent_id", ""),
                context.get("source_event_seq"),
            ),
            "effect_type": "parallel_pass_candidates_collected",
            "turn_id": turn_id,
            "target_count": len(specs),
            "candidate_count": len(candidate_results),
            "error_count": len(candidate_errors),
            "join_policy": join_policy,
            "presentation_mode": presentation_mode,
            **_context_fields(context),
            **_claim_fields(claim),
        })
        _safe_record_interaction_evaluation(
            space,
            outcome="success" if candidate_results else "failed",
            context=context,
            source_event="parallel_pass",
            actor="공간관리",
            target="candidate_queue",
            what_worked=["parallel candidate replies were stored without direct publish"] if candidate_results else [],
            what_failed=[item["error"] for item in candidate_errors][:5] if candidate_errors else [],
            lesson_candidate_needed=not bool(candidate_results),
            no_lesson_reason="parallel_candidate_collection_v0" if candidate_results else "parallel_candidate_collection_failed_v0",
        )
        _write_state(
            space,
            "idle",
            last_action="parallel_pass",
            last_target=",".join(spec["wake"] for spec in specs),
            label=f"병렬 후보 {len(candidate_results)}개 저장",
            reason=decision.get("reason", ""),
            turn_id=turn_id,
            candidate_count=len(candidate_results),
            candidate_error_count=len(candidate_errors),
            **_context_fields(context),
            **_claim_fields(claim),
        )
    elif action == "pass" and wake and wake in member_tokens and message:
        agent_context_pack = {}
        turn_handoff_pack = {}
        agent_pack_manifest = {}
        try:
            agent_context_pack = context_pack.build_context_pack(
                space, mode="chat", event=message, context=context, target_agent=wake
            )
            turn_handoff_pack = context_pack.build_turn_handoff_pack(
                space,
                target_agent=wake,
                manager_message=message,
                reason=decision.get("reason", ""),
                context=context,
                manager_claim_context=claim,
                context_pack=agent_context_pack,
            )
            agent_pack_manifest = context_pack.record_pack_delivery(
                space,
                recipient=wake,
                delivery_type="agent_wake",
                context_pack=agent_context_pack,
                turn_handoff_pack=turn_handoff_pack,
                manager_claim_context=claim,
            )
            _safe_obligation(
                space,
                "assigned_to_agent",
                lambda: response_obligation.assign_for_context(
                    space,
                    context,
                    assignee=wake,
                    actor="공간관리",
                    reason=decision.get("reason", ""),
                    wake_id=turn_handoff_pack.get("wake_id", ""),
                    turn_handoff_id=turn_handoff_pack.get("turn_handoff_id", ""),
                ),
            )
            _write_state(space, "agent_running", current=wake, target=wake, reason=decision.get("reason", ""),
                         label=f"{wake} 턴 받음 · 생각 중",
                         context_pack_id=agent_context_pack.get("context_pack_id", ""),
                         wake_id=turn_handoff_pack.get("wake_id", ""),
                         turn_handoff_id=turn_handoff_pack.get("turn_handoff_id", ""),
                         wake_pack_manifest_id=agent_pack_manifest.get("manifest_id", ""),
                         **_context_fields(context), **_claim_fields(claim))
            dispatched_async = _dispatch_chat_turn(space, wake=wake, message=message, context=context,
                                                   reason=decision.get("reason", ""))
            if dispatched_async:
                # 턴은 백그라운드에서 돈다 — 이 tick은 여기서 끝나 claim이 곧 풀리고, 그동안 도착하는
                # 대표 메시지는 claim_busy 없이 정상 처리된다(티키타카 비차단). 응답 공개·대화 연속은
                # 자식이 publish_ready_chat_candidates + tick으로 잇는다.
                events.append({
                    "type": "chat_turn_dispatched",
                    "person": wake,
                    "message": message,
                    "context_pack_id": agent_context_pack.get("context_pack_id", ""),
                    "wake_id": turn_handoff_pack.get("wake_id", ""),
                    "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
                })
            else:
                reply = _run_agent_turn(
                    space,
                    wake,
                    message,
                    claim,
                    context,
                    handoff_context_pack=agent_context_pack,
                    turn_handoff_pack=turn_handoff_pack,
                    reason=decision.get("reason", ""),
                )
                events.append({
                    "type": "wake",
                    "person": wake,
                    "message": message,
                    "reply": reply,
                    "context_pack_id": agent_context_pack.get("context_pack_id", ""),
                    "wake_id": turn_handoff_pack.get("wake_id", ""),
                    "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
                })
        except Exception as exc:
            if isinstance(exc, StaleManagerClaim) or _is_stale_publish_error(exc):
                _release, release_events = _release_redrive(space, claim, "stale_agent_reply")
                if _release.get("released") and not _release.get("redrive_required"):
                    _write_state(space, "idle", last_action="stale_agent_reply", last_target=wake,
                                 label="오래된 에이전트 응답 차단", reason=_public_error_summary(exc),
                                 context_pack_id=agent_context_pack.get("context_pack_id", ""),
                                 wake_id=turn_handoff_pack.get("wake_id", ""),
                                 turn_handoff_id=turn_handoff_pack.get("turn_handoff_id", ""),
                                 **_context_fields(context), **_claim_fields(claim))
                events.append({
                    "type": "manager_stale_result",
                    "person": wake,
                    "error": _public_error_summary(exc),
                    "context_pack_id": agent_context_pack.get("context_pack_id", ""),
                    "wake_id": turn_handoff_pack.get("wake_id", ""),
                })
                events.extend(release_events)
                _safe_record_interaction_evaluation(
                    space,
                    outcome="superseded",
                    context=context,
                    source_event="stale_agent_reply",
                    actor=wake,
                    target="space",
                    what_worked=["room_generation or manager claim fence blocked stale agent reply"],
                    what_failed=[_public_error_summary(exc)],
                    lesson_candidate_needed=True,
                    no_lesson_reason="stale_agent_reply_fence_worked_no_new_lesson_v0",
                )
                return {"ok": False, "stale": True, "events": events}
            if _is_lesson_application_hold(exc):
                err = _public_error_summary(exc)
                _append_activity(space, {
                    "상태": "lesson_application_missing", "시각": now_iso(), "actor": "공간관리",
                    "target": wake, "label": "레슨 적용 보고 누락으로 공개 보류", "detail": err,
                    "recovery_action": "에이전트 응답 마지막에 LessonApplicationReport JSON을 포함해 다시 진행",
                    "context_pack_id": agent_context_pack.get("context_pack_id", ""),
                    "wake_id": turn_handoff_pack.get("wake_id", ""),
                    "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
                    **_context_fields(context),
                    **_claim_fields(claim),
                })
                events.append({
                    "type": "lesson_application_missing",
                    "person": wake,
                    "message": message,
                    "error": err,
                    "context_pack_id": agent_context_pack.get("context_pack_id", ""),
                    "wake_id": turn_handoff_pack.get("wake_id", ""),
                })
                _safe_record_interaction_evaluation(
                    space,
                    outcome="rejected",
                    context=context,
                    source_event="lesson_application_missing",
                    actor=wake,
                    target="space",
                    what_failed=[err],
                    lesson_candidate_needed=True,
                    no_lesson_reason="must_apply_lesson_missing_application_report",
                )
                _release, release_events = _release_redrive(space, claim, "lesson_application_missing")
                events.extend(release_events)
                if _release.get("released") and not _release.get("redrive_required"):
                    _write_state(space, "idle", last_action="lesson_application_missing", last_target=wake,
                                 label="레슨 적용 보고 누락으로 공개 보류", reason=err,
                                 context_pack_id=agent_context_pack.get("context_pack_id", ""),
                                 wake_id=turn_handoff_pack.get("wake_id", ""),
                                 turn_handoff_id=turn_handoff_pack.get("turn_handoff_id", ""),
                                 **_context_fields(context), **_claim_fields(claim))
                return {"ok": False, "lesson_application_missing": True, "events": events}
            err = _public_error_summary(exc)
            _append_activity(space, {
                "상태": "wake_failed", "시각": now_iso(), "actor": "공간관리",
                "target": wake, "label": "턴 전달 실패", "detail": err,
                "recovery_action": "엔진/모델/멤버 상태를 확인한 뒤 수동 진행",
                "context_pack_id": agent_context_pack.get("context_pack_id", ""),
                "wake_id": turn_handoff_pack.get("wake_id", ""),
                "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
                **_context_fields(context),
                **_claim_fields(claim),
            })
            events.append({"type": "wake_failed", "person": wake, "message": message, "error": err})
            # 지목 에이전트 턴이 실패했다 = 아직 대표에게 답이 안 나갔다. pass 실행 전에 'assigned'로 전이해
            # 둔 응답의무를 다시 'open'으로 되돌려, 미응답 sweep(_open_user_obligations는 open만 재구동)이
            # 재구동해 자가치유하게 한다. 안 그러면 의무가 'assigned'로 고아가 돼 대표 메시지가 영영 무응답·
            # 무재시도로 스트랜드된다(manager_failure가 의무를 open으로 유지하는 것과 대칭). 종결된 의무는 no-op.
            _safe_obligation(
                space,
                "reopen_after_wake_failed",
                lambda: response_obligation.reopen_for_context(
                    space, context, actor="공간관리", reason=f"wake_failed 재구동: {err[:200]}",
                ),
            )
            # 타임아웃류 wake 실패는 자기성장 루프로 케이스화한다(중복기동 방지 교훈 → 스킬 승격 후보).
            # 그 외 실패(모델/멤버/스키마 등)는 종전대로 수동검토 보류로 둔다 — 회귀 최소화.
            _err_l = err.lower()
            _is_timeout_failure = ("타임아웃" in err) or ("timeout" in _err_l) or ("시간 초과" in err)
            if _is_timeout_failure:
                _safe_record_interaction_evaluation(
                    space,
                    outcome="failed",
                    context=context,
                    source_event="wake_failed",
                    actor="공간관리",
                    target=wake,
                    what_failed=[err],
                    lesson_candidate_needed=True,
                    # instruction은 안정 문자열 → lesson_id가 내용기반으로 dedup되어 폭증하지 않는다.
                    lesson_candidate={
                        "kind": "lesson",
                        "scope": "space",
                        "promotion_target": "skill",
                        "instruction": (
                            "엔진 타임아웃·턴 전달 실패(wake_failed)가 나면 같은 목표로 새 작업을 또 띄우지 말고, "
                            "① 같은 worker에 살아있는 기존 작업이 있으면 그 체크포인트(결과.md)를 확인해 재개하고 "
                            "② 죽었으면 같은 작업폴더에서 미통과 단계부터 재실행한다(통과 단계 재생성 금지). "
                            "한 worker는 동시에 하나의 작업만 — 중복 기동을 금지한다(large-task-checkpointing)."
                        ),
                        "applies_when": {
                            "keywords": ["타임아웃", "timeout", "wake_failed", "중복 기동", "재개", "체크포인트"],
                        },
                        "evidence_type": "agent_observation",
                        "source_quote": err[:240],
                    },
                )
            else:
                _safe_record_interaction_evaluation(
                    space,
                    outcome="failed",
                    context=context,
                    source_event="wake_failed",
                    actor="공간관리",
                    target=wake,
                    what_failed=[err],
                    lesson_candidate_needed=True,
                    no_lesson_reason="agent_wake_failure_requires_manual_review_v0",
                )
            _write_state(space, "idle", last_action="wake_failed", last_target=wake,
                         label="턴 전달 실패", reason=err,
                         context_pack_id=agent_context_pack.get("context_pack_id", ""),
                         wake_id=turn_handoff_pack.get("wake_id", ""),
                         turn_handoff_id=turn_handoff_pack.get("turn_handoff_id", ""),
                         **_context_fields(context), **_claim_fields(claim))
        else:
            if dispatched_async:
                # detached 경로 — reply는 아직 없다(자식이 생성 중). 상태는 agent_running을 유지하고
                # 공개·idle 전환은 publish_ready_chat_candidates가 한다. (여기서 reply를 참조하면
                # UnboundLocalError로 tick이 죽고 claim이 고아가 된다 — e2e 실증 후 수정.)
                pass
            else:
                _append_activity(space, {
                    "상태": "agent_replied", "시각": now_iso(), "actor": wake,
                    "label": f"{wake} 응답 기록", "detail": str(reply)[:120],
                    "context_pack_id": agent_context_pack.get("context_pack_id", ""),
                    "wake_id": turn_handoff_pack.get("wake_id", ""),
                    "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
                    **_context_fields(context),
                    **_claim_fields(claim),
                })
                _write_state(space, "idle", last_action="pass", last_target=wake, label=f"{wake} 응답 완료",
                             read_until_event_seq=transcript_state(space).get("last_event_seq"),
                             context_pack_id=agent_context_pack.get("context_pack_id", ""),
                             wake_id=turn_handoff_pack.get("wake_id", ""),
                             turn_handoff_id=turn_handoff_pack.get("turn_handoff_id", ""),
                             **_context_fields(context), **_claim_fields(claim))
    elif wake:
        events.append({"type": "wake_skipped", "person": wake, "reason": "멤버 토큰이 아니거나 메시지가 비었음"})
        _write_state(space, "idle", last_action="wake_skipped", last_target=wake,
                     label="턴 전달 실패", reason="멤버 토큰이 아니거나 메시지가 비었음",
                     **_context_fields(context), **_claim_fields(claim))
    else:
        if manager_failed:
            _write_state(space, "idle", last_action="manager_failed", reason=decision.get("reason", ""),
                         label="공간관리 실패", read_until_event_seq=delivery.get("last_event_seq"),
                         **_context_fields(context), **_claim_fields(claim))
        else:
            _safe_record_interaction_evaluation(
                space,
                outcome="success",
                context=context,
                source_event="manager_stop",
                actor="공간관리",
                target="space",
                what_worked=["space manager stopped the turn without unnecessary wake"],
                lesson_candidate_needed=False,
                no_lesson_reason="no_failure_or_correction",
            )
            _safe_obligation(
                space,
                "closed_by_manager_stop",
                lambda: response_obligation.close_for_context(
                    space,
                    context,
                    outcome="manager_closed",
                    actor="공간관리",
                    reason=decision.get("reason", "") or "공간관리가 턴을 멈춤",
                ),
            )
            _write_state(space, "idle", last_action="stop", reason=decision.get("reason", ""), label="턴 멈춤",
                         read_until_event_seq=delivery.get("last_event_seq"),
                         **_context_fields(context), **_claim_fields(claim))

    outcome = "manager_failed" if manager_failed else f"action_{action or 'stop'}"
    _release, release_events = _release_redrive(space, claim, outcome)
    events.extend(release_events)

    return {"ok": True, "decision": decision, "events": events}
