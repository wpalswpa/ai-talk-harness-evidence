"""오케스트레이션 정책 인터페이스 — B2/B2N/B3/B4(JEV)가 같은 계약을 쓴다.

## 왜 인터페이스부터인가

JEV(학습 정책)를 만들기 전에, **교체 가능한 자리**를 먼저 만든다. 지금
`ask_ledger()` 안에 LLM 호출이 박혀 있어서 다른 정책을 끼울 자리가 없다.

여기서 정의하는 것은 계약뿐이다. 실제 신경망·학습·분류기는 만들지
않는다 — 측정 전에 모델부터 만들지 않는다는 원칙 때문이다.

## 핵심 설계: 생성이 아니라 분류

현재 LLM 사회자는 자유 텍스트 instruction을 만든다. 작은 정책 모델이
임의의 자연어를 생성하게 하면 문제가 급격히 어려워진다. 그래서:

    policy → (next_agent, instruction_class)
           ↓
    deterministic template renderer
           ↓
    worker prompt

이 구조면 JEV는 **생성 모델이 아니라 작은 분류기**가 될 수 있다.

## instruction_class는 데이터에서 나와야 한다

아래 목록은 **잠정**이다. 실제 trace의 instruction을 분석해
bottom-up으로 확정해야 한다(P0-c). 지금 고정하면 데이터에 없는
taxonomy를 강제하게 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# ── 행동 ────────────────────────────────────────────────────

ACTION_ASK_AGENT = "ASK_AGENT"    # 누군가에게 발언시킨다
ACTION_STOP = "STOP"              # 의논을 끝낸다
ACTION_SYNTHESIZE = "SYNTHESIZE"  # 결과물로 넘어간다 (현재는 STOP과 동일 경로)
ACTION_REPAIR = "REPAIR"          # 수정 (현재는 Gate가 결정)

#: 잠정 instruction 유형. **trace 분석 전이므로 확정이 아니다.**
#: 현재 LEDGER_SYS가 요구하는 것("무엇을 다뤄야 하는지 구체적으로")에서
#: 나올 법한 것들을 적어 뒀을 뿐, 실제 분포는 측정해야 안다.
IC_PROPOSE = "propose"                  # 안을 내라
IC_CRITIQUE = "critique"                # 앞선 안의 약점을 짚어라
IC_VERIFY = "verify"                    # 근거를 확인하라
IC_COMPARE = "compare"                  # 두 안을 비교하라
IC_FILL_GAP = "fill_gap"                # 빠진 관점을 채워라
IC_SYNTHESIZE = "synthesize"            # 합의된 것을 정리하라
IC_CHALLENGE = "challenge_assumption"   # 전제를 의심하라
IC_NONE = ""

PROVISIONAL_CLASSES = (
    IC_PROPOSE, IC_CRITIQUE, IC_VERIFY, IC_COMPARE,
    IC_FILL_GAP, IC_SYNTHESIZE, IC_CHALLENGE,
)


@dataclass
class PolicyState:
    """정책이 보는 것의 전부.

    `decisions` 테이블의 state 칼럼과 1:1로 맞춘다 — 기록된 trace에서
    이 객체를 그대로 복원할 수 있어야 counterfactual replay가 가능하다.
    """

    task: str = ""
    transcript: list[tuple[str, str]] = field(default_factory=list)
    active: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    agent_info: dict[str, dict] = field(default_factory=dict)
    turn_index: int = 0
    round_no: int = 0
    max_rounds: int = 0
    #: 이 세션에서 이미 내린 결정들 (eff_next 목록). 같은 사람을
    #: 연달아 부르는지 같은 패턴을 정책이 볼 수 있게 한다.
    previous: list[str] = field(default_factory=list)

    @classmethod
    def from_decision_row(cls, row: dict) -> "PolicyState":
        """기록된 결정에서 state를 복원한다 — counterfactual replay용."""
        return cls(
            task=row.get("task") or "",
            transcript=[(c.get("who", ""), c.get("text", ""))
                        for c in (row.get("context") or [])],
            active=list(row.get("active") or []),
            candidates=list(row.get("candidates") or []),
            agent_info=dict(row.get("agent_info") or {}),
            turn_index=row.get("turn_index") or 0,
            round_no=row.get("round_no") or 0,
            max_rounds=row.get("max_rounds") or 0,
        )


@dataclass
class PolicyAction:
    """정책이 내는 것.

    기존 `parse_ledger()` 반환값으로 무손실 변환돼야 한다 — 그래야
    server.py를 고치지 않고 정책만 갈아끼울 수 있다.
    """

    action: str = ACTION_ASK_AGENT
    next_agent: str = ""
    instruction_class: str = IC_NONE
    confidence: float = 0.0
    reason_code: str = ""

    @property
    def done(self) -> bool:
        return self.action in (ACTION_STOP, ACTION_SYNTHESIZE)

    def to_ledger(self, instruction: str = "") -> dict:
        """기존 ledger 형식으로. server.py가 이 모양만 안다."""
        return {
            "done": self.done,
            "looping": False,
            "next": self.next_agent,
            "instruction": instruction,
            "ok": True,
            "raw_next": self.next_agent,
            "flags": [],
            "fallback": False,
        }


class Policy(Protocol):
    """B2·B2N·B3·B4가 모두 구현할 계약."""

    name: str

    def decide(self, state: PolicyState) -> PolicyAction:
        ...


# ── 별칭 정규화 (B2N) ───────────────────────────────────────

def build_alias_map(members: dict) -> dict[str, str]:
    """표시 이름 → 참여자 키. **MEMBERS에서 만든다. 하드코딩하지 않는다.**

    실측에서 사회자가 `"A"` 대신 `"Codex"`를 반환하는 일이 절반 가까이
    있었다. 이것은 LLM 추론 실패가 아니라 **직렬화 실패**다. 둘을
    분리해야 "LLM 사회자가 정말 필요한가"를 제대로 물을 수 있다.

    명확한 결정적 별칭만 허용한다 — 의미 유사도 매칭으로 확장하지
    않는다. 그러면 정규화가 또 다른 추론이 되어 버린다.
    """
    out: dict[str, str] = {}
    for key, m in members.items():
        out[key.lower()] = key
        name = getattr(m, "name", "") or ""
        cli = getattr(m, "cli", "") or ""
        for alias in (name, cli):
            if alias:
                out[alias.strip().lower()] = key
    return out


def normalize_next(raw: str, candidates: list[str],
                   alias: dict[str, str]) -> tuple[str, bool]:
    """사회자가 말한 것을 참여자 키로 푼다.

    반환: (키, 별칭으로 풀렸는지). 못 풀면 ("", False) — 폴백 판단은
    호출부가 한다. 여기서 candidates[0]으로 떨어뜨리지 않는다.
    """
    s = (raw or "").strip()
    if not s:
        return "", False
    if s in candidates:
        return s, False
    k = alias.get(s.lower())
    if k and k in candidates:
        return k, True
    return "", False
