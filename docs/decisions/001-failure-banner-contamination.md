# 001. CLI 실패 배너가 참여자 발언으로 기록돼 분석 기록을 오염시켰다

2026-09-23. 중단 시점 판정 규칙을 만들려고 수집 실행 대화 8개의 사회자 판정 기록 21건을 분석하던 중 발견했다.

## 무엇이 잘못됐나

Codex 발언 10회 중 8회가 발언 자리에 Codex CLI의 실패 배너로 저장돼 있었다(대화 2개에 몰림).

```
[A] Reading additional input from stdin...
    OpenAI Codex v0.155.1
    workdir: (경로)
```

같은 배너가 반복되니 "참여자가 같은 말을 반복한다"는 신호(`repeat_ratio = 1.00`)가 나왔다. 이 기록으로 중단 규칙을 학습하면 틀린 신호를 배운다.

## 원인 두 가지

1. `bridge.py`가 CLI 프로세스의 stdin을 닫지 않았다. 배너 문구("Reading additional input from stdin...")로 보아 Codex가 추가 입력을 기다리다 끝난 것으로 추정한다(재현 시험은 하지 않았다).
2. `Codex.parse()`가 본문이 비면 `stderr`를 그대로 발언으로 돌려줬다(`return text or stderr.strip()`, 코드로 확인).

## 고친 것

- 모든 CLI 참여자가 `stdin=subprocess.DEVNULL`로 실행한다(`test_all_cli_members_close_stdin`).
- `stderr`는 발언으로 돌려주지 않는다. 본문이 비면 `stderr` 첫 줄을 담은 대괄호 오류 표시를 돌려주고(`is_error`로 구분), 서버는 이를 발언으로 기록하지 않는다. 서버 코드는 이 저장소에 없다(`test_codex_failure_does_not_become_speech`, `test_codex_real_output_still_returned`).
- 이미 쌓인 기록은 고칠 수 없어, 실패 메시지 뒤에 내려진 사회자 판정 기록 6건을 걸러 15건으로 분석했다(원본의 `is_contaminated()`).

## 한계

수치는 2026-09-23 시점 수집 실행 대화 8개 기준이며, 수정 뒤 기록의 오염률은 재측정하지 않았다. 걸러 낸 기록을 원래 내용으로 되살리지는 못했다.
