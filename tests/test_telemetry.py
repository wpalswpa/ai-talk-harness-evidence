"""계측·결과 기록(P0-b) 검사.

가장 중요한 두 가지를 본다.

1. **NULL 과 0 을 구분하는가.** 측정 못 한 것을 0 으로 적으면
   "토큰을 안 썼다"·"돈을 안 썼다"는 거짓이 된다. 합계가 거짓말을 한다.
2. **계측이 대화를 죽이지 않는가.** telemetry failure != conversation failure
"""

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "talk"))

import store  # noqa: E402
import telemetry  # noqa: E402

TMP = Path(__file__).resolve().parent / "_tmp_tel"
_n = [0]


def _close():
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
    _close()
    TMP.mkdir(parents=True, exist_ok=True)
    _n[0] += 1
    p = TMP / f"t{_n[0]}.db"
    _wipe(p)
    store.init(p)
    store.open_session("s1", "과제", 2, ["A", "B"], [])
    return p


# ── NULL vs 0 (§3) ──────────────────────────────────────────

def test_unmeasured_tokens_are_null_not_zero():
    """CLI 참여자는 토큰을 모른다. 0 으로 적으면 거짓이 된다."""
    c = telemetry.Call(stage="draft", vendor="codex")
    assert c.tokens_in is None
    assert c.token_provenance == telemetry.PROV_NONE


def test_subscription_cost_is_real_zero():
    """구독은 호출당 과금이 없다 — 이건 미측정이 아니라 실제 0이다."""
    c = telemetry.Call(stage="draft", vendor="codex")
    telemetry.price_call(c)
    assert c.cost_usd == 0.0, "구독 비용이 0 이 아니다"
    assert c.cost_provenance == telemetry.PROV_MEASURED


def test_unknown_vendor_cost_is_null():
    """단가를 모르는 공급자는 None 이어야 한다. 0 으로 적으면 지출이 숨는다."""
    c = telemetry.Call(stage="draft", vendor="처음보는벤더",
                       tokens_in=100, tokens_out=50)
    telemetry.price_call(c)
    assert c.cost_usd is None
    assert c.cost_provenance == telemetry.PROV_NONE


def test_null_and_zero_are_distinct_in_db():
    """DB 에서도 구분돼야 한다. 이게 무너지면 집계가 전부 거짓."""
    fresh_db()
    store.add_call("s1", 0, {"stage": "draft", "vendor": "codex",
                             "cost_usd": 0.0, "tokens_in": None})
    store.add_call("s1", 1, {"stage": "draft", "vendor": "x",
                             "cost_usd": None, "tokens_in": None})
    rows = store.calls_for("s1")
    assert rows[0]["cost_usd"] == 0.0, "실제 0 이 사라졌다"
    assert rows[1]["cost_usd"] is None, "미측정이 0 이 됐다"
    st = store.telemetry_stats()
    assert st["cost_zero"] == 1 and st["cost_null"] == 1


# ── provenance (§2) ─────────────────────────────────────────

def test_measured_beats_estimated():
    """공급자 usage 가 있으면 추정으로 덮어쓰지 않는다.

    실제 흐름과 같게 **호출 도중**에 stash 한다 — bridge 의 API 참여자가
    응답을 파싱하는 시점이 measure() 블록 안이기 때문이다.
    """
    with telemetry.measure("ledger", vendor="groq", prompt="x" * 400) as c:
        telemetry.stash_usage(120, 45, "groq")     # 공급자 응답 파싱 시점
    assert c.tokens_in == 120 and c.tokens_out == 45
    assert c.token_provenance == telemetry.PROV_MEASURED


def test_estimated_when_no_usage():
    with telemetry.measure("draft", vendor="codex", prompt="가" * 400) as c:
        pass
    telemetry.finish(c, "응답" * 50, is_error=False)
    assert c.token_provenance == telemetry.PROV_ESTIMATED
    assert c.tokens_in == 100, c.tokens_in


def test_usage_does_not_leak_between_calls():
    """앞 호출의 usage 가 다음 호출에 묻으면 안 된다.

    CLI 참여자는 usage 를 stash 하지 않으므로, 바로 앞 API 호출의 값이
    남아 있으면 CLI 호출이 남의 토큰을 자기 것으로 기록하게 된다.
    """
    with telemetry.measure("ledger", vendor="groq") as c1:
        telemetry.stash_usage(999, 999, "groq")
    with telemetry.measure("draft", vendor="codex") as c2:
        pass                                   # CLI 는 stash 하지 않는다
    assert c1.tokens_in == 999
    assert c2.tokens_in is None, f"이전 값이 샜다: {c2.tokens_in}"


# ── 지연·성공/실패 (§1) ─────────────────────────────────────

def test_latency_measured():
    with telemetry.measure("draft") as c:
        time.sleep(0.02)
    assert c.latency_ms is not None and c.latency_ms >= 15, c.latency_ms


def test_success_and_failure_classified():
    ok = telemetry.finish(telemetry.Call(stage="draft"), "정상 응답",
                          is_error=False)
    assert ok.ok is True and ok.error_kind == ""

    to = telemetry.finish(telemetry.Call(stage="draft"),
                          "[Codex timeout 발생]", is_error=True)
    assert to.ok is False and to.error_kind == "timeout" and to.timed_out

    pv = telemetry.finish(telemetry.Call(stage="draft"),
                          "[Groq 키 없음]", is_error=True)
    assert pv.ok is False and pv.error_kind == "provider"

    em = telemetry.finish(telemetry.Call(stage="draft"),
                          "[로컬 가 빈 응답을 보냈습니다]", is_error=True)
    assert em.error_kind == "empty"


def test_controller_stage_marked():
    """ledger 만 제어 비용이다. B0~B3 비교에서 따로 세야 한다."""
    assert telemetry.STAGE_LEDGER in telemetry.CONTROLLER_STAGES
    assert telemetry.STAGE_WRITE not in telemetry.CONTROLLER_STAGES


def test_all_stages_distinct():
    stages = [telemetry.STAGE_RUBRIC, telemetry.STAGE_DRAFT,
              telemetry.STAGE_LEDGER, telemetry.STAGE_DISCUSS,
              telemetry.STAGE_WRITE, telemetry.STAGE_GRADE,
              telemetry.STAGE_REPAIR, telemetry.STAGE_REGRADE]
    assert len(set(stages)) == 8


# ── 계측 실패가 대화를 안 죽인다 (§8) ───────────────────────

def test_measure_does_not_swallow_real_errors():
    """ask() 는 예외를 안 던지므로, 여기서 삼키면 진짜 버그가 숨는다."""
    try:
        with telemetry.measure("draft"):
            raise ValueError("진짜 버그")
        assert False, "예외가 삼켜졌다"
    except ValueError:
        pass


def test_add_call_failure_returns_none():
    fresh_db()
    assert store.add_call("없는세션", 0, {"stage": "draft"}) is None


def test_set_outcome_failure_returns_false():
    fresh_db()
    assert store.set_outcome("없는세션", turns=1) is False


def test_stash_usage_never_raises():
    telemetry.stash_usage(None, None)
    telemetry.stash_usage("이상한값", object())     # type: ignore[arg-type]


# ── 연결 (§5) ───────────────────────────────────────────────

def test_call_links_to_decision():
    fresh_db()
    did = store.add_decision("s1", 0, 0, task="과제",
                             parsed={"next": "A", "ok": True})
    store.add_call("s1", 0, {"stage": "ledger"}, decision_id=did,
                   is_controller=True)
    row = store.calls_for("s1")[0]
    assert row["decision_id"] == did
    assert row["is_controller"] == 1


def test_outcome_separates_session_from_decision():
    """§4 — session outcome 과 decision reward 는 다른 테이블이다."""
    fresh_db()
    store.set_outcome("s1", turns=4, completed=1,
                      grade_pre_pass=2, grade_pre_total=6)
    o = store.outcome_for("s1")
    assert o["turns"] == 4 and o["grade_pre_pass"] == 2
    # decisions 에는 reward 칼럼이 없어야 한다 — 섞으면 안 된다
    cols = [r[1] for r in store.db().execute("PRAGMA table_info(decisions)")]
    assert not any("reward" in c or "outcome" in c for c in cols), cols


def test_outcome_null_when_not_graded():
    """채점 안 한 세션의 점수는 0 이 아니라 NULL 이다."""
    fresh_db()
    store.set_outcome("s1", turns=2, completed=1)
    o = store.outcome_for("s1")
    assert o["grade_pre_pass"] is None, "채점 안 했는데 0 이 들어갔다"


def test_outcome_is_upsert():
    fresh_db()
    store.set_outcome("s1", turns=1)
    store.set_outcome("s1", turns=5, completed=1)
    assert store.outcome_for("s1")["turns"] == 5


# ── fallback 분류 (§7) ──────────────────────────────────────

def test_alias_resolvable_vs_true_invalid():
    """'Codex' 는 A 로 풀린다. 'Zzz' 는 진짜 잘못이다."""
    fresh_db()
    import orchestrator
    for i, raw in enumerate(('{"done":false,"looping":false,"next":"Codex",'
                             '"instruction":"x"}',
                             '{"done":false,"looping":false,"next":"Zzz",'
                             '"instruction":"x"}')):
        store.add_decision("s1", i, i, task="과제",
                           parsed=orchestrator.parse_ledger(raw, ["A", "B"]))
    st = store.fallback_stats({"Codex": "A", "Claude": "B"})
    assert st["legacy_fallback"] == 2
    assert st["alias_resolvable"] == 1
    assert st["true_invalid"] == 1


def test_fallback_stats_without_alias_map():
    fresh_db()
    assert store.fallback_stats()["legacy_fallback"] == 0


# ── 마이그레이션 (§9) ───────────────────────────────────────

def test_migration_adds_tables_without_loss():
    _close()
    TMP.mkdir(parents=True, exist_ok=True)
    _n[0] += 1
    p = TMP / f"old{_n[0]}.db"
    _wipe(p)
    import sqlite3
    con = sqlite3.connect(p)
    con.executescript("""
      CREATE TABLE sessions(id TEXT PRIMARY KEY, created_at TEXT,
        updated_at TEXT, task TEXT, rounds INTEGER, active TEXT, files TEXT,
        deliverable TEXT, status TEXT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT, who TEXT, name TEXT, model TEXT, color TEXT,
        kind TEXT, text TEXT, created_at TEXT);
    """)
    con.execute("INSERT INTO sessions VALUES('old','t','t','옛 과제',1,"
                "'[]','[]','','done')")
    con.commit(); con.close()

    store.init(p)
    names = {r[0] for r in store.db().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("decisions", "model_calls", "session_outcomes"):
        assert t in names, f"{t} 가 안 생겼다"
    assert store.db().execute(
        "SELECT task FROM sessions WHERE id='old'").fetchone()[0] == "옛 과제"


def test_delete_removes_telemetry():
    fresh_db()
    store.add_call("s1", 0, {"stage": "draft"})
    store.set_outcome("s1", turns=1)
    store.delete("s1")
    assert store.calls_for("s1") == []
    assert store.outcome_for("s1") is None


# ── 동시 실행 안전성 (실측 버그) ────────────────────────────

def test_init_does_not_interrupt_recent_running_session():
    """서버와 수집 스크립트를 같이 돌리면 서로의 대화를 죽이던 버그.

    store.init() 이 running 을 전부 interrupted 로 바꿨는데,
    "이 프로세스가 유일한 실행자"라는 전제가 깨져 있었다.
    """
    p = fresh_db()
    store.open_session("live", "진행 중인 대화", 2, ["A", "B"], [])
    assert store.db().execute(
        "SELECT status FROM sessions WHERE id='live'").fetchone()[0] == "running"

    _close()
    store.init(p)               # 다른 프로세스가 시작한 셈
    st = store.db().execute(
        "SELECT status FROM sessions WHERE id='live'").fetchone()[0]
    assert st == "running", f"진행 중인 대화가 {st} 로 바뀌었다"


def test_init_still_cleans_stale_running():
    """오래된 running 은 여전히 정리돼야 한다 — 서버가 죽은 경우."""
    p = fresh_db()
    con = store.db()
    con.execute("INSERT INTO sessions(id,created_at,updated_at,task,rounds,"
                "active,files,deliverable,status) VALUES(?,?,?,?,?,?,?,?,?)",
                ("stale", "2020-01-01T00:00:00+00:00",
                 "2020-01-01T00:00:00+00:00", "옛 대화", 1, "[]", "[]", "",
                 "running"))
    con.commit()
    _close()
    store.init(p)
    st = store.db().execute(
        "SELECT status FROM sessions WHERE id='stale'").fetchone()[0]
    assert st == "interrupted", f"죽은 세션이 {st} 로 남았다"


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
