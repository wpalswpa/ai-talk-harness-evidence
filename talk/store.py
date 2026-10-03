"""세션 저장. SQLite 두 테이블.

LangGraph checkpointer는 이 용도에 과하다 — 그쪽은 그래프 상태를 msgpack
BLOB으로 저장해 재생하는 구조라 사람이 읽을 수 없다. 우리 참여자는 매번
전체 대화를 프롬프트로 다시 쌓는 무상태 호출이므로, 저장된 행 자체가
원천이 된다. OpenAI Agents SDK의 SQLiteSession 모양을 따랐다.

규칙 두 가지:
  - 스레드마다 연결을 따로 연다 (SQLite 객체는 스레드 간 공유 불가)
  - 정렬은 id(AUTOINCREMENT)로 한다. timestamp는 같은 값이 겹칠 수 있다
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  task        TEXT NOT NULL,
  rounds      INTEGER,
  active      TEXT,          -- JSON 배열: 참가자 키
  files       TEXT,          -- JSON 배열: 첨부 파일명
  deliverable TEXT,          -- 최종 결과물
  status      TEXT           -- running | done | stopped
);
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  who         TEXT,          -- A/B/C/D/E | USER | final
  name        TEXT,
  model       TEXT,
  color       TEXT,
  kind        TEXT,          -- say | interject | final
  text        TEXT,
  created_at  TEXT,
  FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_msg ON messages(session_id, id);

-- 오케스트레이션 결정 기록 (P0-a).
-- 목적은 화면 표시가 아니라 **나중에 state→action 데이터셋을 복원하는 것**이다.
-- 그래서 판정에 들어간 입력(state)과 실제로 시스템이 쓴 값(effective action)을
-- 둘 다 남긴다. 지금은 학습 데이터가 아니라 raw decision trace다 —
-- 결과(reward)와 연결되기 전에는 그렇게 부르지 않는다.
--
-- raw_* 와 eff_* 를 나눈 이유: teacher 모방학습에는 사회자 원문이 필요하고,
-- 실제 행동 재현에는 폴백까지 거친 값이 필요하다. 둘은 같지 않다.
CREATE TABLE IF NOT EXISTS decisions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT NOT NULL,
  seq           INTEGER,        -- 이 세션에서 몇 번째 결정인가 (0부터)
  turn_index    INTEGER,        -- 결정 시점의 발언 인덱스 i
  decision_type TEXT,           -- orchestration | participant_selection
                                -- | writer_selection | grader_selection
  created_at    TEXT,

  -- ── state: 판정이 본 것 ──
  task          TEXT,           -- 과제 원문
  context       TEXT,           -- JSON: 사회자에게 실제로 넘어간 transcript 요약
  active        TEXT,           -- JSON 배열: 이번 대화 참가자 키
  candidates    TEXT,           -- JSON 배열: 지목 가능한 후보 키
  agent_info    TEXT,           -- JSON: {키: {name, model, vendor}}
  round_no      INTEGER,        -- 현재 바퀴
  max_rounds    INTEGER,        -- 총 바퀴
  controller    TEXT,           -- 판정한 참여자 키

  -- ── teacher: 사회자가 뭐라 했나 ──
  raw_response  TEXT,           -- 원문 그대로
  raw_next      TEXT,           -- 파싱된 next (폴백 전)
  parse_ok      INTEGER,        -- 1=JSON 읽힘

  -- ── effective action: 시스템이 실제로 한 것 ──
  eff_done      INTEGER,
  eff_looping   INTEGER,
  eff_next      TEXT,
  eff_instruction TEXT,

  -- ── 이상 징후 ──
  fallback      INTEGER,        -- 1=폴백이 한 번이라도 발동
  flags         TEXT,           -- JSON 배열: parse_failed, invalid_next …

  FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dec ON decisions(session_id, seq);

-- 모델 호출 계측 (P0-b).
-- 단계별 지연·성공/실패·토큰·비용. **NULL 은 '측정 못 함'이고 0 과 다르다.**
-- CLI 참여자는 토큰을 알 수 없어 NULL 이고, 이것을 0 으로 적으면
-- "토큰을 안 썼다"는 거짓이 된다. 그래서 NOT NULL 을 걸지 않는다.
--
-- provenance 는 '어떻게 얻은 값인가'다: measured(공급자 usage) /
-- estimated(문자 수 추정) / none(모름). 나중에 이 숫자를 믿어도 되는지
-- 판단하는 근거가 된다.
CREATE TABLE IF NOT EXISTS model_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT NOT NULL,
  decision_id   INTEGER,        -- 이 호출이 어느 결정에 속하나 (§5 연결)
  seq           INTEGER,        -- 세션 내 호출 순서
  stage         TEXT,           -- rubric|draft|ledger|discuss|write|grade|repair|regrade
  is_controller INTEGER,        -- 1 = 결과물을 안 만드는 제어 비용
  created_at    TEXT,

  member_key    TEXT,
  member_name   TEXT,
  vendor        TEXT,
  model         TEXT,

  latency_ms    INTEGER,        -- NULL 가능
  ok            INTEGER,        -- 1/0, NULL=미완
  error_kind    TEXT,           -- timeout|provider|empty|''
  timed_out     INTEGER,

  tokens_in     INTEGER,        -- NULL = 측정 못 함
  tokens_out    INTEGER,
  cost_usd      REAL,           -- NULL = 측정 못 함, 0.0 = 실제 0(구독)
  token_provenance TEXT,
  cost_provenance  TEXT,
  chars_in      INTEGER,
  chars_out     INTEGER,

  FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_call ON model_calls(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_call_dec ON model_calls(decision_id);

-- 세션 결과 (P0-b).
-- **session-level outcome 과 decision-level reward 는 다르다(§4).**
-- 여기 있는 것은 세션 하나의 결과이고, 개별 결정이 좋았는지는
-- 여기서 직접 나오지 않는다. reward 설계는 P0-c 이후의 일이다.
CREATE TABLE IF NOT EXISTS session_outcomes (
  session_id    TEXT PRIMARY KEY,
  created_at    TEXT,
  variant       TEXT,           -- B0|B1|B2|B2N|B3  (P0-c 에서 채움)
  experiment_id TEXT,           -- 실험 묶음 (P0-c)
  task_id       TEXT,           -- 평가셋 과제 (P0-c)
  repetition    INTEGER,

  status        TEXT,           -- done|stopped|interrupted
  completed     INTEGER,        -- 1 = 결과물까지 나옴

  grade_pre_pass    INTEGER,    -- 수정 전 통과 수. NULL = 채점 안 함
  grade_pre_total   INTEGER,
  grade_post_pass   INTEGER,    -- 수정 후. NULL = 수정 안 함
  grade_post_total  INTEGER,
  repaired      INTEGER,        -- 1 = 수정 시도
  regressed     INTEGER,        -- 1 = 수정본이 더 나빠 원본 유지

  turns         INTEGER,        -- 의논 발언 수
  early_stopped INTEGER,        -- 1 = 사회자가 조기 종료
  wall_ms       INTEGER,

  FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""

_local = threading.local()
_path: Path | None = None


def init(db_path: Path) -> None:
    global _path
    _path = db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")   # 동시 읽기/쓰기
    con.executescript(SCHEMA)
    # 서버가 죽으면 진행 중이던 세션이 running 으로 영원히 남는다.
    # 시작할 때 정리하되, **최근 것은 건드리지 않는다.**
    #
    # 예전에는 running 을 전부 interrupted 로 바꿨다. "이 프로세스가
    # 유일한 실행자"라는 전제였는데, 수집 스크립트와 서버를 같이 돌리자
    # 그 전제가 깨졌다 — 진행 중이던 대화가 남의 시작 때문에 중단된
    # 것으로 표시됐다(실측). 계측 데이터는 살아남았지만 status 라벨이
    # 오염되면 outcome 집계가 거짓이 된다.
    #
    # 10분은 한 대화의 상한보다 넉넉하다(실측 벽시계 80초, 최대 바퀴에서도
    # 수 분). 그보다 오래 running 이면 정말로 죽은 것이다.
    #
    # 주의: updated_at 은 ISO('...T...+00:00')이고 sqlite datetime() 은
    # 공백 구분('... ...')이라 문자열 비교가 'T' 자리에서 어긋난다.
    # julianday() 로 둘 다 숫자로 바꿔 비교한다.
    con.execute(
        "UPDATE sessions SET status='interrupted' WHERE status='running' "
        "AND julianday(replace(substr(updated_at,1,19),'T',' ')) "
        "    < julianday('now', '-10 minutes')")
    con.commit()
    con.close()


def db() -> sqlite3.Connection:
    """이 스레드 전용 연결. SSE 생성기와 POST 핸들러가 다른 스레드다."""
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        _local.con = con
    return con


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── 쓰기 ────────────────────────────────────────────────────

def open_session(sid: str, task: str, rounds: int,
                 active: list[str], files: list[str]) -> None:
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO sessions"
        "(id,created_at,updated_at,task,rounds,active,files,deliverable,status)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, now(), now(), task, rounds,
         json.dumps(active), json.dumps(files, ensure_ascii=False), "", "running"))
    con.commit()


def add(sid: str, who: str, name: str, model: str,
        color: str, kind: str, text: str) -> None:
    con = db()
    con.execute(
        "INSERT INTO messages(session_id,who,name,model,color,kind,text,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (sid, who, name, model, color, kind, text, now()))
    con.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), sid))
    con.commit()


def close_session(sid: str, status: str, deliverable: str = "") -> None:
    con = db()
    con.execute("UPDATE sessions SET status=?, deliverable=?, updated_at=? WHERE id=?",
                (status, deliverable, now(), sid))
    con.commit()


def close_if_running(sid: str) -> None:
    """아직 running 이면 stopped 로 닫는다. 이미 닫혔으면 건드리지 않는다."""
    con = db()
    con.execute("UPDATE sessions SET status='stopped', updated_at=? "
                "WHERE id=? AND status='running'", (now(), sid))
    con.commit()


def delete(sid: str) -> bool:
    con = db()
    cur = con.execute("DELETE FROM sessions WHERE id=?", (sid,))
    con.execute("DELETE FROM messages WHERE session_id=?", (sid,))
    con.execute("DELETE FROM decisions WHERE session_id=?", (sid,))
    con.execute("DELETE FROM model_calls WHERE session_id=?", (sid,))
    con.execute("DELETE FROM session_outcomes WHERE session_id=?", (sid,))
    con.commit()
    return cur.rowcount > 0


# ── 결정 기록 (P0-a) ────────────────────────────────────────

def add_decision(sid: str, seq: int, turn_index: int, *,
                 decision_type: str = "orchestration",
                 task: str = "", context: list | None = None,
                 active: list[str] | None = None,
                 candidates: list[str] | None = None,
                 agent_info: dict | None = None,
                 round_no: int = 0, max_rounds: int = 0,
                 controller: str = "", raw_response: str = "",
                 parsed: dict | None = None) -> int | None:
    """결정 한 건을 남긴다. **실패해도 예외를 던지지 않는다.**

    기록이 대화를 죽이면 안 된다(`decision logging failure !=
    conversation failure`). 그래서 모든 예외를 삼키고 None 을 돌려준다.
    호출부는 반환값을 확인할 필요가 없다.

    parsed 는 parse_ledger() 의 반환값을 그대로 받는다. raw_next/flags 가
    없어도(옛 형태) 동작하도록 get 으로만 읽는다.
    """
    p = parsed or {}
    try:
        con = db()
        cur = con.execute(
            "INSERT INTO decisions(session_id,seq,turn_index,decision_type,"
            "created_at,task,context,active,candidates,agent_info,round_no,"
            "max_rounds,controller,raw_response,raw_next,parse_ok,eff_done,"
            "eff_looping,eff_next,eff_instruction,fallback,flags)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, seq, turn_index, decision_type, now(), task,
             json.dumps(context or [], ensure_ascii=False),
             json.dumps(active or [], ensure_ascii=False),
             json.dumps(candidates or [], ensure_ascii=False),
             json.dumps(agent_info or {}, ensure_ascii=False),
             round_no, max_rounds, controller,
             raw_response, str(p.get("raw_next") or ""),
             1 if p.get("ok") else 0,
             1 if p.get("done") else 0,
             1 if p.get("looping") else 0,
             str(p.get("next") or ""), str(p.get("instruction") or ""),
             1 if p.get("fallback") else 0,
             json.dumps(p.get("flags") or [], ensure_ascii=False)))
        con.commit()
        return cur.lastrowid
    except Exception:
        return None


def decisions_for(sid: str) -> list[dict]:
    rows = db().execute(
        "SELECT * FROM decisions WHERE session_id=? ORDER BY seq", (sid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("context", "active", "candidates", "agent_info", "flags"):
            try:
                d[k] = json.loads(d[k] or "[]")
            except (TypeError, ValueError):
                d[k] = []
        out.append(d)
    return out


def decision_stats() -> dict:
    """개발자용 점검. 총 수·정상 파싱·폴백·유형별·세션별."""
    con = db()
    one = lambda q: con.execute(q).fetchone()[0]
    return {
        "total": one("SELECT COUNT(*) FROM decisions"),
        "parse_ok": one("SELECT COUNT(*) FROM decisions WHERE parse_ok=1"),
        "fallback": one("SELECT COUNT(*) FROM decisions WHERE fallback=1"),
        "by_type": dict(con.execute(
            "SELECT decision_type,COUNT(*) FROM decisions "
            "GROUP BY decision_type").fetchall()),
        "by_session": dict(con.execute(
            "SELECT session_id,COUNT(*) FROM decisions "
            "GROUP BY session_id ORDER BY COUNT(*) DESC").fetchall()),
        "by_flag": _flag_counts(con),
    }


# ── 계측 (P0-b) ─────────────────────────────────────────────

def add_call(sid: str, seq: int, call_row: dict, *,
             decision_id: int | None = None,
             is_controller: bool = False) -> int | None:
    """모델 호출 계측 한 건. **실패해도 예외를 던지지 않는다.**

    계측이 대화를 죽이면 안 된다 — P0-a 의 add_decision 과 같은 원칙이다.
    """
    try:
        con = db()
        cur = con.execute(
            "INSERT INTO model_calls(session_id,decision_id,seq,stage,"
            "is_controller,created_at,member_key,member_name,vendor,model,"
            "latency_ms,ok,error_kind,timed_out,tokens_in,tokens_out,cost_usd,"
            "token_provenance,cost_provenance,chars_in,chars_out)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, decision_id, seq, call_row.get("stage"),
             1 if is_controller else 0, now(),
             call_row.get("member_key"), call_row.get("member_name"),
             call_row.get("vendor"), call_row.get("model"),
             call_row.get("latency_ms"), call_row.get("ok"),
             call_row.get("error_kind"), call_row.get("timed_out"),
             call_row.get("tokens_in"), call_row.get("tokens_out"),
             call_row.get("cost_usd"), call_row.get("token_provenance"),
             call_row.get("cost_provenance"),
             call_row.get("chars_in"), call_row.get("chars_out")))
        con.commit()
        return cur.lastrowid
    except Exception:
        return None


def set_outcome(sid: str, **fields) -> bool:
    """세션 결과를 기록/갱신한다. 실패해도 예외를 던지지 않는다.

    session-level outcome 이다. 개별 결정의 reward 가 아니다(§4).
    """
    allowed = {
        "variant", "experiment_id", "task_id", "repetition", "status",
        "completed", "grade_pre_pass", "grade_pre_total", "grade_post_pass",
        "grade_post_total", "repaired", "regressed", "turns",
        "early_stopped", "wall_ms",
    }
    use = {k: v for k, v in fields.items() if k in allowed}
    try:
        con = db()
        con.execute("INSERT OR IGNORE INTO session_outcomes(session_id,created_at)"
                    " VALUES(?,?)", (sid, now()))
        if use:
            sets = ",".join(f"{k}=?" for k in use)
            con.execute(f"UPDATE session_outcomes SET {sets} WHERE session_id=?",
                        (*use.values(), sid))
        con.commit()
        return True
    except Exception:
        return False


def calls_for(sid: str) -> list[dict]:
    return [dict(r) for r in db().execute(
        "SELECT * FROM model_calls WHERE session_id=? ORDER BY seq", (sid,))]


def outcome_for(sid: str) -> dict | None:
    r = db().execute("SELECT * FROM session_outcomes WHERE session_id=?",
                     (sid,)).fetchone()
    return dict(r) if r else None


def telemetry_stats() -> dict:
    """계측 요약. NULL(미측정)과 0(실제)을 따로 센다."""
    con = db()
    one = lambda q: con.execute(q).fetchone()[0]
    total = one("SELECT COUNT(*) FROM model_calls")
    return {
        "calls": total,
        "controller_calls": one("SELECT COUNT(*) FROM model_calls "
                                "WHERE is_controller=1"),
        "failed": one("SELECT COUNT(*) FROM model_calls WHERE ok=0"),
        "timed_out": one("SELECT COUNT(*) FROM model_calls WHERE timed_out=1"),
        "latency_measured": one("SELECT COUNT(*) FROM model_calls "
                                "WHERE latency_ms IS NOT NULL"),
        "tokens_null": one("SELECT COUNT(*) FROM model_calls "
                           "WHERE tokens_in IS NULL"),
        "cost_null": one("SELECT COUNT(*) FROM model_calls "
                         "WHERE cost_usd IS NULL"),
        "cost_zero": one("SELECT COUNT(*) FROM model_calls "
                         "WHERE cost_usd = 0.0"),
        "by_stage": dict(con.execute(
            "SELECT stage,COUNT(*) FROM model_calls GROUP BY stage").fetchall()),
        "by_token_prov": dict(con.execute(
            "SELECT token_provenance,COUNT(*) FROM model_calls "
            "GROUP BY token_provenance").fetchall()),
        "outcomes": one("SELECT COUNT(*) FROM session_outcomes"),
    }


def fallback_stats(alias_map: dict[str, str] | None = None) -> dict:
    """폴백을 '별칭으로 풀리는 것'과 '진짜 잘못된 것'으로 나눈다(§7).

    alias_map 은 호출부가 MEMBERS 에서 만들어 넘긴다 — store 가 bridge 를
    import 하면 순환이 된다. 하드코딩하지 않는 이유이기도 하다.
    """
    alias_map = {k.lower(): v for k, v in (alias_map or {}).items()}
    con = db()
    legacy = alias_ok = true_invalid = 0
    for raw_next, flags in con.execute(
            "SELECT raw_next,flags FROM decisions WHERE fallback=1"):
        try:
            fl = json.loads(flags or "[]")
        except (TypeError, ValueError):
            fl = []
        legacy += 1
        if "invalid_next" in fl:
            if alias_map.get(str(raw_next or "").strip().lower()):
                alias_ok += 1
            else:
                true_invalid += 1
    return {"legacy_fallback": legacy,
            "alias_resolvable": alias_ok,
            "true_invalid": true_invalid}


def _flag_counts(con) -> dict:
    counts: dict[str, int] = {}
    for (raw,) in con.execute("SELECT flags FROM decisions WHERE fallback=1"):
        try:
            for f in json.loads(raw or "[]"):
                counts[f] = counts.get(f, 0) + 1
        except (TypeError, ValueError):
            continue
    return counts


# ── 읽기 ────────────────────────────────────────────────────

def recent(limit: int = 30) -> list[dict]:
    rows = db().execute(
        "SELECT id,task,created_at,updated_at,status,active,"
        "(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS n "
        "FROM sessions s ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["active"] = json.loads(d["active"] or "[]")
        out.append(d)
    return out


def as_markdown(sid: str) -> str | None:
    """대화 전체를 사람이 읽을 수 있는 마크다운으로 만든다.

    결과물만 저장하면 '왜 그렇게 됐는지'가 사라진다. 의논 과정까지
    남겨야 나중에 판단을 되짚을 수 있다.
    """
    d = load(sid)
    if not d:
        return None
    out = [f"# {d['task']}", ""]
    out.append(f"- 일시: {d['created_at']}")
    out.append(f"- 참가자: {', '.join(d['active'])}")
    out.append(f"- 바퀴: {d['rounds']} · 상태: {d['status']}")
    if d["files"]:
        out.append(f"- 첨부: {', '.join(d['files'])}")
    out += ["", "---", "", "## 의논", ""]
    for m in d["messages"]:
        if m["kind"] == "final":
            continue
        who = "**나**" if m["kind"] == "interject" else f"**{m['name']}**"
        model = f" `{m['model']}`" if m["model"] else ""
        out.append(f"{who}{model}")
        out.append(f"> {m['text']}".replace("\n", "\n> "))
        out.append("")
    final = next((m for m in d["messages"] if m["kind"] == "final"), None)
    if final:
        out += ["---", "", "## 결과물", "", final["text"], ""]
    return "\n".join(out)


def load(sid: str) -> dict | None:
    con = db()
    s = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        return None
    msgs = con.execute(
        "SELECT who,name,model,color,kind,text FROM messages "
        "WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    d = dict(s)
    d["active"] = json.loads(d["active"] or "[]")
    d["files"] = json.loads(d["files"] or "[]")
    d["messages"] = [dict(m) for m in msgs]
    return d
