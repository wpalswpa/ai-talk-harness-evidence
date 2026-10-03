# 여러 LLM 의논 하네스의 사회자 판정 기록과 실패 분류 근거

Codex CLI·Claude Code CLI와 API 모델을 한 대화에 묶고, 사회자 LLM이 "다음에 누가 말할지·무엇을 시킬지·끝낼지"를 JSON으로 판정하는 개인 프로젝트(2026-09-21~25)의 핵심 모듈과 회귀 검사입니다. 원본 저장소는 비공개이고, 이 저장소에는 포트폴리오에서 설명한 부분만 그대로 옮겼습니다. 웹 서버·화면·대화 기록(SQLite)은 넣지 않았습니다.

## 어떤 문제였나

Claude Code와 Codex 세션 사이에 맥락이 공유되지 않아 답을 서로 복사해 붙여 넣고 있었습니다. 두 도구를 한 방에서 의논시키되, 누가 말할 차례인지와 언제 끝낼지를 사람이 매번 정하지 않게 하는 것이 목표였습니다. 그러면서 세 가지를 확인해야 했습니다.

1. 사회자 LLM의 판정이 망가졌을 때(JSON 깨짐, 없는 참여자 지목, 빈 지시) 대화가 멈추지 않으면서도 그 사실이 기록에 남는가.
2. 호출 지연·토큰을 참여자 코드의 반환형을 바꾸지 않고 잴 수 있는가. 못 잰 값이 0으로 둔갑하지 않는가.
3. 분석용으로 쌓은 대화 기록을 믿을 수 있는가.

## 바로 실행

```bash
python -m pytest -q      # 69 passed (Python 3.12, 외부 패키지 없음)
```

검사는 API 키, CLI 로그인, 네트워크 없이 돕니다. `bridge.py`는 실제 CLI와 API를 부르는 코드지만 검사는 호출 계약(인자 이름, 반환형, stdin 처리)만 봅니다.

## 무엇을 보면 되나

| 궁금한 것 | 파일 |
|---|---|
| 사회자 판정 형식과 깨진 출력의 분류 | [`talk/orchestrator.py`](talk/orchestrator.py) `LEDGER_SCHEMA`, `parse_ledger`, 플래그 상수 |
| 판정을 상태로 복원해 다시 쓰는 자리 | [`talk/policy.py`](talk/policy.py) `PolicyState.from_decision_row`, `Policy.decide` |
| 반환형을 바꾸지 않는 측정 슬롯, 미측정과 0의 구분 | [`talk/telemetry.py`](talk/telemetry.py) |
| 결정의 원본과 보정본을 둘 다 저장하는 이유 | [`talk/store.py`](talk/store.py) `add_decision` |
| CLI 입출력 처리(stdin 차단, 오류 출력과 발언의 분리) | [`talk/bridge.py`](talk/bridge.py) CLI 참여자의 `ask`, `Codex.parse` |
| 기록 오염 38%의 원인과 수정 | [docs/decisions/001](docs/decisions/001-failure-banner-contamination.md) |
| 중단 시점 자동화를 실험으로 기각한 판단 | [docs/decisions/002](docs/decisions/002-stop-automation-no-go.md) |

## 핵심 설계

```
과제 입력
  → 참여자별 독립 초안 (서로 보지 않음)
  → 사회자 판정 {done, looping, next, instruction}   ← JSON 스키마, 깨지면 플래그를 남기고 규칙으로 대체
  → 지시받은 참여자 발언
  → 결과물
```

- **깨진 판정도 대화를 멈추지 않는다.** `parse_ledger`는 JSON 실패·없는 참여자·빈 지시·후보 없음을 각각 다른 코드(`parse_failed`, `invalid_next`, `empty_instruction`, `no_candidates`, `controller_error`)로 표시하고 규칙 기반 다음 발언자로 넘어갑니다. 표시 이름을 키 대신 돌려준 경우는 별칭 표로 복구하고 `alias_resolved`로 따로 셉니다. 복구를 실패로 세지 않고, 진짜 오류를 복구로 가리지도 않습니다.
- **판정의 원본과 보정본을 둘 다 저장한다.** `store.add_decision`은 사회자가 돌려준 값(raw)과 실제로 쓴 값(effective)을 함께 남깁니다. 나중에 "그때 상태"를 다시 만들어 다른 판정 방식을 돌려 볼 수 있게 하기 위해서입니다(`PolicyState.from_decision_row`).
- **측정은 반환형을 건드리지 않는다.** 참여자 `ask()`는 문자열을 돌려주고, 지연·토큰·비용은 호출 스레드의 슬롯에 남깁니다. 못 잰 값은 `None`이고, 구독 호출의 비용 0은 실제 0으로 구분합니다.

## 검사 이름이 곧 불변식입니다

| 불변식 | 검사 |
|---|---|
| CLI 실패 배너가 발언으로 기록되지 않고, 모든 CLI 참여자가 stdin을 닫는다(decisions/001) | `test_codex_failure_does_not_become_speech`, `test_all_cli_members_close_stdin` |
| 실패 코드가 서로 섞이지 않는다 | `test_controller_error_is_distinguished_from_parse_failure`, `test_alias_resolution_is_not_counted_as_fallback`, `test_alias_does_not_rescue_truly_invalid` |
| 결정의 원본·보정본이 저장되고 상태를 복원할 수 있다 | `test_raw_and_effective_both_persisted`, `test_state_is_recoverable_for_dataset` |
| 미측정과 0이 다르다 | `test_unmeasured_tokens_are_null_not_zero`, `test_subscription_cost_is_real_zero`, `test_null_and_zero_are_distinct_in_db` |
| 기록 실패는 대화를 깨지 않고, 측정 코드가 실제 오류를 삼키지 않는다 | `test_logging_failure_returns_none_not_raise`, `test_measure_does_not_swallow_real_errors` |

원본의 검사 중 서버 호출 인자를 대조하는 1개(`test_server_kwargs_are_covered`)와 작업 폴더 격리 검사는 서버 코드가 필요해 이 저장소에 없습니다.

## 원본 기록에서 확인한 수치 (비공개 DB, 2026-09-22~28 본인 사용 기록)

- 사회자 결정 78건(LLM 59, 규칙 19). 표시 이름 복구 18, 사회자 호출 실패 3, 파싱 실패 3, 빈 지시 3, 규칙 대체 3.
- 모델 호출 364건. 평균 지연은 사회자 판정 0.95초, 의논 발언 20.0초, 결과물 작성 61.4초. 여러 과제·참여자가 섞인 사용 기록 평균이며 성능 비교 실험이 아닙니다.
- 분석용 대화 기록 21건 중 8건(38%)이 CLI 실패 배너로 오염돼 있었고, 원인 2개를 고친 뒤 기존 기록은 걸러 냈습니다(decisions/001).
- 중단 시점 학습 모델은 라벨 10건에서 정확도 50%로 "항상 멈춤" 60%를 넘지 못해 만들지 않았습니다(decisions/002).

## 이 설계가 막지 못하는 것

- 사회자 판정이 "옳았는지"는 기록으로 알 수 없습니다. 형식이 맞고 실행됐다는 것만 남습니다.
- `policy.py`의 `Policy.decide`는 판정 방식을 바꿔 끼울 자리로 정의하고 검사했지만, 원본 서버는 이 경로를 거치지 않고 사회자 LLM을 직접 불렀습니다. 교체 가능한 계약이 운영에 쓰였다고 주장하지 않습니다.
- CLI 참여자는 로그인된 사용자 권한으로 디스크를 읽습니다(Codex는 `--sandbox read-only`). 원본의 보안 점검에서 이 항목은 미해결로 남았고, 이 저장소의 코드도 같습니다.
- 대화를 짧게 주고받는 "압축" 방식의 효과는 1회 측정뿐이라 수치를 싣지 않았습니다.

## 담당과 도구

문제 정의, 구조, 측정 방법, 판정 기준(decisions/002의 사전 규칙)은 본인이 정했고, 코드와 검사 작성에는 Claude Code와 Codex를 썼습니다. 이 저장소의 파일은 원본에서 그대로 복사했고, 위에 적은 검사 1개 제거 외에는 고치지 않았습니다. 라이선스는 MIT입니다.
