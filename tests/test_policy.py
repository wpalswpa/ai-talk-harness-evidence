"""정책 인터페이스(B2/B2N/B3/B4 공통 계약) 검사.

여기서 검사하는 것은 **교체 가능성**이다. 정책을 갈아끼워도
server.py가 안 바뀌어야 하고, 기록된 trace에서 state를 복원할 수
있어야 counterfactual replay가 가능하다.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "talk"))

import policy  # noqa: E402


class _M:
    def __init__(self, name, cli):
        self.name, self.cli = name, cli


MEMBERS = {"A": _M("Codex", "codex"), "B": _M("Claude", "claude"),
           "C": _M("Gemini", "gemini-api"), "E": _M("Groq", "groq")}


# ── 별칭 정규화 (B2N) ───────────────────────────────────────

def test_alias_map_built_from_members_not_hardcoded():
    """MEMBERS에서 생성돼야 참여자를 늘려도 따라온다."""
    a = policy.build_alias_map(MEMBERS)
    assert a["codex"] == "A" and a["claude"] == "B"
    assert a["a"] == "A", "키 자신도 별칭이어야"
    # 참여자를 추가하면 자동으로 들어온다
    a2 = policy.build_alias_map({**MEMBERS, "Z": _M("새참여자", "newcli")})
    assert a2["새참여자"] == "Z"


def test_normalize_resolves_display_name():
    """실측 문제: 사회자가 'A' 대신 'Codex'를 반환한다."""
    a = policy.build_alias_map(MEMBERS)
    key, was_alias = policy.normalize_next("Codex", ["A", "B"], a)
    assert key == "A" and was_alias is True


def test_normalize_passes_through_valid_key():
    a = policy.build_alias_map(MEMBERS)
    key, was_alias = policy.normalize_next("B", ["A", "B"], a)
    assert key == "B" and was_alias is False, "이미 키면 별칭 해석이 아니다"


def test_normalize_is_case_insensitive():
    a = policy.build_alias_map(MEMBERS)
    assert policy.normalize_next("  codex ", ["A", "B"], a)[0] == "A"


def test_normalize_rejects_unknown_without_falling_back():
    """여기서 candidates[0]으로 떨어뜨리면 안 된다 — 폴백은 호출부 몫."""
    a = policy.build_alias_map(MEMBERS)
    assert policy.normalize_next("Zzz", ["A", "B"], a) == ("", False)
    assert policy.normalize_next("", ["A", "B"], a) == ("", False)


def test_normalize_respects_candidates():
    """별칭이 풀려도 후보가 아니면 못 쓴다."""
    a = policy.build_alias_map(MEMBERS)
    assert policy.normalize_next("Gemini", ["A", "B"], a) == ("", False)


# ── 상태 복원 (counterfactual replay 전제) ──────────────────

def test_state_restored_from_recorded_decision():
    row = {"task": "과제", "context": [{"who": "A", "text": "안"}],
           "active": ["A", "B"], "candidates": ["A", "B"],
           "agent_info": {"A": {"vendor": "codex"}},
           "turn_index": 2, "round_no": 2, "max_rounds": 3}
    s = policy.PolicyState.from_decision_row(row)
    assert s.task == "과제"
    assert s.transcript == [("A", "안")]
    assert s.active == ["A", "B"] and s.turn_index == 2
    assert s.max_rounds == 3


def test_state_survives_missing_fields():
    """깨진 행이 있어도 복원이 죽으면 안 된다."""
    s = policy.PolicyState.from_decision_row({})
    assert s.task == "" and s.transcript == [] and s.turn_index == 0


# ── 행동 → 기존 ledger 형식 무손실 변환 ─────────────────────

def test_action_converts_to_ledger_shape():
    """server.py는 이 모양만 안다. 정책을 갈아끼워도 안 바뀌어야."""
    a = policy.PolicyAction(action=policy.ACTION_ASK_AGENT, next_agent="B",
                            instruction_class=policy.IC_CRITIQUE)
    led = a.to_ledger("앞선 안의 약점을 짚어라")
    for k in ("done", "looping", "next", "instruction", "ok",
              "raw_next", "flags", "fallback"):
        assert k in led, f"{k} 누락 — parse_ledger 와 모양이 다르다"
    assert led["done"] is False and led["next"] == "B"


def test_stop_action_sets_done():
    a = policy.PolicyAction(action=policy.ACTION_STOP)
    assert a.done is True and a.to_ledger()["done"] is True


def test_synthesize_also_ends_discussion():
    a = policy.PolicyAction(action=policy.ACTION_SYNTHESIZE)
    assert a.done is True


def test_ask_agent_does_not_end():
    a = policy.PolicyAction(action=policy.ACTION_ASK_AGENT, next_agent="A")
    assert a.done is False


# ── 분류 설계 ───────────────────────────────────────────────

def test_instruction_classes_are_distinct():
    assert len(set(policy.PROVISIONAL_CLASSES)) == len(policy.PROVISIONAL_CLASSES)


def test_classes_marked_provisional():
    """데이터 분석 전이므로 확정이 아님이 문서에 남아야 한다."""
    assert "잠정" in policy.__doc__ or "잠정" in str(policy.PROVISIONAL_CLASSES.__doc__ or "")
    src = (ROOT / "talk" / "policy.py").read_text(encoding="utf-8")
    assert "확정이 아니다" in src, "잠정임을 코드에 남겨야 한다"


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
