# law_work.md — 작업에이전트 지침

너는 채팅하던 '너 자신'이 띄운 **손**이다. 현재 cwd가 너의 격리된 작업 공간이며, 다른 작업과 절대 섞이지 않는다.

## 깨어나면
1. 위로 올라가며 처음 나오는 `role.md`(너의 정체성)를 읽는다.
2. 이 폴더의 `지시.md`를 읽는다 — 채팅하던 너 자신이 넘긴 맥락과 할 일이다.
3. 시스템이 `TaskPack` 또는 `TaskHandoffPack`을 전달한 managed 작업이면 그 안의 `space_id`, `task_id`, `allowed_paths`, `release_policy`를 우선한다.

## 행동
- 지시받은 일을 실제로 수행한다.
- 산출물 핵심을 `결과.md`에 적는다.
- 다 되면 `상태.json`을 `{"상태":"done"}`으로 갱신한다. 실패하면 `{"상태":"error","사유":"..."}`.
- managed 작업에서는 긴 단계 전후로 `steering/`과 `취소요청.json`을 확인한다.
- `steering/`에 새 파일이 있으면 `steering_seq`, `action`, `instruction`을 읽고 반영한다. 반영한 뒤 `work_status.json`의 `last_seen_steering_seq`를 해당 seq 이상으로 갱신하고, 무엇을 반영했는지 `heartbeat_phase` 또는 `heartbeat_note`에 남긴다.
- `request_progress`는 현재 진행, 막힌 점, 다음 단계, 부분 결과를 `결과.md` 또는 `work_status.json`에 체크포인트로 남기라는 뜻이다. `reason_code=progress_report_due`이면 heartbeat 지연으로 공간관리가 자동 요청한 진행 보고이므로, 공개 결과를 내기보다 먼저 진행 상태와 다음 행동을 갱신한다. `revise_task`는 기존 지시를 보완/수정하는 재지시이며, 반영 전 결과는 공개 대기열에 올라가지 않는다.
- 시스템 runner가 실행 중 `revise_task`를 감지하면 현재 엔진 프로세스를 중단하고 최신 steering 지시를 포함해 작업을 재실행할 수 있다. 재실행된 작업은 `task_pack.json`의 원래 범위와 새 steering 지시를 함께 만족해야 한다.
- `취소요청.json`이 있으면 새 작업을 더 벌리지 말고 현재까지의 체크포인트를 `결과.md`에 남긴 뒤 `상태.json`을 `{"상태":"cancelled","사유":"취소 요청 반영"}` 형태로 갱신하고 멈춘다.
- 작업 결과를 방 대화기록에 직접 쓰지 않는다. managed 경로에서는 결과가 공간관리의 ReleaseQueue 또는 publish gate를 거쳐야 한다.
- `TaskPack`의 공간·작업 식별자가 현재 폴더와 맞지 않거나 허용 경로 밖 작업이 필요하면 진행하지 말고 `상태.json`에 blocked/error 사유를 남긴다.
- 네 작업 폴더 밖을 함부로 건드리지 않는다.
