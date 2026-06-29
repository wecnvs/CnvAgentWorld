---
name: "skill-creator"
description: "Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a skill from scratch, edit, or optimize an existing skill, run evals to test a skill, benchmark skill performance with variance analysis, or optimize a skill's description for better triggering accuracy."
version: "1.0.0"
status: "stable"
---
# Skill Creator

A skill for creating new skills and iteratively improving them.

At a high level, the process of creating a skill goes like this:

- Decide what you want the skill to do and roughly how it should do it
- Write a draft of the skill
- Create a few test prompts and run claude-with-access-to-the-skill on them
- Help the user evaluate the results both qualitatively and quantitatively
  - While the runs happen in the background, draft some quantitative evals if there aren't any (if there are some, you can either use as is or modify if you feel something needs to change about them). Then explain them to the user (or if they already existed, explain the ones that already exist)
  - Use the `eval-viewer/generate_review.py` script to show the user the results for them to look at, and also let them look at the quantitative metrics
- Rewrite the skill based on feedback from the user's evaluation of the results (and also if there are any glaring flaws that become apparent from the quantitative benchmarks)
- Repeat until you're satisfied
- Expand the test set and try again at larger scale

Your job when using this skill is to figure out where the user is in this process and then jump in and help them progress through these stages. So for instance, maybe they're like "I want to make a skill for X". You can help narrow down what they mean, write a draft, write the test cases, figure out how they want to evaluate, run all the prompts, and repeat.

On the other hand, maybe they already have a draft of the skill. In this case you can go straight to the eval/iterate part of the loop.

Of course, you should always be flexible and if the user is like "I don't need to run a bunch of evaluations, just vibe with me", you can do that instead.

Then after the skill is done (but again, the order is flexible), you can also run the skill description improver, which we have a whole separate script for, to optimize the triggering of the skill.

Cool? Cool.

## Communicating with the user

The skill creator is liable to be used by people across a wide range of familiarity with coding jargon. If you haven't heard (and how could you, it's only very recently that it started), there's a trend now where the power of Claude is inspiring plumbers to open up their terminals, parents and grandparents to google "how to install npm". On the other hand, the bulk of users are probably fairly computer-literate.

So please pay attention to context cues to understand how to phrase your communication! In the default case, just to give you some idea:

- "evaluation" and "benchmark" are borderline, but OK
- for "JSON" and "assertion" you want to see serious cues from the user that they know what those things are before using them without explaining them

It's OK to briefly explain terms if you're in doubt, and feel free to clarify terms with a short definition if you're unsure if the user will get it.

---

## Skill Output Directory (이 워크스페이스 기준)

**모든 새 스킬은 `루트폴더/스킬/` 아래 4개 등급 폴더 중 하나에 만든다.** 등급은 아래 Post-Creation Step 0에서 판정한다.

| 등급 | 경로 | 의미 | Git |
|------|------|------|-----|
| 기본 | `스킬/기본/<skill-name>/` | 워크스페이스 구동에 반드시 필요한 핵심 스킬 | 추적(공개) |
| 추가 | `스킬/추가/<skill-name>/` | 일반 확장 스킬 | 로컬 전용(제외) |
| 고급 | `스킬/고급/<skill-name>/` | 고급·특수 용도 | 로컬 전용(제외) |
| 대외비 | `스킬/대외비/<skill-name>/` | 민감·비공개(자격증명·내부 전용 등) | 로컬 전용(제외) |

스킬 폴더 구조:

```
스킬/<등급>/<skill-name>/
├── SKILL.md        (필수)
├── scripts/        (고정 실행 코드)
├── references/     (참고용 코드/문서)
└── assets/         (산출물용 파일/템플릿)
```

다른 위치(`.agents/skills/` 등)에 만들지 않는다. 항상 `스킬/<등급>/`을 쓴다.

---

## Post-Creation: 등급 판정 & 등록 (이 워크스페이스 기준)

### Step 0: 등급 판정 (기본 / 추가 / 고급 / 대외비)

스킬을 어느 등급 폴더에 둘지 정한다:

- **기본** — 이 워크스페이스를 구동하는 데 *반드시 필요한* 핵심 스킬. **공개 깃 저장소에 올라가는 유일한 등급.**
- **추가** — 일반적인 확장. (로컬 전용, 깃 제외)
- **고급** — 고급·특수 용도. (로컬 전용, 깃 제외)
- **대외비** — 외부 공유 부적절. `.gitignore`로 Git에서 제외된다.

> **🔒 대외비 판정 (보안 — law.md §7 보안 불변식):** 스킬 본문·예시·description에 **개인정보(이름·연락처·계정)·특정 회사/고객/기관 이름·내부 전용 자료·자격증명**이 들어가면 **반드시 `대외비/`** 다. 특정 회사명이 박힌 스킬(예: 특정 기관용 산출물 제작)은 대외비다. 일반화할 수 있으면 회사/개인 식별정보를 빼고 `추가/`로, 못 빼면 `대외비/`로. **민감정보가 `기본/`에 들어가면 공개 유출**이므로, 애매하면 더 안전한(상위) 등급으로 둔다.

판정이 애매하면 사용자(대표)에게 묻는다.

### Step 1: 배치 & 자기충족성 확인

- SKILL.md가 단독으로 실행 가능한가? (아래 "저성능 모델 친화 표준" 참조)
- `scripts/` · `references/` · `assets/` 분리 원칙을 지켰는가?
- 실행형 스킬이면 상단에 🚨 호출 규칙 박스가 있는가?

### Step 2: 발견 최적화 description (필수 — 태그·등록 없음)

이 워크스페이스는 태그·수동 인덱스가 없다. 스킬은 **frontmatter `description`만으로 발견기에 잡힌다**(law.md §6·§7). 그러니 description이 곧 발견 품질이다.

1. **description을 또렷이** — *무엇을 하는지 + 언제 쓰는지 + 사용자가 그걸 말할 법한 표현(예: "…해줘", "…정리해줘") + 핵심 용어*를 담는다. (아래 "Description Optimization" 절의 방법을 그대로 적용.)
2. **등록 불필요** — 발견기가 매번 frontmatter를 라이브 스캔하므로 별도 인덱스에 손으로 등록하지 않는다(드리프트 없음).
3. **확인** — `python3 도구/기본/발견기/발견.py "<이 스킬을 부를 법한 표현>" --type skill` 로 이 스킬이 후보에 뜨는지 검증.
4. **대외비 스킬**은 폴더가 Git에서 빠지므로(`스킬/대외비/`) 클론에 따라 실제 폴더가 없을 수 있다 → 참조 측은 "없구나" 하고 차선(law.md §6).

---

## 도구 우선 (Tool-first) — 범용 기능은 반드시 `도구/`로 (이 워크스페이스 필수)

이 워크스페이스에서 **스킬은 `도구/`의 도구들을 조합해서 만든다.** 도구가 부품, 스킬이 조립품이다. 스킬을 만들거나 고칠 때 다음을 반드시 지킨다.

### 규칙
1. **먼저 `도구/`를 뒤진다.** 필요한 기능(곱셈·파일읽기·HTTP 호출 등)이 이미 도구로 있으면 새로 짜지 말고 그 도구를 참조·조합한다.
2. **범용 기능은 스킬 안에 인라인으로 박지 말고 `도구/`에 도구로 만든다.** 판단 기준은 단 하나 — **"여러 스킬·작업이 또 쓸 만한가?"** Yes면 그 코드는 도구다. 스킬의 `scripts/`엔 *그 스킬에만 특수한* 로직만 남긴다.
   - **범용·재사용 → `도구/<등급>/<tool-name>/`**
   - 이 스킬 전용 → 스킬의 `scripts/`
3. **도구를 만들 땐 발견 잘 되는 `description`을 쓴다** — 도구는 frontmatter `description`(+`entry`·`runtime`)으로 발견기에 잡힌다. 태그·등록 없음. 도구 만들기 전체 절차는 `스킬/기본/tool-creator`를 따른다.

### 왜 (저성능 모델도 이해하도록)
도구를 공용 1차 레이어로 두면 능력이 **누적**된다 — 다음 스킬은 바퀴를 다시 발명하지 않고 **발견기로 기존 도구를 찾아 조립**한다. 그래서 도구의 `description`을 또렷이 쓰는 게 핵심이다("X 하는 도구"를 사용자 표현으로 찾을 수 있게).

### 도구 생성 절차 (요약)
1. 기능이 범용인지 판단 → 범용이면 도구로 만든다.
2. 등급 폴더 선택: `도구/{기본|추가|고급|대외비}/<tool-name>/`.
3. `도구/<등급>/<tool-name>/`에 `도구.md`(name·description·entry·runtime) + 코드 작성.
4. 발견기로 잘 잡히는지 확인하고, 그 도구를 스킬에서 호출한다.

### 도구·자산을 스킬 본문에서 참조하는 법 (law.md §8)
스킬이 도구·자산에 기대는 부분은 특정 문서를 콕 집기보다 **무엇이 필요한지 자연어로** 적는다 — 사용 시점에 발견기로 찾게:
- `관련 도구: 두 수를 곱하기 — 발견기로 찾아 사용`  /  `관련 자산: OpenAI API 키 — 발견기로 찾아 사용`
- 마땅한 게 없으면 차선/판단(law.md §6). 특정 자원을 확정하려면 경로로 직접 참조(`도구/기본/<이름>/`).

---

## Model-Agnostic SKILL.md Writing

SKILL.md files must work identically across all AI engines (Claude, Gemini, Gemma, etc.):

- Use **bash** and **python** for executable steps — these are universally available
- Provide both **file direct access** and **REST API** methods when the skill interacts with dashboard data
- Do **not** use engine-specific syntax (e.g., Claude's `<antThinking>` tags, Gemini's `@` tool references)
- Keep instructions in plain imperative markdown that any LLM can follow

---

## 저성능 모델 친화 표준 (필수, 모든 신규/개정 스킬 적용)

대표님의 명시 지시: **"모든 스킬은 Gemini Flash급 저성능 모델도 SKILL.md만 보고 즉시 실행할 수 있도록 만들 것."**
이 절의 6원칙은 새 스킬을 만들 때나 기존 스킬을 고칠 때 빠짐없이 적용한다. 이 절을 따르지 않은 스킬은 미완성으로 본다.

### 1) 자연어 입력은 에이전트가 받고, 스킬은 단일·단순 스키마만 노출

스킬을 호출하는 사용자가 정형 JSON을 직접 만들 필요가 없게 한다. SKILL.md 안에 **"자연어 → 스킬 입력 스키마" 변환 가이드**를 풍부한 예시 페어와 함께 적는다. 변환은 호출하는 에이전트가 수행한다. 스킬 자체의 입력 스키마는 단 하나의 단순한 형태로만 정의한다 (입력 형식이 여러 개면 저성능 모델이 어느 쪽을 쓸지 헷갈린다).

```markdown
## 자연어 → 입력 변환 (에이전트가 수행)

| 사용자가 한 말 | 스킬에 넣을 입력 |
|----|----|
| "북쪽으로 15m, 동쪽으로 20m 사각 대지" | `[[0,0],[20000,0],[20000,15000],[0,15000]]` |
| "20×30 직사각형" | `[[0,0],[20000,0],[20000,30000],[0,30000]]` |
| ... | ... |

**기본값**:
- 단위 미명시 → mm 가정
- 시작점 미명시 → (0,0)
- 폐합 미명시 → 자동 폐합
```

### 2) 구체화 — "적당히/필요시" 금지

저성능 모델은 모호함에 약하다. SKILL.md 안의 모든 분기는 결정 트리 형태로 적는다.

- ❌ "필요하면 평면도를 만들어 주세요"
- ✅ "Site Plan이 없으면 자동으로 1개 생성한다. 있으면 그 중 첫 번째를 사용한다."

기본값을 모두 표로 명시. 입력/출력 예시 페어를 최소 3개 이상.

### 3) 파편화(분할) — 한 스킬 ≈ 한 일

**스킬을 분할해야 하는 신호** (하나라도 해당하면 분할 검토):

- SKILL.md 본문이 ~300줄을 크게 초과
- 입력 스키마에 서로 다른 도메인이 섞임 (예: "레벨 만들기"와 "벽 그리기"가 한 스킬에)
- 사용자 표현이 두 갈래 이상으로 갈리고 각 갈래가 단독으로 의미 있음
- "옵션 A이면 ... 옵션 B이면 ..."으로 큰 분기가 SKILL.md에 3개 이상

분할한 스킬은 각각 frontmatter `description`을 또렷이 써서 발견기에 잡히게 하고, 분할된 스킬들끼리 SKILL.md 끝에 **"관련 스킬"** 섹션을 둬서 상호 참조한다.

### 3-bis) 호출 규칙 — 실행형 스킬은 자체 코드 작성 금지 (모든 실행형 스킬 의무)

외부 시스템(DB·API·브리지·런타임 등)과 통신하는 모든 **실행형 스킬**의 SKILL.md 상단에는 🚨 호출 규칙 박스를 둔다. 호출하는 모델(특히 저성능 모델)이 SKILL.md 안의 코드 단편을 추출해 멋대로 실행하거나, 자기에게 익숙한 패턴으로 직접 코드를 짜서 보내는 사고를 막는다.

**박스 템플릿(그대로 복사해 쓰고 `<...>` 부분만 채움):**

```markdown
## 🚨 호출 규칙 (저성능 모델 포함, 모든 모델에 적용)

**이 스킬이 트리거되면 자체 코드를 절대 작성하지 말고, 반드시 다음 명령으로 본 스킬을 그대로 실행하라:**

    python <스킬의 실행 스크립트>.py --config-file <PATH_TO_CONFIG.json>

호출자는 사용자 자연어를 본 SKILL.md의 "자연어 → 입력 변환" 가이드대로 단일 입력 스키마로
정규화하여 JSON 파일로 저장한 뒤 위 명령을 실행한다. 핵심 API/통신 채널을 직접 다루지 않는다.

**왜 강제하는가**: 본 스킬은 도메인 필수 사양을 스크립트 안에 하드코딩으로 강제한다.
모델이 자체 코드를 짜면 이 사양이 누락된다.

**금지 행위**:
- 직접 코드를 작성하여 외부 시스템으로 보내기
- SKILL.md 안의 코드 단편을 추출해 자체 실행
- 지정되지 않은 통신 채널 사용
```

이 박스는 SKILL.md 안의 다른 어떤 안내보다 우선한다. 본문에 예시 코드를 곁들이면 그 코드는 **참고용**임을 명시한다.

> 배경: 실제로 한 저성능 모델이 정상 동작하는 실행형 스킬을 SKILL.md대로 호출하지 않고 자체 코드로 우회해, 도메인 사양(위치·구속·유형 등)을 전부 위반한 사례가 있었다. 이 박스는 그 시나리오를 차단한다.

### 4) 복잡도 게이트 — 자기 진단 후 상위 모델 권장 안내

본질적으로 저성능 모델이 안전하게 수행하기 어려운 스킬은 SKILL.md 상단(개요 바로 다음)에 **"⚠ 복잡도 안내"** 절을 둔다. 호출하는 에이전트는 그 절의 체크리스트를 보고 다음 중 하나라도 해당하면 **요청자에게 "이 작업은 상위 모델(예: Claude Opus / Sonnet)을 사용하시는 것이 안전합니다"라고 명시적으로 안내한 뒤** 진행/중단을 묻는다.

게이트 트리거 예시 (스킬마다 명시적 체크리스트로 박을 것):

- 입력 모호도가 임계 이상 (자연어로만 들어왔고 핵심 치수·좌표가 누락됨)
- 결과가 비가역적이며 한 번에 정합성을 맞춰야 함 (대규모 모델 일괄 변환, 스키마 마이그레이션)
- 외부 도메인 지식이 필요 (구조 안전성 판단, 법규 해석, 음악 작곡)
- 작업이 여러 파일·여러 트랜잭션에 걸쳐 일관성을 유지해야 함

게이트는 추측이 아니라 **명시적 체크리스트**로 적는다 ("이 중 하나라도 ✓이면 권장 안내"). 안내 메시지 템플릿도 SKILL.md에 박아둔다.

```markdown
## ⚠ 복잡도 안내

다음 중 하나라도 해당되면 **상위 모델 사용 권장**을 요청자에게 안내한 뒤 진행하라:

- [ ] 입력에 핵심 치수가 빠져 있고 자연어 추론이 필요함
- [ ] 작업이 비가역적이고 모델 전체 정합성에 영향
- [ ] (스킬 도메인 특화 조건)

권장 안내 메시지(그대로 사용):
> "이 작업은 정합성·추론 부담이 큽니다. Gemini Flash·Haiku 같은 저성능 모델로는
> 정확도가 떨어질 수 있어 Opus 또는 Sonnet 사용을 권장드립니다. 그래도 진행할까요?"
```

### 5) 검색 용이성 — 파편화해도 한 번에 도달 가능하게

스킬을 잘게 쪼개면 사용자가 어떤 스킬을 써야 하는지 모를 수 있다. 다음을 지킨다:

- frontmatter `description`에 **사용자 표현 예시**를 풍부히 넣는다 (`"…해줘"`, `"…그려줘"`, `"…정리해줘"`) — 발견기가 그 표현으로 이 스킬을 찾게.
- *무엇을 + 언제 쓰는지 + 핵심 용어*를 description에 빠짐없이 담는다 (자연어 요청 → 발견기 → 스킬로 도달하게).
- 분할된 스킬끼리는 SKILL.md 끝의 **"관련 스킬"** 섹션에서 상호 링크한다.

### 6) 자기충족적 SKILL.md — 외부 문서 의존 금지

SKILL.md는 단독으로 작업이 끝나도록 작성한다. 위키 문서를 안 읽어도 핵심 예시·기본값·실패 시 대응이 본문에 모두 있어야 한다. 위키는 깊은 배경 설명용으로만 분리한다.

### 7) 스킬 수행에 대한 검수 기준 및 절차(DoD) 필수 포함

모든 스킬 문서(SKILL.md)에는 해당 스킬이 완료(Done)되었음을 정의하는 명확한 **검수 기준 및 절차(Definition of Done, DoD)**와 **물리적 검증 체크리스트**를 명문화해야 한다. 
스킬 실행 결과로 생성되는 물리적 산출물(예: 캡처 이미지, 실행 로그, 결과 파일 등)의 파일 경로 및 형식을 검수 절차에 정확히 명시하여, 작업자가 임의로 완료를 판단하거나 실물 파일이 없는 상태에서 허위 완료 보고를 하는 행위(환각)를 원천 차단한다.

#### [skill-creator 스킬 자체의 완료 기준 (DoD) 및 물리적 검증 체크리스트]
본 `skill-creator` 스킬의 수행을 완료(Done)로 인정하기 위한 구체적인 물리적 검증 체크리스트는 다음과 같다:
- **[DoD 1] 스킬 명세서 물리적 생성**: 대상 스킬의 `SKILL.md` 파일이 `스킬/<등급>/<스킬명>/` 경로에 실제로 생성되어 있어야 하며, 파일 내부에는 `name`, `description` 메타데이터 및 실행 프로토콜이 작성되어 있어야 한다.
- **[DoD 2] 테스트 벤치마크 데이터 생성**: 대상 스킬의 `evals/evals.json` 파일이 존재해야 하며, 2개 이상의 실제 테스트 케이스 데이터가 등록되어 있어야 한다.
- **[DoD 3] 정량 벤치마크 결과 및 피드백**: 스킬 workspace 하위 `iteration-N` 폴더 내에 `benchmark.json`, `benchmark.md`, `feedback.json` 파일이 정상 존재하고, 속도/비용/정량적 Pass Rate 및 사용자 리뷰 피드백 내용이 공란 없이 실측 수치로 기록되어 있어야 한다.
- **[DoD 4] 필수 검증 구조 반영**: 작성 및 보완된 `SKILL.md` 상단에 🚨 호출 규칙 박스, 복잡도 안내, 그리고 DoD 체크리스트가 명문화되어 있어 저성능 모델이 즉시 인지할 수 있는 구조여야 한다.


### 작성 시 자체 점검 체크리스트

새/개정 스킬 작성 후 본인이 다음을 한 번씩 확인:

- [ ] **🚨 호출 규칙 박스가 SKILL.md 상단에 박혀 있는가? (실행형 스킬 의무, 위 3-bis 절 참조)**
- [ ] SKILL.md를 처음 보는 저성능 모델 입장에서 읽어봤는가? 모호한 표현이 없는가?
- [ ] 자연어 → 입력 변환 예시 페어가 ≥ 3개 있는가?
- [ ] 모든 기본값이 표로 명시되어 있는가?
- [ ] 입력 스키마는 단 하나의 단순한 형태인가?
- [ ] 본문이 300줄을 크게 넘지 않는가? 분할 신호가 있다면 분할했는가?
- [ ] 복잡도 게이트가 필요한 스킬이라면 ⚠ 절과 권장 안내 메시지가 박혀 있는가?
- [ ] frontmatter `description`에 요청 표현·핵심 용어가 풍부해 발견기가 잘 찾는가? (`발견.py`로 확인)
- [ ] 분할된 형제 스킬과 "관련 스킬" 섹션으로 상호 참조되어 있는가?
- [ ] **스킬 수행에 대한 검수 기준 및 절차(DoD)와 물리적 검증 체크리스트가 본문에 명문화되어 있는가?**

---

## Creating a skill

### Capture Intent

Start by understanding the user's intent. The current conversation might already contain a workflow the user wants to capture (e.g., they say "turn this into a skill"). If so, extract answers from the conversation history first — the tools used, the sequence of steps, corrections the user made, input/output formats observed. The user may need to fill the gaps, and should confirm before proceeding to the next step.

1. What should this skill enable Claude to do?
2. When should this skill trigger? (what user phrases/contexts)
3. What's the expected output format?
4. Should we set up test cases to verify the skill works? Skills with objectively verifiable outputs (file transforms, data extraction, code generation, fixed workflow steps) benefit from test cases. Skills with subjective outputs (writing style, art) often don't need them. Suggest the appropriate default based on the skill type, but let the user decide.

### Interview and Research

Proactively ask questions about edge cases, input/output formats, example files, success criteria, and dependencies. Wait to write test prompts until you've got this part ironed out.

Check available MCPs - if useful for research (searching docs, finding similar skills, looking up best practices), research in parallel via subagents if available, otherwise inline. Come prepared with context to reduce burden on the user.

### Write the SKILL.md

Based on the user interview, fill in these components:

- **name**: Skill identifier
- **description**: When to trigger, what it does. This is the primary triggering mechanism - include both what the skill does AND specific contexts for when to use it. All "when to use" info goes here, not in the body. Note: currently Claude has a tendency to "undertrigger" skills -- to not use them when they'd be useful. To combat this, please make the skill descriptions a little bit "pushy". So for instance, instead of "How to build a simple fast dashboard to display internal Anthropic data.", you might write "How to build a simple fast dashboard to display internal Anthropic data. Make sure to use this skill whenever the user mentions dashboards, data visualization, internal metrics, or wants to display any kind of company data, even if they don't explicitly ask for a 'dashboard.'"
- **compatibility**: Required tools, dependencies (optional, rarely needed)
- **the rest of the skill :)**

### Skill Writing Guide

#### Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description required)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - 고정 실행 코드 (불변, 그대로 실행하거나 MCP로 전달)
    ├── references/ - 레퍼런스 코드/문서 (참고하여 상황에 맞게 변형)
    └── assets/     - Files used in output (templates, icons, fonts)
```

#### scripts/ vs references/ 분리 원칙

스킬의 코드를 두 가지로 명확히 분리한다:

**scripts/ (고정 실행 코드)**
- 절대 불변의 로직. 그대로 실행하거나 MCP/Bridge로 복붙 전달한다.
- 에이전트가 내용을 변형하지 않는다. 실행만 한다.
- 실패 시: 코드를 새로 짜는 게 아니라 **조건문/fallback을 추가**하여 어떤 환경에서도 동작하도록 보강한다.
- 번호 접두사로 실행 순서를 명시한다 (예: `01_connect.py`, `02_query.py`, `03_place.py`).
- 예시: MCP 연결, 공통 유틸리티, 검증된 배치 로직

**references/ (레퍼런스 코드/문서)**
- 에이전트가 참고하여 현재 상황에 맞게 자유롭게 재작성하는 코드/문서.
- 그대로 복사하지 않는다. 맥락에 맞게 변형·조합하여 사용한다.
- 예시: 파라미터 조립 패턴, 통신 프로토콜 가이드, 에러 핸들링 레시피

**판단 기준: "이 코드가 상황에 관계없이 항상 동일하게 실행되어야 하는가?"**
- Yes → `scripts/`에 고정
- No → `references/`에 레퍼런스로 배치

#### Progressive Disclosure

Skills use a three-level loading system:
1. **Metadata** (name + description) - Always in context (~100 words)
2. **SKILL.md body** - In context whenever skill triggers (<500 lines ideal)
3. **Bundled resources** - As needed:
   - `scripts/` → 읽지 않고 그대로 실행 (고정 코드)
   - `references/` → 필요할 때 읽어서 참고 (상황별 변형)

These word counts are approximate and you can feel free to go longer if needed.

**Key patterns:**
- Keep SKILL.md under 500 lines; if you're approaching this limit, add an additional layer of hierarchy along with clear pointers about where the model using the skill should go next to follow up.
- Reference files clearly from SKILL.md with guidance on when to read them
- For large reference files (>300 lines), include a table of contents

**Domain organization**: When a skill supports multiple domains/frameworks, organize by variant:
```
cloud-deploy/
├── SKILL.md (workflow + selection)
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```
Claude reads only the relevant reference file.

#### Principle of Lack of Surprise

This goes without saying, but skills must not contain malware, exploit code, or any content that could compromise system security. A skill's contents should not surprise the user in their intent if described. Don't go along with requests to create misleading skills or skills designed to facilitate unauthorized access, data exfiltration, or other malicious activities. Things like a "roleplay as an XYZ" are OK though.

#### Writing Patterns

Prefer using the imperative form in instructions.

**Defining output formats** - You can do it like this:
```markdown
## Report structure
ALWAYS use this exact template:
# [Title]
## Executive summary
## Key findings
## Recommendations
```

**Examples pattern** - It's useful to include examples. You can format them like this (but if "Input" and "Output" are in the examples you might want to deviate a little):
```markdown
## Commit message format
**Example 1:**
Input: Added user authentication with JWT tokens
Output: feat(auth): implement JWT-based authentication
```

### Writing Style

Try to explain to the model why things are important in lieu of heavy-handed musty MUSTs. Use theory of mind and try to make the skill general and not super-narrow to specific examples. Start by writing a draft and then look at it with fresh eyes and improve it.

### Test Cases

After writing the skill draft, come up with 2-3 realistic test prompts — the kind of thing a real user would actually say. Share them with the user: [you don't have to use this exact language] "Here are a few test cases I'd like to try. Do these look right, or do you want to add more?" Then run them.

Save test cases to `evals/evals.json`. Don't write assertions yet — just the prompts. You'll draft assertions in the next step while the runs are in progress.

```json
{
  "skill_name": "example-skill",
  "evals": [
    {
      "id": 1,
      "prompt": "User's task prompt",
      "expected_output": "Description of expected result",
      "files": []
    }
  ]
}
```

See `references/schemas.md` for the full schema (including the `assertions` field, which you'll add later).

## Running and evaluating test cases

This section is one continuous sequence — don't stop partway through. Do NOT use `/skill-test` or any other testing skill.

Put results in `<skill-name>-workspace/` as a sibling to the skill directory. Within the workspace, organize results by iteration (`iteration-1/`, `iteration-2/`, etc.) and within that, each test case gets a directory (`eval-0/`, `eval-1/`, etc.). Don't create all of this upfront — just create directories as you go.

### Step 1: Spawn all runs (with-skill AND baseline) in the same turn

For each test case, spawn two subagents in the same turn — one with the skill, one without. This is important: don't spawn the with-skill runs first and then come back for baselines later. Launch everything at once so it all finishes around the same time.

**With-skill run:**

```
Execute this task:
- Skill path: <path-to-skill>
- Task: <eval prompt>
- Input files: <eval files if any, or "none">
- Save outputs to: <workspace>/iteration-<N>/eval-<ID>/with_skill/outputs/
- Outputs to save: <what the user cares about — e.g., "the .docx file", "the final CSV">
```

**Baseline run** (same prompt, but the baseline depends on context):
- **Creating a new skill**: no skill at all. Same prompt, no skill path, save to `without_skill/outputs/`.
- **Improving an existing skill**: the old version. Before editing, snapshot the skill (`cp -r <skill-path> <workspace>/skill-snapshot/`), then point the baseline subagent at the snapshot. Save to `old_skill/outputs/`.

Write an `eval_metadata.json` for each test case (assertions can be empty for now). Give each eval a descriptive name based on what it's testing — not just "eval-0". Use this name for the directory too. If this iteration uses new or modified eval prompts, create these files for each new eval directory — don't assume they carry over from previous iterations.

```json
{
  "eval_id": 0,
  "eval_name": "descriptive-name-here",
  "prompt": "The user's task prompt",
  "assertions": []
}
```

### Step 2: While runs are in progress, draft assertions

Don't just wait for the runs to finish — you can use this time productively. Draft quantitative assertions for each test case and explain them to the user. If assertions already exist in `evals/evals.json`, review them and explain what they check.

Good assertions are objectively verifiable and have descriptive names — they should read clearly in the benchmark viewer so someone glancing at the results immediately understands what each one checks. Subjective skills (writing style, design quality) are better evaluated qualitatively — don't force assertions onto things that need human judgment.

Update the `eval_metadata.json` files and `evals/evals.json` with the assertions once drafted. Also explain to the user what they'll see in the viewer — both the qualitative outputs and the quantitative benchmark.

### Step 3: As runs complete, capture timing data

When each subagent task completes, you receive a notification containing `total_tokens` and `duration_ms`. Save this data immediately to `timing.json` in the run directory:

```json
{
  "total_tokens": 84852,
  "duration_ms": 23332,
  "total_duration_seconds": 23.3
}
```

This is the only opportunity to capture this data — it comes through the task notification and isn't persisted elsewhere. Process each notification as it arrives rather than trying to batch them.

### Step 4: Grade, aggregate, and launch the viewer

Once all runs are done:

1. **Grade each run** — spawn a grader subagent (or grade inline) that reads `agents/grader.md` and evaluates each assertion against the outputs. Save results to `grading.json` in each run directory. The grading.json expectations array must use the fields `text`, `passed`, and `evidence` (not `name`/`met`/`details` or other variants) — the viewer depends on these exact field names. For assertions that can be checked programmatically, write and run a script rather than eyeballing it — scripts are faster, more reliable, and can be reused across iterations.

2. **Aggregate into benchmark** — run the aggregation script from the skill-creator directory:
   ```bash
   python -m scripts.aggregate_benchmark <workspace>/iteration-N --skill-name <name>
   ```
   This produces `benchmark.json` and `benchmark.md` with pass_rate, time, and tokens for each configuration, with mean ± stddev and the delta. If generating benchmark.json manually, see `references/schemas.md` for the exact schema the viewer expects.
Put each with_skill version before its baseline counterpart.

3. **Do an analyst pass** — read the benchmark data and surface patterns the aggregate stats might hide. See `agents/analyzer.md` (the "Analyzing Benchmark Results" section) for what to look for — things like assertions that always pass regardless of skill (non-discriminating), high-variance evals (possibly flaky), and time/token tradeoffs.

4. **Launch the viewer** with both qualitative outputs and quantitative data:
   ```bash
   nohup python <skill-creator-path>/eval-viewer/generate_review.py \
     <workspace>/iteration-N \
     --skill-name "my-skill" \
     --benchmark <workspace>/iteration-N/benchmark.json \
     > /dev/null 2>&1 &
   VIEWER_PID=$!
   ```
   For iteration 2+, also pass `--previous-workspace <workspace>/iteration-<N-1>`.

   **Cowork / headless environments:** If `webbrowser.open()` is not available or the environment has no display, use `--static <output_path>` to write a standalone HTML file instead of starting a server. Feedback will be downloaded as a `feedback.json` file when the user clicks "Submit All Reviews". After download, copy `feedback.json` into the workspace directory for the next iteration to pick up.

Note: please use generate_review.py to create the viewer; there's no need to write custom HTML.

5. **Tell the user** something like: "I've opened the results in your browser. There are two tabs — 'Outputs' lets you click through each test case and leave feedback, 'Benchmark' shows the quantitative comparison. When you're done, come back here and let me know."

### What the user sees in the viewer

The "Outputs" tab shows one test case at a time:
- **Prompt**: the task that was given
- **Output**: the files the skill produced, rendered inline where possible
- **Previous Output** (iteration 2+): collapsed section showing last iteration's output
- **Formal Grades** (if grading was run): collapsed section showing assertion pass/fail
- **Feedback**: a textbox that auto-saves as they type
- **Previous Feedback** (iteration 2+): their comments from last time, shown below the textbox

The "Benchmark" tab shows the stats summary: pass rates, timing, and token usage for each configuration, with per-eval breakdowns and analyst observations.

Navigation is via prev/next buttons or arrow keys. When done, they click "Submit All Reviews" which saves all feedback to `feedback.json`.

### Step 5: Read the feedback

When the user tells you they're done, read `feedback.json`:

```json
{
  "reviews": [
    {"run_id": "eval-0-with_skill", "feedback": "the chart is missing axis labels", "timestamp": "..."},
    {"run_id": "eval-1-with_skill", "feedback": "", "timestamp": "..."},
    {"run_id": "eval-2-with_skill", "feedback": "perfect, love this", "timestamp": "..."}
  ],
  "status": "complete"
}
```

Empty feedback means the user thought it was fine. Focus your improvements on the test cases where the user had specific complaints.

Kill the viewer server when you're done with it:

```bash
kill $VIEWER_PID 2>/dev/null
```

---

## Improving the skill

This is the heart of the loop. You've run the test cases, the user has reviewed the results, and now you need to make the skill better based on their feedback.

### How to think about improvements

1. **Generalize from the feedback.** The big picture thing that's happening here is that we're trying to create skills that can be used a million times (maybe literally, maybe even more who knows) across many different prompts. Here you and the user are iterating on only a few examples over and over again because it helps move faster. The user knows these examples in and out and it's quick for them to assess new outputs. But if the skill you and the user are codeveloping works only for those examples, it's useless. Rather than put in fiddly overfitty changes, or oppressively constrictive MUSTs, if there's some stubborn issue, you might try branching out and using different metaphors, or recommending different patterns of working. It's relatively cheap to try and maybe you'll land on something great.

2. **Keep the prompt lean.** Remove things that aren't pulling their weight. Make sure to read the transcripts, not just the final outputs — if it looks like the skill is making the model waste a bunch of time doing things that are unproductive, you can try getting rid of the parts of the skill that are making it do that and seeing what happens.

3. **Explain the why.** Try hard to explain the **why** behind everything you're asking the model to do. Today's LLMs are *smart*. They have good theory of mind and when given a good harness can go beyond rote instructions and really make things happen. Even if the feedback from the user is terse or frustrated, try to actually understand the task and why the user is writing what they wrote, and what they actually wrote, and then transmit this understanding into the instructions. If you find yourself writing ALWAYS or NEVER in all caps, or using super rigid structures, that's a yellow flag — if possible, reframe and explain the reasoning so that the model understands why the thing you're asking for is important. That's a more humane, powerful, and effective approach.

4. **Look for repeated work across test cases.** Read the transcripts from the test runs and notice if the subagents all independently wrote similar helper scripts or took the same multi-step approach to something. If all 3 test cases resulted in the subagent writing a `create_docx.py` or a `build_chart.py`, that's a strong signal the skill should bundle that script. Write it once, put it in `scripts/`, and tell the skill to use it. This saves every future invocation from reinventing the wheel.

   **고정 코드 승격 판단**: 반복되는 코드 중에서도 "상황에 관계없이 항상 동일하게 실행되는 코드"는 `scripts/`에 고정 실행 코드로 넣는다. 반면 "맥락에 따라 파라미터나 로직이 달라지는 코드"는 `references/`에 레퍼런스로 넣어서 에이전트가 참고하여 재작성하게 한다. 고정 코드가 특정 환경에서 실패하면 코드를 새로 짜는 게 아니라 조건문/fallback을 추가하여 어떤 경우에도 실행 가능하도록 강화한다. **이 워크스페이스 보강**: 승격 대상이 *여러 스킬에 걸쳐 재사용될 범용 기능*이면 스킬의 `scripts/`가 아니라 **`도구/`에 도구로 승격**하고 발견 잘 되는 `description`을 단다 (위 "도구 우선" 절 참조).

This task is pretty important (we are trying to create billions a year in economic value here!) and your thinking time is not the blocker; take your time and really mull things over. I'd suggest writing a draft revision and then looking at it anew and making improvements. Really do your best to get into the head of the user and understand what they want and need.

### The iteration loop

After improving the skill:

1. Apply your improvements to the skill
2. Rerun all test cases into a new `iteration-<N+1>/` directory, including baseline runs. If you're creating a new skill, the baseline is always `without_skill` (no skill) — that stays the same across iterations. If you're improving an existing skill, use your judgment on what makes sense as the baseline: the original version the user came in with, or the previous iteration.
3. Launch the reviewer with `--previous-workspace` pointing at the previous iteration
4. Wait for the user to review and tell you they're done
5. Read the new feedback, improve again, repeat

Keep going until:
- The user says they're happy
- The feedback is all empty (everything looks good)
- You're not making meaningful progress

---

## Advanced: Blind comparison

For situations where you want a more rigorous comparison between two versions of a skill (e.g., the user asks "is the new version actually better?"), there's a blind comparison system. Read `agents/comparator.md` and `agents/analyzer.md` for the details. The basic idea is: give two outputs to an independent agent without telling it which is which, and let it judge quality. Then analyze why the winner won.

This is optional, requires subagents, and most users won't need it. The human review loop is usually sufficient.

---

## Description Optimization

The description field in SKILL.md frontmatter is the primary mechanism that determines whether Claude invokes a skill. After creating or improving a skill, offer to optimize the description for better triggering accuracy.

### Step 1: Generate trigger eval queries

Create 20 eval queries — a mix of should-trigger and should-not-trigger. Save as JSON:

```json
[
  {"query": "the user prompt", "should_trigger": true},
  {"query": "another prompt", "should_trigger": false}
]
```

The queries must be realistic and something a Claude Code or Claude.ai user would actually type. Not abstract requests, but requests that are concrete and specific and have a good amount of detail. For instance, file paths, personal context about the user's job or situation, column names and values, company names, URLs. A little bit of backstory. Some might be in lowercase or contain abbreviations or typos or casual speech. Use a mix of different lengths, and focus on edge cases rather than making them clear-cut (the user will get a chance to sign off on them).

Bad: `"Format this data"`, `"Extract text from PDF"`, `"Create a chart"`

Good: `"ok so my boss just sent me this xlsx file (its in my downloads, called something like 'Q4 sales final FINAL v2.xlsx') and she wants me to add a column that shows the profit margin as a percentage. The revenue is in column C and costs are in column D i think"`

For the **should-trigger** queries (8-10), think about coverage. You want different phrasings of the same intent — some formal, some casual. Include cases where the user doesn't explicitly name the skill or file type but clearly needs it. Throw in some uncommon use cases and cases where this skill competes with another but should win.

For the **should-not-trigger** queries (8-10), the most valuable ones are the near-misses — queries that share keywords or concepts with the skill but actually need something different. Think adjacent domains, ambiguous phrasing where a naive keyword match would trigger but shouldn't, and cases where the query touches on something the skill does but in a context where another tool is more appropriate.

The key thing to avoid: don't make should-not-trigger queries obviously irrelevant. "Write a fibonacci function" as a negative test for a PDF skill is too easy — it doesn't test anything. The negative cases should be genuinely tricky.

### Step 2: Review with user

Present the eval set to the user for review using the HTML template:

1. Read the template from `assets/eval_review.html`
2. Replace the placeholders:
   - `__EVAL_DATA_PLACEHOLDER__` → the JSON array of eval items (no quotes around it — it's a JS variable assignment)
   - `__SKILL_NAME_PLACEHOLDER__` → the skill's name
   - `__SKILL_DESCRIPTION_PLACEHOLDER__` → the skill's current description
3. Write to a temp file (e.g., `/tmp/eval_review_<skill-name>.html`) and open it: `open /tmp/eval_review_<skill-name>.html`
4. The user can edit queries, toggle should-trigger, add/remove entries, then click "Export Eval Set"
5. The file downloads to `~/Downloads/eval_set.json` — check the Downloads folder for the most recent version in case there are multiple (e.g., `eval_set (1).json`)

This step matters — bad eval queries lead to bad descriptions.

### Step 3: Run the optimization loop

Tell the user: "This will take some time — I'll run the optimization loop in the background and check on it periodically."

Save the eval set to the workspace, then run in the background:

```bash
python -m scripts.run_loop \
  --eval-set <path-to-trigger-eval.json> \
  --skill-path <path-to-skill> \
  --model <model-id-powering-this-session> \
  --max-iterations 5 \
  --verbose
```

Use the model ID from your system prompt (the one powering the current session) so the triggering test matches what the user actually experiences.

While it runs, periodically tail the output to give the user updates on which iteration it's on and what the scores look like.

This handles the full optimization loop automatically. It splits the eval set into 60% train and 40% held-out test, evaluates the current description (running each query 3 times to get a reliable trigger rate), then calls Claude to propose improvements based on what failed. It re-evaluates each new description on both train and test, iterating up to 5 times. When it's done, it opens an HTML report in the browser showing the results per iteration and returns JSON with `best_description` — selected by test score rather than train score to avoid overfitting.

### How skill triggering works

Understanding the triggering mechanism helps design better eval queries. Skills appear in Claude's `available_skills` list with their name + description, and Claude decides whether to consult a skill based on that description. The important thing to know is that Claude only consults skills for tasks it can't easily handle on its own — simple, one-step queries like "read this PDF" may not trigger a skill even if the description matches perfectly, because Claude can handle them directly with basic tools. Complex, multi-step, or specialized queries reliably trigger skills when the description matches.

This means your eval queries should be substantive enough that Claude would actually benefit from consulting a skill. Simple queries like "read file X" are poor test cases — they won't trigger skills regardless of description quality.

### Step 4: Apply the result

Take `best_description` from the JSON output and update the skill's SKILL.md frontmatter. Show the user before/after and report the scores.

---

### Package and Present (only if `present_files` tool is available)

Check whether you have access to the `present_files` tool. If you don't, skip this step. If you do, package the skill and present the .skill file to the user:

```bash
python -m scripts.package_skill <path/to/skill-folder>
```

After packaging, direct the user to the resulting `.skill` file path so they can install it.

---

## Claude.ai-specific instructions

In Claude.ai, the core workflow is the same (draft → test → review → improve → repeat), but because Claude.ai doesn't have subagents, some mechanics change. Here's what to adapt:

**Running test cases**: No subagents means no parallel execution. For each test case, read the skill's SKILL.md, then follow its instructions to accomplish the test prompt yourself. Do them one at a time. This is less rigorous than independent subagents (you wrote the skill and you're also running it, so you have full context), but it's a useful sanity check — and the human review step compensates. Skip the baseline runs — just use the skill to complete the task as requested.

**Reviewing results**: If you can't open a browser (e.g., Claude.ai's VM has no display, or you're on a remote server), skip the browser reviewer entirely. Instead, present results directly in the conversation. For each test case, show the prompt and the output. If the output is a file the user needs to see (like a .docx or .xlsx), save it to the filesystem and tell them where it is so they can download and inspect it. Ask for feedback inline: "How does this look? Anything you'd change?"

**Benchmarking**: Skip the quantitative benchmarking — it relies on baseline comparisons which aren't meaningful without subagents. Focus on qualitative feedback from the user.

**The iteration loop**: Same as before — improve the skill, rerun the test cases, ask for feedback — just without the browser reviewer in the middle. You can still organize results into iteration directories on the filesystem if you have one.

**Description optimization**: This section requires the `claude` CLI tool (specifically `claude -p`) which is only available in Claude Code. Skip it if you're on Claude.ai.

**Blind comparison**: Requires subagents. Skip it.

**Packaging**: The `package_skill.py` script works anywhere with Python and a filesystem. On Claude.ai, you can run it and the user can download the resulting `.skill` file.

**Updating an existing skill**: The user might be asking you to update an existing skill, not create a new one. In this case:
- **Preserve the original name.** Note the skill's directory name and `name` frontmatter field -- use them unchanged. E.g., if the installed skill is `research-helper`, output `research-helper.skill` (not `research-helper-v2`).
- **Copy to a writeable location before editing.** The installed skill path may be read-only. Copy to `/tmp/skill-name/`, edit there, and package from the copy.
- **If packaging manually, stage in `/tmp/` first**, then copy to the output directory -- direct writes may fail due to permissions.

---

## Cowork-Specific Instructions

If you're in Cowork, the main things to know are:

- You have subagents, so the main workflow (spawn test cases in parallel, run baselines, grade, etc.) all works. (However, if you run into severe problems with timeouts, it's OK to run the test prompts in series rather than parallel.)
- You don't have a browser or display, so when generating the eval viewer, use `--static <output_path>` to write a standalone HTML file instead of starting a server. Then proffer a link that the user can click to open the HTML in their browser.
- For whatever reason, the Cowork setup seems to disincline Claude from generating the eval viewer after running the tests, so just to reiterate: whether you're in Cowork or in Claude Code, after running tests, you should always generate the eval viewer for the human to look at examples before revising the skill yourself and trying to make corrections, using `generate_review.py` (not writing your own boutique html code). Sorry in advance but I'm gonna go all caps here: GENERATE THE EVAL VIEWER *BEFORE* evaluating inputs yourself. You want to get them in front of the human ASAP!
- Feedback works differently: since there's no running server, the viewer's "Submit All Reviews" button will download `feedback.json` as a file. You can then read it from there (you may have to request access first).
- Packaging works — `package_skill.py` just needs Python and a filesystem.
- Description optimization (`run_loop.py` / `run_eval.py`) should work in Cowork just fine since it uses `claude -p` via subprocess, not a browser, but please save it until you've fully finished making the skill and the user agrees it's in good shape.
- **Updating an existing skill**: The user might be asking you to update an existing skill, not create a new one. Follow the update guidance in the claude.ai section above.

---

## Reference files

The agents/ directory contains instructions for specialized subagents. Read them when you need to spawn the relevant subagent.

- `agents/grader.md` — How to evaluate assertions against outputs
- `agents/comparator.md` — How to do blind A/B comparison between two outputs
- `agents/analyzer.md` — How to analyze why one version beat another

The references/ directory has additional documentation:
- `references/schemas.md` — JSON structures for evals.json, grading.json, etc.

---

Repeating one more time the core loop here for emphasis:

- Figure out what the skill is about
- Draft or edit the skill
- Run claude-with-access-to-the-skill on test prompts
- With the user, evaluate the outputs:
  - Create benchmark.json and run `eval-viewer/generate_review.py` to help the user review them
  - Run quantitative evals
- Repeat until you and the user are satisfied
- Package the final skill and return it to the user.

Please add steps to your TodoList, if you have such a thing, to make sure you don't forget. If you're in Cowork, please specifically put "Create evals JSON and run `eval-viewer/generate_review.py` so human can review test cases" in your TodoList to make sure it happens.

Good luck!
