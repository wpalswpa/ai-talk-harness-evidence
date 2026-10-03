"""모델 호출 계측 (P0-b).

## 왜 별도 모듈인가

`Member.ask()` 의 반환형은 `str` 이다(불변식 I7). 여기에 토큰·지연을
끼워 넣으려면 반환형을 바꿔야 하고, 그러면 참여자 5개 구현과 호출부
7곳을 전부 고쳐야 한다. **동작 변경 금지** 요구와 정면으로 충돌한다.

그래서 **side-channel** 로 만들었다. `ask()` 는 그대로 문자열을 주고,
계측값은 호출 스레드에 딸린 슬롯에 남는다. 호출부는 원하면 꺼내 쓰고,
안 꺼내도 아무 일도 일어나지 않는다.

    with telemetry.measure("ledger", key="E", model="groq") as m:
        text = member.ask(...)
    m.record(...)      # 선택

스레드 지역 변수를 쓰는 이유: SSE 생성기와 POST 핸들러가 다른
스레드에서 돈다(`store.db()` 가 같은 이유로 스레드 지역이다).

## NULL 과 0 을 구분한다

**측정 못 한 것과 실제로 0 인 것은 다르다.** CLI 참여자는 토큰 수를
알 방법이 없으므로 `tokens_in=None` 이고, 이것을 0 으로 적으면
"토큰을 안 썼다"는 거짓이 된다. 그래서 모든 수치 필드의 기본값은
`None` 이고, DB 칼럼도 NOT NULL 을 걸지 않는다.

비용도 같다. 구독 CLI 는 호출당 과금이 없지만 그것은 `cost_usd=0.0`
(실제 0)이고, 토큰을 모르는 API 실패는 `cost_usd=None`(미측정)이다.
둘을 섞으면 지출 합계가 거짓말을 한다.

## provenance

어떻게 얻은 값인지 남긴다. 나중에 "이 비용 숫자를 믿어도 되나"를
판단하는 근거다.

  measured   공급자 응답의 usage 필드에서 읽음
  estimated  문자 수 기반 추정
  none       모름 (값은 None)
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

#: 단계 이름. 호출부 7곳과 1:1 로 맞춘다.
STAGE_RUBRIC = "rubric"            # 평가 기준 생성
STAGE_DRAFT = "draft"              # 독립 초안
STAGE_LEDGER = "ledger"            # 사회자 판정 (= controller call)
STAGE_DISCUSS = "discuss"          # 의논 발언
STAGE_WRITE = "write"              # 결과물 작성
STAGE_GRADE = "grade"              # 채점
STAGE_REPAIR = "repair"            # 수정본 작성
STAGE_REGRADE = "regrade"          # 재채점

#: 이 단계들은 결과물을 만들지 않는 '제어 비용'이다. B0~B3 비교에서
#: controller cost 를 따로 세려면 구분이 필요하다.
CONTROLLER_STAGES = {STAGE_LEDGER}

PROV_MEASURED = "measured"
PROV_ESTIMATED = "estimated"
PROV_NONE = "none"

_local = threading.local()


@dataclass
class Call:
    """모델 호출 한 번의 계측값.

    수치가 None 이면 **측정 못 한 것**이다. 0 과 구분한다.
    """

    stage: str
    member_key: str = ""
    member_name: str = ""
    vendor: str = ""
    model: str = ""

    started_at: float = 0.0
    latency_ms: int | None = None

    ok: bool | None = None            # None = 아직 안 끝남
    error_kind: str = ""              # timeout | provider | empty | ""
    timed_out: bool = False

    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    token_provenance: str = PROV_NONE
    cost_provenance: str = PROV_NONE

    chars_in: int | None = None
    chars_out: int | None = None

    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "stage": self.stage, "member_key": self.member_key,
            "member_name": self.member_name, "vendor": self.vendor,
            "model": self.model, "latency_ms": self.latency_ms,
            "ok": None if self.ok is None else int(self.ok),
            "error_kind": self.error_kind, "timed_out": int(self.timed_out),
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "token_provenance": self.token_provenance,
            "cost_provenance": self.cost_provenance,
            "chars_in": self.chars_in, "chars_out": self.chars_out,
        }


# ── 공급자가 알려준 usage 를 받는 통로 ──────────────────────
#
# bridge.py 의 API 참여자가 응답을 파싱할 때 여기에 넣는다. ask() 의
# 반환형을 바꾸지 않고 토큰을 건지는 유일한 방법이다. 호출 직전에
# 비우고 직후에 읽으므로 한 호출의 값만 들어온다.

def stash_usage(tokens_in: int | None, tokens_out: int | None,
                vendor: str = "") -> None:
    """공급자 응답의 usage 를 이번 호출 슬롯에 넣는다. 실패해도 조용히."""
    try:
        _local.usage = {"in": tokens_in, "out": tokens_out, "vendor": vendor}
    except Exception:
        pass


def _take_usage() -> dict | None:
    u = getattr(_local, "usage", None)
    _local.usage = None
    return u


@contextmanager
def measure(stage: str, *, key: str = "", name: str = "", vendor: str = "",
            model: str = "", prompt: str = ""):
    """모델 호출 한 번을 잰다.

    예외를 삼키지 않는다 — ask() 가 이미 예외를 안 던지므로 여기서
    또 삼키면 진짜 버그를 숨긴다. 다만 계측 자체의 실패는 무시한다.
    """
    call = Call(stage=stage, member_key=key, member_name=name,
                vendor=vendor, model=model, started_at=time.perf_counter())
    call.chars_in = len(prompt) if prompt else None
    _take_usage()                      # 이전 호출 잔여물 제거
    try:
        yield call
    finally:
        try:
            call.latency_ms = int((time.perf_counter() - call.started_at) * 1000)  # monotonic은 Windows에서 15.6ms 단위
            u = _take_usage()
            if u and (u.get("in") is not None or u.get("out") is not None):
                call.tokens_in = u.get("in")
                call.tokens_out = u.get("out")
                call.token_provenance = PROV_MEASURED
        except Exception:
            pass


def finish(call: Call, text: str, *, is_error: bool) -> Call:
    """응답을 보고 성공/실패와 남은 계측값을 채운다.

    `is_error` 는 호출부가 `bridge.is_error()` 로 판정해 넘긴다 —
    telemetry 가 bridge 를 import 하면 순환이 된다.
    """
    try:
        call.chars_out = len(text) if text is not None else None
        call.ok = not is_error
        if is_error:
            low = (text or "").lower()
            if "timeout" in low or "timed out" in low:
                call.error_kind, call.timed_out = "timeout", True
            elif "빈 응답" in (text or ""):
                call.error_kind = "empty"
            else:
                call.error_kind = "provider"
        # 토큰을 못 받았으면 추정치라도 남긴다. provenance 로 구분된다.
        if call.tokens_in is None and call.chars_in:
            call.tokens_in = call.chars_in // 4
            call.tokens_out = (call.chars_out or 0) // 4
            call.token_provenance = PROV_ESTIMATED
    except Exception:
        pass
    return call


#: 구독 CLI 는 호출당 과금이 없다. **공짜라는 뜻이 아니라 '이 원장이
#: 세는 단위로 0'** 이다. 구독료는 호출 수에 비례하지 않으므로
#: 호출당 비용을 지어내면 지출 합계가 거짓말을 한다.
SUBSCRIPTION_VENDORS = {"codex", "claude"}

#: 1M 토큰당 USD. 값을 모르는 공급자는 넣지 않는다 — 넣으면 추정이
#: 측정처럼 보인다. 확인한 것만 적는다.
PRICES: dict[str, tuple[float, float]] = {}


def price_call(call: Call) -> Call:
    """비용을 채운다. 모르면 None 으로 둔다(0 이 아니다)."""
    try:
        if call.vendor in SUBSCRIPTION_VENDORS:
            call.cost_usd = 0.0            # 실제 0
            call.cost_provenance = PROV_MEASURED
            return call
        p = PRICES.get(call.vendor)
        if p and call.tokens_in is not None and call.tokens_out is not None:
            call.cost_usd = (call.tokens_in * p[0] + call.tokens_out * p[1]) / 1e6
            call.cost_provenance = (PROV_MEASURED
                                    if call.token_provenance == PROV_MEASURED
                                    else PROV_ESTIMATED)
        else:
            call.cost_usd = None           # 미측정
            call.cost_provenance = PROV_NONE
    except Exception:
        pass
    return call
