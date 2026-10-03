"""CLI 참여자 호출을 실제 하위 프로세스로 돌려 본다.

`test_talk_contract.py`의 실패 검사는 파서에 문자열을 넣어 보는 단위 검사다.
여기서는 가짜 CLI 프로그램을 진짜 프로세스로 띄워 `Member.ask()` 전체 경로
(프로세스 생성 → 출력 수집 → 시간 초과 처리 → 파서)를 지나가게 한다.

가짜 CLI가 흉내 내는 것은 저장된 실패 기록의 모양이다. 수집 실행에서 실패한
Codex 발언은 본문 없이 실행 배너와 사용 한도 초과 오류 줄만 남겼고, 호출은
3~7초 만에 끝났다. 실제 Codex를 부르지 않으므로 비용과 네트워크가 들지 않는다.
"""

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "talk"))

import bridge  # noqa: E402

BANNER = ("Reading additional input from stdin...\n"
          "OpenAI Codex v0.155.1 (research preview)\n--------\n"
          "workdir: C:\\work\n--------\n"
          "ERROR: You've hit your usage limit.")

FAKE_CLI = textwrap.dedent('''
    import os, sys, time
    mode = os.environ.get("FAKE_CLI_MODE", "ok")
    if mode == "ok":
        print("OpenAI Codex v0.155.1\\n--------\\ncodex\\n실제 답변입니다\\ntokens used\\n123")
    elif mode == "usage_limit":
        sys.stderr.write(os.environ["FAKE_CLI_BANNER"])
        sys.exit(1)
    elif mode == "read_stdin":
        data = sys.stdin.read()          # 입력이 열려 있으면 여기서 멈춘다
        print("OpenAI Codex v0.155.1\\n--------\\ncodex\\n입력 " + str(len(data)) + "자를 읽고 답했다\\ntokens used\\n9")
    elif mode == "hang":
        time.sleep(60)
''')


class FakeCodex(bridge.Codex):
    """명령만 가짜 CLI로 바꾸고 ask·parse는 Codex 것을 그대로 쓴다."""

    def __init__(self, script: Path):
        super().__init__("A", "Codex", "codex", "a", "")
        self._script = script

    def build(self, prompt, workdir, images, json_mode=False):
        return [sys.executable, str(self._script), prompt]


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    monkeypatch.setenv("FAKE_CLI_BANNER", BANNER)

    def run(mode, timeout=20):
        monkeypatch.setenv("FAKE_CLI_MODE", mode)
        started = time.monotonic()
        out = FakeCodex(script).ask("질문", str(tmp_path), [], timeout=timeout)
        return out, time.monotonic() - started
    return run


def test_runtime_normal_output_is_speech(fake):
    out, _ = fake("ok")
    assert out == "실제 답변입니다"
    assert not bridge.is_error(out)


def test_runtime_usage_limit_failure_becomes_error_marker(fake):
    """본문 없이 배너와 오류 줄만 나온 실패는 발언이 아니라 오류 표시다."""
    out, _ = fake("usage_limit")
    assert bridge.is_error(out), f"실패가 발언으로 통과했다: {out[:80]}"
    assert "Reading additional input" in out   # 첫 줄만 이유로 남긴다
    assert "usage limit" not in out             # 배너 전체를 발언처럼 옮기지 않는다


PARENT = textwrap.dedent('''
    import sys
    sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/talk")
    sys.path.insert(0, sys.argv[2])
    import bridge, test_cli_runtime as t
    from pathlib import Path
    out = t.FakeCodex(Path(sys.argv[3])).ask("질문", sys.argv[4], [], timeout=5)
    sys.stdout.reconfigure(encoding="utf-8")
    print(out)
''')


def test_runtime_stdin_is_closed_even_if_server_stdin_stays_open(tmp_path):
    """서버의 stdin이 열린 채로 있어도 CLI가 입력을 기다리지 않는다.

    수정 전 코드는 stdin 인자를 주지 않아 CLI가 서버의 stdin을 물려받았다.
    그 조건을 만들려고, 닫히지 않는 파이프를 stdin으로 가진 부모 프로세스
    안에서 ask()를 부른다. stdin을 물려받으면 가짜 CLI가 입력을 기다리다
    시간 초과로 끝나고, 닫으면 바로 답한다.
    """
    import subprocess
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(PARENT, encoding="utf-8")
    env = dict(os.environ, FAKE_CLI_MODE="read_stdin", FAKE_CLI_BANNER=BANNER)
    p = subprocess.Popen(
        [sys.executable, str(parent), str(ROOT), str(Path(__file__).parent),
         str(script), str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        # stdin은 일부러 닫지 않은 채 읽는다. ask()의 시간 초과(5초)가 상한이다.
        out, err = p.stdout.read(), p.stderr.read()
    finally:
        p.stdin.close()
        p.wait(timeout=30)
    text = out.decode("utf-8", "replace").strip()
    assert text == "입력 0자를 읽고 답했다", (text, err.decode("utf-8", "replace")[-300:])


def test_runtime_hanging_cli_times_out_as_error_marker(fake):
    """응답하지 않는 CLI는 시간 초과 표시로 끝나고 대화를 막지 않는다."""
    out, elapsed = fake("hang", timeout=2)
    assert bridge.is_error(out) and "시간 초과" in out
    assert elapsed < 20
