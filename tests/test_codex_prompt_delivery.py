"""Codex 프롬프트가 바이트 그대로 도착하는지 실제 하위 프로세스로 확인한다.

결정 기록 003. 예전에는 프롬프트를 명령줄 인자 마지막에 붙였다. Windows의
npm 전역 설치 `codex`는 `.CMD` 래퍼라 cmd.exe가 명령줄을 다시 해석하고,
여러 줄 프롬프트는 첫 줄만 도착했다(9/22 운영 기록). 지금은 인자 자리에 `-`를
두고 프롬프트를 stdin으로 보낸 뒤 닫는다.

가짜 CLI는 codex exec처럼 마지막 인자가 `-`이면 stdin을 끝까지 읽고, 아니면
마지막 인자를 프롬프트로 쓴다. 받은 프롬프트의 UTF-8 바이트 길이와 SHA-256을
돌려준다. 실제 Codex를 부르지 않는다.
"""

import hashlib
import os
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "talk"))

import bridge  # noqa: E402

REPORT_CLI = textwrap.dedent('''
    import hashlib, sys
    # codex exec처럼 마지막 인자가 "-"이면 stdin에서, 아니면 인자에서 프롬프트를 읽는다
    if sys.argv[-1] == "-":
        data, via = sys.stdin.buffer.read(), "stdin"   # stdin이 닫혀야 여기서 돌아온다
    else:
        data, via = sys.argv[-1].encode("utf-8"), "argv"
    sys.stdout.reconfigure(encoding="utf-8")
    print("OpenAI Codex v0")
    print("--------")
    print("codex")
    print("BYTES=%d SHA=%s VIA=%s" % (len(data), hashlib.sha256(data).hexdigest(), via))
    print("tokens used")
    print("1")
''')

# 서버 프롬프트와 같은 모양(과제·역할·지금까지의 의논·규칙) 20줄
SERVER_SHAPE = "\n".join(
    ["[과제] JSON과 TOML 중 설정 형식 고르기", "", "[네 역할] 너는 기획·분석 담당이다.",
     "", "[지금까지의 의논]"]
    + [f"참여자{i}: 주석 지원과 중첩 표현을 비교했다 {i}" for i in range(13)]
    + ["", "이제 네 차례다. 위 의논에 이어서 네 의견을 말해라."])

CASES = {
    "server_shape_20_lines": SERVER_SHAPE,
    "percent_var": "값=%TALK_TEST_VAR% 끝\n둘째 줄",
    "quote_then_amp": 'A는 "좋다" & echo INJECTED> injected_marker.txt\n다음 줄',
    "odd_quote_amp": 'A는 "좋다 & echo INJECTED> injected_marker.txt',
    "caret_pipe_lt_gt": "a^b | c < d > e\n끝",
    "long_9000": ("가나다라마바사아자차\n" * 819)[:9000],
}


def _expect(prompt: str) -> str:
    data = prompt.encode("utf-8")
    return f"BYTES={len(data)} SHA={hashlib.sha256(data).hexdigest()}"


class ReportCodex(bridge.Codex):
    """Codex.build를 그대로 쓰고 실행 파일만 가짜 CLI로 바꾼다."""

    def __init__(self, exe: list[str]):
        super().__init__("A", "Codex", "codex", "a", "")
        self._exe = exe

    def build(self, prompt, workdir, images, json_mode=False):
        cmd = super().build(prompt, workdir, images, json_mode)
        assert cmd[0] == "codex"
        return self._exe + cmd[1:]


def test_codex_build_puts_dash_not_prompt_on_command_line():
    cmd = bridge.Codex("A", "Codex", "codex", "a", "").build(
        "첫 줄\n둘째 줄", "/w", ["x.png"])
    assert "-" in cmd and not any("첫 줄" in c for c in cmd)
    assert cmd.index("-") < cmd.index("-i"), "'-'는 이미지 인자보다 앞에 둔다"


@pytest.mark.parametrize("name", list(CASES))
def test_prompt_arrives_byte_exact_on_stdin(name, tmp_path, monkeypatch):
    """직접 실행(모든 OS): 여러 줄·%·&·따옴표·9,000자가 그대로 도착한다."""
    script = tmp_path / "report_cli.py"
    script.write_text(REPORT_CLI, encoding="utf-8")
    monkeypatch.setenv("TALK_TEST_VAR", "EXPANDED")
    prompt = CASES[name]
    out = ReportCodex([sys.executable, str(script)]).ask(prompt, str(tmp_path), [], timeout=30)
    assert out.startswith(_expect(prompt) + " VIA="), out[:200]
    assert not (tmp_path / "injected_marker.txt").exists()


SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=__PY__"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\report_cli.py" %*
'''


@pytest.mark.skipif(os.name != "nt", reason="npm .CMD 래퍼는 Windows에만 있다")
@pytest.mark.parametrize("name", list(CASES))
def test_prompt_survives_windows_cmd_shim(name, tmp_path, monkeypatch):
    """Windows: npm이 만드는 codex.cmd와 같은 모양의 래퍼를 거쳐도 그대로 도착한다.

    래퍼 본문은 npm 전역 설치의 codex.cmd에서 대상만 바꾼 것이다.
    `Member.ask()`는 이름을 PATH에서 찾아 .CMD 절대경로로 실행한다.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "report_cli.py").write_text(REPORT_CLI, encoding="utf-8")
    (bindir / "fakecodex.cmd").write_text(SHIM.replace("__PY__", sys.executable),
                                          encoding="ascii", newline="\r\n")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TALK_TEST_VAR", "EXPANDED")
    assert bridge._which("fakecodex").lower().endswith(".cmd")
    wd = tmp_path / "wd"
    wd.mkdir()
    prompt = CASES[name]
    out = ReportCodex(["fakecodex"]).ask(prompt, str(wd), [], timeout=60)
    assert out.startswith(_expect(prompt) + " VIA="), out[:200]
    assert not (wd / "injected_marker.txt").exists(), "& 뒤 명령이 실행됐다"
