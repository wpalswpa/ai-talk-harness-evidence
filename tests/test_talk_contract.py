"""단톡방 참여자 계약 검사 — 호출부와 구현부가 어긋나지 않는가.

실제로 터진 버그가 이것이다. `server.py`가 `ask(..., schema=...)`로
부르는데 `Member.ask()`에 `schema` 인자가 없어서, 화면에는
"대화가 끊겼습니다 (TypeError)" 한 줄만 나왔다.

파이썬은 이런 불일치를 **호출되는 순간에만** 잡는다. 참여자 5명 중
Groq만 `schema`를 받고 있었으므로, 사회자가 Groq으로 뽑히면 돌고
Codex로 뽑히면 죽었다 — 어떤 참여자가 뽑히느냐에 따라 달라지는
버그라 재현이 들쭉날쭉했다.

그래서 **실제로 호출하지 않고 시그니처만 대조**한다. 참여자를 추가할
때 이 검사가 먼저 잡는다.
"""

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "talk"))

import bridge  # noqa: E402

#: `server.py`·`orchestrator.py`가 실제로 넘기는 키워드 인자.
#: 여기 있는 것은 참여자 전원이 받아야 한다.
REQUIRED_KWARGS = ("timeout", "cancel", "long", "json_mode", "schema")


def test_every_member_accepts_required_kwargs():
    """참여자 전원이 호출부가 넘기는 인자를 받아야 한다."""
    missing = []
    for key, m in bridge.MEMBERS.items():
        params = inspect.signature(type(m).ask).parameters
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
        if has_var_kw:
            continue   # **kwargs 로 받으면 통과
        for kw in REQUIRED_KWARGS:
            if kw not in params:
                missing.append(f"{key}({type(m).__name__}).ask 에 {kw} 없음")
    assert not missing, "\n  ".join([""] + missing)


def test_ask_returns_string_contract():
    """ask()는 실패해도 예외가 아니라 문자열을 돌려준다는 계약.

    한 명이 죽어도 대화가 이어져야 하므로. 반환형 주석으로 확인한다
    (실제 호출은 CLI 설치·구독 상태에 따라 달라져 테스트에 부적합).
    """
    sig = inspect.signature(bridge.Member.ask)
    assert sig.return_annotation in (str, "str"), sig.return_annotation


def test_schema_is_optional_everywhere():
    """schema는 기본값이 있어야 한다 — 안 쓰는 호출부가 깨지면 안 된다."""
    for key, m in bridge.MEMBERS.items():
        params = inspect.signature(type(m).ask).parameters
        if "schema" not in params:
            continue
        p = params["schema"]
        assert p.default is not inspect.Parameter.empty, \
            f"{key}({type(m).__name__}) 의 schema 에 기본값이 없다"


# test_server_kwargs_are_covered는 원본의 server.py를 읽는 검사라 이 공개 저장소(서버 미포함)에서는 뺐다.

def test_members_are_registered_with_distinct_keys():
    keys = list(bridge.MEMBERS)
    assert len(keys) == len(set(keys))
    assert keys, "참여자가 하나도 없다"


# ── CLI 실패가 발언으로 둔갑하지 않는가 (실측 버그) ────────

def test_codex_failure_does_not_become_speech():
    """codex 가 실패하면 stderr 배너가 발언으로 기록되던 버그.

    실측: 수집 trace 에서 "Reading additional input from stdin...
    OpenAI Codex v0.155.1 workdir: ..." 이 세 턴 연속 참여자 발언으로
    남았고, 사회자가 그걸 두고 의논을 이어갔다.
    """
    codex = bridge.MEMBERS["A"]
    banner = ("Reading additional input from stdin...\n"
              "OpenAI Codex v0.155.1\n--------\nworkdir: C:\\work")
    out = codex.parse("", banner, 1)
    assert bridge.is_error(out), f"실패가 발언으로 통과했다: {out[:60]}"


def test_codex_real_output_still_returned():
    codex = bridge.MEMBERS["A"]
    out = codex.parse("\ncodex\n실제 답변입니다\ntokens used\n123", "", 0)
    assert "실제 답변입니다" in out
    assert not bridge.is_error(out)


def test_all_cli_members_close_stdin():
    """stdin 을 열어 두면 CLI 가 입력을 기다리다 timeout 된다."""
    src = (ROOT / "talk" / "bridge.py").read_text(encoding="utf-8")
    assert "stdin=subprocess.DEVNULL" in src, "stdin 을 닫지 않는다"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name}  →  {e}")
        except Exception as e:
            fails += 1
            print(f"  ERR   {name}  →  {type(e).__name__}: {e}")
    print(f"\n{'모두 통과' if not fails else f'{fails}건 실패'}")
    sys.exit(1 if fails else 0)
