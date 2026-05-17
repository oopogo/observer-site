# 관제 컨텍스트 표시 기준

관제 카드의 `CHAT CTX`는 OpenClaw 전체 설정을 새로 정하는 기능이 아니라, **관제 채팅 세션 하나의 현재 토큰 사용량을 보여주는 표시값**이다.

## 표시값 계산 위치

- Backend: `server.py`
  - `context_window_info(row)`
  - `session_context_usage_ratio(row)`
  - `summarize_agents()`에서 agent payload에 아래 필드를 넣는다.
    - `contextTokens`
    - `contextMaxTokens`
    - `contextRemainingPercent`
    - `contextSource`
    - `contextPolicy`
    - `contextSessionKey`
- Frontend: `index.html`
  - `renderContextBattery(agent)`

## 출처 우선순위

1. `model.auto.*`
   - 현재 세션 모델 또는 agent config의 primary model을 기준으로 자동 추정한 context window.
   - 세션 저장소의 `contextTokens`가 오래된 값일 수 있으므로 모델 자동값을 가장 우선한다.
   - 현재 관제 매핑:
     - `openai-codex/gpt-5*`, `gpt-5*` → `1,000,000`
     - `kimi-k2.6`, `kimi*` → `262,144`
     - `claude*` → `200,000`
2. `session.contextTokens`
   - 모델을 알 수 없을 때 OpenClaw 세션 저장소에 이미 기록된 값을 사용한다.
3. `observer.default` 또는 config 기본값
   - 위 둘 다 없으면 관제 기본값 `1,000,000`으로 표시한다.

## 실제 OpenClaw 쪽 관련 위치

관제 표시는 위 함수 하나로 정리했지만, OpenClaw 런타임 자체에는 별도 context 출처가 있다.

- `/home/oopogo/.openclaw/openclaw.json`
  - `agents.defaults.contextTokens`가 schema에서 허용되면 기본값으로 쓰일 수 있다.
  - 현재 config normalize/auto-restore 때문에 임의 삽입은 신중해야 한다.
- `/home/oopogo/.openclaw/agents/*/sessions/sessions.json`
  - 각 세션의 `contextTokens` 저장값.
  - 관제 카드가 가장 우선해서 읽는 값.
- `/home/oopogo/.npm-global/lib/node_modules/openclaw/dist/defaults-*.js`
  - OpenClaw 설치 패키지의 기본 context token 상수.
- `/home/oopogo/.npm-global/lib/node_modules/openclaw/dist/provider-catalog-*.js`
  - provider/model catalog의 context window 기본값.

## 운영 원칙

- 관제 카드의 context 표시는 **보정/표시 정책**이지 gateway config 변경이 아니다.
- 모델 자동값은 `context_window_info()`에서만 결정한다.
- 자동 새 세션 전환은 `session_context_usage_ratio()`만 사용한다.
- `running/queued/pending` 상태만으로 새 세션 전환하지 않는다.
- 표시 기준을 바꾸려면 `context_window_info()`만 수정한다.

## 자동 세션 인계

관제 채팅 세션의 context 사용률이 `CONTEXT_ROLLOVER_RATIO` 이상이면 다음 사용자 메시지를 보낼 때 새 세션을 만든다.

- 기준: `session_context_usage_ratio(row) >= 0.95`
- 위치:
  - `maybe_rollover_chat_session_from_sessions()`
  - `fast_chat_session_for_send()`
- 새 세션 생성 후 `build_observer_handoff_text()`로 인계 패킷을 만든다.
- 인계 패킷은 다음 사용자 요청 앞에 붙어 새 세션의 첫 실제 요청으로 전달된다.
- 인계 패킷에는 다음이 들어간다.
  - 최근 대화 압축
  - 미완료/실패 작업 상태
  - 핵심 결정/주의사항 후보
  - 최근 완료보고 후보
  - 세션 연속성 규칙

새 세션은 인계 패킷을 포함해 시작하므로 완전한 `0 token` 세션이 아니다. 카드에는 `남은 CTX`와 함께 인계 패킷 기록이 있는 세션이면 `인계포함`을 표시한다.

## 카드 상태 판정 기준

관제 카드는 서로 다른 상태 시간을 한 상수로 공유하지 않는다. 현재 기준은 다음과 같다.

- `작업 중`
  - 일반 세션 상태가 `running / processing / queued`이고 최근 3분 안에 갱신됨.
- `응답 대기`
  - 사용자 요청 전송 후 최종 응답 전, 0~3분.
- `응답 지연`
  - 사용자 요청 전송 후 최종 응답 전, 3~15분.
- `응답 만료`
  - 15분을 넘긴 pending은 카드 상태를 더 붙잡지 않도록 `expired`로 숨김 처리.
- `보고 대기`
  - 시작/진행 보고만 있고 최종 완료보고가 없는 상태, 0~15분.
- `보고 지연`
  - 보고 대기 상태가 15분을 넘김.
- `무음 경고`
  - 작업/응답/보고 상태에서 최근 가시 활동 또는 live session 갱신이 15분 이상 없음.
  - 단, live `작업 중` 세션은 active stale 기준(3분)을 우선해 너무 빨리 무음 경고를 붙이지 않는다.
- stale work-state 실패 처리
  - 등록 작업이 2시간 이상 갱신되지 않고 terminal row도 없으면 실패 처리.

이 기준은 observer-site 표시/정리 정책이며, OpenClaw gateway 런타임 설정을 변경하지 않는다.
