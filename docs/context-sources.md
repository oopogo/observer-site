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

1. `session.contextTokens`
   - OpenClaw 세션 저장소에 이미 기록된 값.
   - 관제 표시는 이 값을 가장 우선한다.
2. `observer.policy.openai-codex`
   - 세션 저장값이 없고 모델/프로바이더가 `gpt-5*` 또는 `openai-codex`인 경우 관제 정책상 `1,000,000`으로 표시한다.
3. `observer.default`
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
