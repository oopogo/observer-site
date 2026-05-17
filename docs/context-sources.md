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
