# 완료보고 이벤트/감지 정책

대상: `main`, `observer`, `lina`, `mediacontentproducer` 전부.

## 1. 이벤트 지점

observer-site는 gateway를 반복 polling하지 않고 다음 지점에서 로컬 이벤트를 남긴다.

- observer-site chat bridge의 `sessions.send --expect-final` 반환
- 최종 텍스트가 없거나 내부 상태만 온 경우
- 전송 실패/timeout/error
- `/api/agents`가 이미 읽은 로컬 session snapshot에서 work item의 세션이 `done/failed/error/timeout`으로 바뀐 경우
- 외부/미래 hook은 `POST /api/events`로 `events.jsonl`에 기록 가능

## 2. 이벤트 저장

파일: `.data/events.jsonl`

기본 필드:

```json
{
  "id": "evt-...",
  "ts": 1779010000000,
  "eventKey": "agent|kind|status|session|request|work",
  "agentId": "observer",
  "sessionKey": "agent:observer:observer-site-chat:...",
  "requestId": "...",
  "workId": "...",
  "kind": "session.completed",
  "status": "completed-unreported",
  "summary": "...",
  "evidence": {}
}
```

`eventKey`가 같으면 중복 기록하지 않는다.

## 3. 완료보고 판정

작업 완료와 사용자 보고 완료는 별개다.

- 세션이 `done/completed/success`인데 visible assistant 완료보고가 없으면 `completed-unreported`.
- 세션이 `failed/error/timeout`이면 `failed`.
- 완료 이벤트 이후 visible assistant 완료보고가 확인되면 `reported`.
- `NO_REPLY`, 내부 JSON/status, pending/delayed-pending, 매크로/하네스 fallback은 완료보고로 보지 않는다.

## 4. 자동 회수

`completed-unreported`가 감지되면 카드 상태는 `완료 후 보고 누락`으로 유지한다.

같은 작업/세션에 대해 15분 쿨다운으로 에이전트에게 완료보고만 요청한다.

요청 원칙:

- 시스템이 대신 완료했다고 말하지 않는다.
- 에이전트에게 “새 작업 시작하지 말고 완료보고만 작성”을 요청한다.
- 응답은 자연어 완료보고만 사용자-visible로 남긴다.

## 5. fallback

주 동작은 이벤트 기반이지만, 이벤트 누락 대비로 `/api/agents`가 이미 사용하는 로컬 session snapshot과 `.data/work-state.json`만 가볍게 대조한다.

금지:

- gateway `sessions.list` 반복 호출
- OpenClaw cron으로 감시 루프 추가
- 매크로 완료보고 자동 생성
