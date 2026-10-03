# 001. 분석용 대화 기록의 38%가 CLI 실패 배너로 오염돼 있었다

2026-09-23. 중단 시점 판정 규칙을 만들려고 기록된 대화 21건을 분석하던 중 발견했다.

## 무엇이 잘못됐나

8건의 기록에서 참여자 발언 자리에 Codex CLI의 실패 배너가 들어 있었다.

```
[A] Reading additional input from stdin...
    OpenAI Codex v0.155.1
    workdir: (경로)
```

같은 배너가 반복되니 "참여자가 같은 말을 반복한다"는 신호(`repeat_ratio = 1.00`)가 나왔다. 이 기록으로 중단 규칙을 학습하면 틀린 신호를 배운다.

## 원인 두 가지

1. `bridge.py`가 CLI 프로세스의 stdin을 닫지 않아, Codex가 추가 입력을 기다리다 죽었다.
2. `Codex.parse()`가 본문이 비면 `stderr`를 그대로 발언으로 돌려줬다(`return text or stderr.strip()`).

## 고친 것

- 모든 CLI 참여자가 `stdin=subprocess.DEVNULL`로 실행한다(`test_all_cli_members_close_stdin`).
- `stderr`는 발언으로 돌려주지 않는다. 본문이 비면 빈 결과와 실패로 처리한다(`test_codex_failure_does_not_become_speech`, `test_codex_real_output_still_returned`).
- 이미 쌓인 기록은 고칠 수 없어 분석에서 걸러 냈다(원본의 `is_contaminated()`).

## 한계

38%는 그 시점 분석 대상 21건 기준이다. 걸러 낸 기록을 원래 내용으로 되살리지는 못했다.
