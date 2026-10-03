"""결정 기록(P0-a) 검사.

이 기록의 목적은 화면 표시가 아니라 **나중에 state→action 데이터셋을
복원하는 것**이다. 그래서 검사하는 것도 "행이 생겼나"가 아니라
"복원에 필요한 것이 다 남았나"와 "기록이 대화를 죽이지 않나"다.

지금 남는 것은 학습 데이터가 아니라 raw decision trace다 —
결과(reward)와 연결되기 전에는 그렇게 부르지 않는다.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "talk"))

import orchestrator  # noqa: E402
import store  # noqa: E402

TMP = Path(__file__).resolve().parent / "_tmp_dec"


_n = [0]


def _close():
    """열린 연결을 닫는다. Windows 는 열린 파일을 못 지운다."""
    con = getattr(store._local, "con", None)
    if con is not None:
        try:
            con.close()
        except Exception:
            pass
        store._local.__dict__.pop("con", None)



def _wipe(p: Path) -> None:
    """이전 실행이 남긴 같은 이름의 DB 를 지운다 — 남아 있으면 행이 충돌한다."""
    for suf in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(p) + suf).unlink()
        except FileNotFoundError:
            pass


def fresh_db() -> Path:
    """검사마다 새 DB 파일. 지우는 대신 새 이름을 쓴다 —
    WAL 파일까지 잡혀 있어 삭제가 실패하는 환경이 있다."""
    _close()
    TMP.mkdir(parents=True, exist_ok=True)
    _n[0] += 1
    p = TMP / f"s{_n[0]}.db"
    _wipe(p)
    store.init(p)
    store.open_session("s1", "테스트 과제", 2, ["A", "B"], [])
    return p


CANDS = ["A", "B"]


def _log(raw: str, parsed: dict, **kw):
    return store.add_decision(
        "s1", kw.pop("seq", 0), kw.pop("turn_index", 0),
        task="테스트 과제", context=[{"who": "A", "text": "안녕"}],
        active=CANDS, candidates=CANDS,
        agent_info={"A": {"name": "Codex", "model": "", "vendor": "codex"}},
        round_no=1, max_rounds=2, controller="E",
        raw_response=raw, parsed=parsed, **kw)


# ── parse_ledger: 기존 동작 보존 + 새 진단 ──────────────────

def test_parse_keeps_existing_contract():
    """기존 키의 의미와 값이 그대로여야 한다. 동작 변경 금지."""
    raw = json.dumps({"done": False, "looping": False,
                      "next": "B", "instruction": "근거를 대라"})
    d = orchestrator.parse_ledger(raw, CANDS)
    assert d["done"] is False and d["looping"] is False
    assert d["next"] == "B"
    assert d["instruction"] == "근거를 대라"
    assert d["ok"] is True


def test_normal_parse_has_no_flags():
    raw = json.dumps({"done": False, "looping": False,
                      "next": "B", "instruction": "근거를 대라"})
    d = orchestrator.parse_ledger(raw, CANDS)
    assert d["flags"] == [], d["flags"]
    assert d["fallback"] is False


def test_broken_json_falls_back_and_is_flagged():
    """깨진 JSON — 기존 폴백 그대로, 사실만 추가로 기록."""
    d = orchestrator.parse_ledger("이건 JSON이 아니다", CANDS)
    assert d["next"] == "A", "기존 폴백(candidates[0])이 깨졌다"
    assert d["done"] is False and d["ok"] is False
    assert orchestrator.FLAG_PARSE_FAILED in d["flags"]
    assert d["fallback"] is True


def test_invalid_next_records_raw_and_effective():
    """없는 참여자를 지목 — raw 와 실제 행동이 다르게 남아야 한다."""
    raw = json.dumps({"done": False, "looping": False,
                      "next": "Z", "instruction": "해봐"})
    d = orchestrator.parse_ledger(raw, CANDS)
    assert d["raw_next"] == "Z", "사회자 원래 의도가 사라졌다"
    assert d["next"] == "A", "폴백이 안 걸렸다"
    assert d["raw_next"] != d["next"], "raw 와 effective 가 구분되지 않는다"
    assert orchestrator.FLAG_INVALID_NEXT in d["flags"]


def test_controller_error_is_distinguished_from_parse_failure():
    """사회자 호출 실패와 JSON 실패는 다른 원인이다."""
    d = orchestrator.parse_ledger("[Codex 를 찾을 수 없습니다]", CANDS)
    assert orchestrator.FLAG_CONTROLLER_ERROR in d["flags"]
    assert d["next"] == "A", "폴백이 안 걸렸다"


def test_empty_instruction_flagged():
    raw = json.dumps({"done": False, "looping": False, "next": "B",
                      "instruction": "   "})
    d = orchestrator.parse_ledger(raw, CANDS)
    assert d["instruction"] == ""
    assert orchestrator.FLAG_EMPTY_INSTRUCTION in d["flags"]


def test_no_candidates_does_not_crash():
    d = orchestrator.parse_ledger('{"done":false,"next":"A"}', [])
    assert d["next"] == ""
    assert orchestrator.FLAG_NO_CANDIDATES in d["flags"]


def test_behavior_identical_to_pre_change():
    """변경 전 구현과 기존 4개 키가 완전히 같아야 한다.

    P0-a 는 관측성만 추가한다. 판정값이 한 글자라도 달라지면
    "동작 변경 없음"이 거짓이 된다. 실패 경로까지 전부 대조한다.
    """
    from orchestrator import parse_json

    def old(text, candidates):
        d = parse_json(text) or {}
        nxt = d.get("next", "")
        if nxt not in candidates:
            nxt = candidates[0] if candidates else ""
        return {"done": bool(d.get("done")), "looping": bool(d.get("looping")),
                "next": nxt,
                "instruction": str(d.get("instruction") or "").strip(),
                "ok": bool(d)}

    cases = [
        ('{"done":false,"looping":false,"next":"B","instruction":"x"}', CANDS),
        ('{"done":true,"looping":false,"next":"A","instruction":"끝"}', CANDS),
        ('{"done":false,"looping":true,"next":"Z","instruction":"y"}', CANDS),
        ("깨진 JSON", CANDS),
        ("[Codex 를 찾을 수 없습니다]", CANDS),
        ('{"next":"A"}', []),
        ('```json\n{"done":false,"next":"B","looping":false,'
         '"instruction":" 공백 "}\n```', CANDS),
        ("", CANDS),
    ]
    for raw, cands in cases:
        o, n = old(raw, cands), orchestrator.parse_ledger(raw, cands)
        for k in ("done", "looping", "next", "instruction", "ok"):
            assert o[k] == n[k], f"{raw[:24]!r} 의 {k}: {o[k]!r} → {n[k]!r}"


# ── 저장 ────────────────────────────────────────────────────

def test_normal_path_writes_exactly_one_row():
    fresh_db()
    raw = json.dumps({"done": False, "looping": False,
                      "next": "B", "instruction": "근거를 대라"})
    _log(raw, orchestrator.parse_ledger(raw, CANDS))
    rows = store.decisions_for("s1")
    assert len(rows) == 1, f"{len(rows)}행"
    r = rows[0]
    assert r["parse_ok"] == 1 and r["fallback"] == 0
    assert r["eff_next"] == "B" and r["eff_instruction"] == "근거를 대라"


def test_state_is_recoverable_for_dataset():
    """state→action 을 복원하려면 이 필드들이 다 있어야 한다."""
    fresh_db()
    raw = json.dumps({"done": False, "looping": False,
                      "next": "B", "instruction": "근거를 대라"})
    _log(raw, orchestrator.parse_ledger(raw, CANDS))
    r = store.decisions_for("s1")[0]
    # state
    for k in ("task", "context", "active", "candidates", "agent_info",
              "round_no", "max_rounds", "controller"):
        assert r[k] not in (None, "", [], {}), f"state 에 {k} 가 비었다"
    # action
    for k in ("eff_done", "eff_looping", "eff_next", "eff_instruction"):
        assert k in r, f"action 에 {k} 가 없다"
    assert r["turn_index"] is not None and r["seq"] is not None


def test_raw_and_effective_both_persisted():
    """둘을 같은 것으로 취급하지 않는다."""
    fresh_db()
    raw = json.dumps({"done": False, "looping": False,
                      "next": "Z", "instruction": "해봐"})
    _log(raw, orchestrator.parse_ledger(raw, CANDS))
    r = store.decisions_for("s1")[0]
    assert r["raw_next"] == "Z"
    assert r["eff_next"] == "A"
    assert "Z" in r["raw_response"], "사회자 원문이 안 남았다"
    assert r["fallback"] == 1
    assert orchestrator.FLAG_INVALID_NEXT in r["flags"]


def test_sequence_increments():
    fresh_db()
    raw = json.dumps({"done": False, "next": "B", "looping": False,
                      "instruction": "x"})
    p = orchestrator.parse_ledger(raw, CANDS)
    for i in range(3):
        _log(raw, p, seq=i, turn_index=i)
    assert [r["seq"] for r in store.decisions_for("s1")] == [0, 1, 2]


# ── 기록 실패가 대화를 죽이지 않는다 ────────────────────────

def test_logging_failure_returns_none_not_raise():
    """decision logging failure != conversation failure"""
    fresh_db()
    # 존재하지 않는 세션 → FK 위반. 예외가 새면 대화가 죽는다.
    got = store.add_decision("없는세션", 0, 0, task="x",
                             parsed={"next": "A"})
    assert got is None, "실패했는데 None 이 아니다"


def test_logging_failure_with_unserializable_input():
    fresh_db()
    got = store.add_decision("s1", 0, 0, task="x",
                             context=[object()],      # JSON 불가
                             parsed={"next": "A"})
    assert got is None


def test_add_decision_tolerates_old_parsed_shape():
    """flags/raw_next 가 없는 옛 형태도 받아야 한다."""
    fresh_db()
    got = store.add_decision("s1", 0, 0, task="x",
                             parsed={"done": True, "looping": False,
                                     "next": "A", "instruction": "i",
                                     "ok": True})
    assert got is not None
    r = store.decisions_for("s1")[0]
    assert r["raw_next"] == "" and r["flags"] == []


# ── 마이그레이션 ────────────────────────────────────────────

def test_migration_on_existing_db():
    """기존 DB를 지우지 않아도 새 테이블이 생겨야 한다."""
    _close()
    TMP.mkdir(parents=True, exist_ok=True)
    _n[0] += 1
    p = TMP / f"old{_n[0]}.db"
    _wipe(p)
    # decisions 없는 옛 DB를 만든다
    import sqlite3
    con = sqlite3.connect(p)
    con.executescript("""
      CREATE TABLE sessions(id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT,
        task TEXT, rounds INTEGER, active TEXT, files TEXT, deliverable TEXT,
        status TEXT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
        who TEXT, name TEXT, model TEXT, color TEXT, kind TEXT, text TEXT,
        created_at TEXT);
    """)
    con.execute("INSERT INTO sessions VALUES('old','t','t','옛 과제',1,'[]','[]','','done')")
    con.commit(); con.close()

    store.init(p)                      # 마이그레이션이 여기서 일어나야 한다
    names = {r[0] for r in store.db().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "decisions" in names, f"테이블이 안 생겼다: {names}"
    # 기존 데이터가 살아 있어야 한다
    assert store.db().execute(
        "SELECT task FROM sessions WHERE id='old'").fetchone()[0] == "옛 과제"


# ── 점검 도구 ───────────────────────────────────────────────

def test_stats_counts():
    fresh_db()
    good = json.dumps({"done": False, "looping": False, "next": "B",
                       "instruction": "x"})
    _log(good, orchestrator.parse_ledger(good, CANDS), seq=0)
    _log("깨진 것", orchestrator.parse_ledger("깨진 것", CANDS), seq=1)
    st = store.decision_stats()
    assert st["total"] == 2
    assert st["parse_ok"] == 1
    assert st["fallback"] == 1
    assert st["by_type"] == {"orchestration": 2}
    assert st["by_session"] == {"s1": 2}
    assert st["by_flag"].get(orchestrator.FLAG_PARSE_FAILED) == 1


def test_delete_removes_decisions():
    fresh_db()
    raw = json.dumps({"done": False, "looping": False, "next": "B",
                      "instruction": "x"})
    _log(raw, orchestrator.parse_ledger(raw, CANDS))
    store.delete("s1")
    assert store.decisions_for("s1") == []


# ── B2N: 별칭 정규화 ────────────────────────────────────────

def test_b2_legacy_unchanged_without_alias():
    """alias 를 안 주면 예전(B2) 동작 그대로여야 한다.

    역사적 baseline 을 한 글자도 바꾸지 않기 위한 검사다.
    """
    raw = json.dumps({"done": False, "looping": False,
                      "next": "Codex", "instruction": "해봐"})
    d = orchestrator.parse_ledger(raw, CANDS)          # alias 없음
    assert d["next"] == "A", "폴백이 바뀌었다"
    assert orchestrator.FLAG_INVALID_NEXT in d["flags"]
    assert d["fallback"] is True
    assert d.get("alias_resolved") is False


def test_b2n_resolves_display_name():
    """alias 를 주면 'Codex' 가 'A' 로 풀린다 — 사회자 의도 보존."""
    import policy
    alias = {"codex": "A", "claude": "B", "a": "A", "b": "B"}
    raw = json.dumps({"done": False, "looping": False,
                      "next": "Codex", "instruction": "해봐"})
    d = orchestrator.parse_ledger(raw, CANDS, alias=alias)
    assert d["next"] == "A"
    assert orchestrator.FLAG_ALIAS_RESOLVED in d["flags"]
    assert d["alias_resolved"] is True


def test_alias_resolution_is_not_counted_as_fallback():
    """복구는 폴백이 아니다. 섞으면 '판정이 버려진 횟수'가 거짓이 된다."""
    alias = {"codex": "A"}
    raw = json.dumps({"done": False, "looping": False,
                      "next": "Codex", "instruction": "x"})
    d = orchestrator.parse_ledger(raw, CANDS, alias=alias)
    assert d["fallback"] is False, "복구가 폴백으로 계산됐다"


def test_alias_does_not_rescue_truly_invalid():
    """없는 사람은 여전히 폴백이다. 정규화가 추론으로 번지면 안 된다."""
    alias = {"codex": "A"}
    raw = json.dumps({"done": False, "looping": False,
                      "next": "존재하지않음", "instruction": "x"})
    d = orchestrator.parse_ledger(raw, CANDS, alias=alias)
    assert d["next"] == "A"
    assert orchestrator.FLAG_INVALID_NEXT in d["flags"]
    assert d["fallback"] is True


def test_alias_respects_candidates():
    """별칭이 풀려도 이번 대화 참가자가 아니면 못 쓴다."""
    alias = {"gemini": "C"}
    raw = json.dumps({"done": False, "looping": False,
                      "next": "Gemini", "instruction": "x"})
    d = orchestrator.parse_ledger(raw, ["A", "B"], alias=alias)
    assert d["next"] == "A"
    assert orchestrator.FLAG_INVALID_NEXT in d["flags"]


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
    _close()
    TMP.exists() and shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'모두 통과' if not fails else f'{fails}건 실패'}")
    sys.exit(1 if fails else 0)
