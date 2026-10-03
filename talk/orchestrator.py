"""중앙집중 오케스트레이터 — 진행 원장으로 대화를 통제한다.

왜 라운드로빈이 아닌가. 260개 구성을 비교한 연구(arXiv:2512.08296)에서
토폴로지가 성능을 +81%~−70% 좌우했고, **오류 증폭이 독립형 17.2배 대
중앙집중형 4.4배**로 4배 차이 났다. 업계도 같은 방향으로 수렴했다 —
LangGraph는 supervisor 패키지를 접고 서브에이전트로, CrewAI는 위임을
기본 비활성화로 바꿨다.

진행 원장은 Magentic-One(arXiv:2411.04468)에서 필요한 부분만 가져왔다.
매 턴 뒤 네 가지를 판정한다: 끝났나 / 맴도나 / 다음은 누구 / 무엇을 시킬까.
전체 구조는 MAST 측정에서 실패율 78.6%였으므로 그대로 쓰지 않는다.
"""

from __future__ import annotations

import json


def parse_json(text: str) -> dict | None:
    """모델이 JSON 주변에 군말을 붙여도 건진다."""
    if not text:
        return None
    t = text.strip()
    if "```" in t:                       # 코드펜스 제거
        parts = t.split("```")
        if len(parts) > 1:
            t = parts[1]
            if t.lstrip().lower().startswith("json"):
                t = t.lstrip()[4:]
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            d = json.loads(t[i:j + 1])
            return d if isinstance(d, dict) else None
        except json.JSONDecodeError:
            return None
    return None


LEDGER_SYS ="""너는 대화를 관리하는 사회자다. 직접 의견을 내지 않는다.

지금까지의 의논을 보고 다음을 판정하라.

- done: 과제에 답할 내용이 충분히 나왔으면 true. 더 논의해도 같은 말이
  반복될 것 같아도 true.
- looping: 최근 발언들이 서로 같은 말을 다시 하고 있으면 true.
- next: 다음에 말할 사람의 키. 아직 안 나온 관점을 가진 사람을 골라라.
- instruction: 그 사람에게 시킬 일을 한 문장으로. 무엇을 다뤄야 하는지
  구체적으로. "의견을 말해라" 같은 막연한 지시는 금지.

JSON만 출력한다."""

LEDGER_SCHEMA = {
    "type": "object",
    "properties": {
        "done": {"type": "boolean"},
        "looping": {"type": "boolean"},
        "next": {"type": "string"},
        "instruction": {"type": "string"},
    },
    "required": ["done", "looping", "next", "instruction"],
    "additionalProperties": False,
}


def ledger_prompt(task: str, transcript: list[tuple[str, str]],
                  members: dict, candidates: list[str]) -> str:
    """사회자가 보는 것. 발언 내용과 후보만 준다."""
    lines = [f"[과제] {task}", "", "[지금까지의 의논]"]
    if not transcript:
        lines.append("(아직 없음)")
    for who, text in transcript[-8:]:      # 최근 8개만. 길면 중간이 묻힌다
        name = "사용자" if who == "USER" else members[who].name
        lines.append(f"{name}: {text[:200]}")
    lines += ["", "[다음 발언 후보]"]
    for k in candidates:
        m = members[k]
        lines.append(f"  {k} = {m.name} ({m.persona[:40]}…)")
    lines += ["", "판정하라."]
    return "\n".join(lines)


#: parse_ledger 가 붙이는 이상 징후 코드. 대화 동작에는 영향을 주지 않고
#: 기록에만 쓴다. 나중에 teacher 판정의 품질을 세는 기준이 된다.
#:   controller_error  사회자 호출 자체가 실패(오류 문자열 반환)
#:   parse_failed      JSON 을 못 읽음
#:   invalid_next      존재하지 않는 참여자를 지목
#:   empty_instruction 지시가 비어 있음
#:   no_candidates     고를 후보가 없음
FLAG_CONTROLLER_ERROR = "controller_error"
FLAG_PARSE_FAILED = "parse_failed"
FLAG_INVALID_NEXT = "invalid_next"
FLAG_EMPTY_INSTRUCTION = "empty_instruction"
FLAG_NO_CANDIDATES = "no_candidates"
#: 표시 이름을 키로 풀어서 살린 경우(B2N). invalid_next 와 달리
#: **사회자 의도가 보존된다** — 폴백이 아니라 복구다.
FLAG_ALIAS_RESOLVED = "alias_resolved"


def parse_ledger(text: str, candidates: list[str],
                 alias: dict[str, str] | None = None) -> dict:
    """원장 판정을 읽는다. 망가졌으면 안전한 기본값으로 계속 간다.

    사회자가 실패해도 대화가 멈추면 안 된다.

    반환값에 `flags`·`raw_next` 를 덧붙이지만 **기존 키의 의미와 값은
    그대로다.** 호출부는 예전처럼 done/looping/next/instruction 만 봐도
    되고, 새 키는 기록용이다. 동작을 바꾸지 않기 위해 판정 순서와
    폴백 규칙은 한 글자도 건드리지 않았다.
    """
    flags: list[str] = []

    # 사회자 호출이 실패하면 ask() 가 "[...]" 오류 문자열을 준다.
    # 그대로 parse_json 에 넣으면 parse_failed 로만 보여 원인이 묻힌다.
    from bridge import is_error
    if is_error(text or ""):
        flags.append(FLAG_CONTROLLER_ERROR)

    d = parse_json(text) or {}
    if not d:
        flags.append(FLAG_PARSE_FAILED)

    raw_next = d.get("next", "")
    nxt = raw_next
    if nxt not in candidates:
        # alias 를 주면(B2N) 표시 이름을 키로 푼다. 안 주면(B2) 예전
        # 그대로 candidates[0] 으로 떨어진다 — 기본값이 None 인 이유다.
        # 실측: Groq 이 "A" 대신 "Codex" 를 절반 가까이 반환했고,
        # 그때마다 사회자 의도가 조용히 버려지고 있었다.
        resolved = ""
        if alias and raw_next:
            cand = alias.get(str(raw_next).strip().lower(), "")
            if cand in candidates:
                resolved = cand
        if resolved:
            nxt = resolved
            flags.append(FLAG_ALIAS_RESOLVED)
        else:
            if raw_next:
                flags.append(FLAG_INVALID_NEXT)
            nxt = candidates[0] if candidates else ""
            if not candidates:
                flags.append(FLAG_NO_CANDIDATES)

    instruction = str(d.get("instruction") or "").strip()
    if not instruction:
        flags.append(FLAG_EMPTY_INSTRUCTION)

    return {
        "done": bool(d.get("done")),
        "looping": bool(d.get("looping")),
        "next": nxt,
        "instruction": instruction,
        "ok": bool(d),          # 판정 자체가 됐는지
        # ── 아래는 기록 전용. 대화 로직은 보지 않는다 ──
        "raw_next": str(raw_next or ""),
        "flags": flags,
        # alias_resolved 는 폴백이 아니라 **복구**다 — 사회자 의도가
        # 보존됐으므로 "판정이 버려진 횟수"에 넣으면 안 된다.
        "fallback": any(f != FLAG_ALIAS_RESOLVED for f in flags),
        "alias_resolved": FLAG_ALIAS_RESOLVED in flags,
    }
