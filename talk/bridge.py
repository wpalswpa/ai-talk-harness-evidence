"""Codex(ChatGPT 구독)와 Claude Code를 한 톡방에 붙인다.

API 키가 아니라 이미 로그인된 CLI를 그대로 호출한다. 추가 요금이 없고
각자의 구독 한도를 쓴다.

두 CLI 모두 작업 폴더를 받으므로 파일을 직접 읽는다. 업로드한 파일은
작업 폴더에 저장되고, 둘 다 경로로 접근한다. 이미지는 Codex가 -i로
직접 보고, Claude는 Read 도구로 본다.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# npm 전역 설치 경로가 PATH에 없을 수 있다
EXTRA_PATH = [
    r"C:\Program Files\nodejs",
    os.path.expanduser(r"~\AppData\Roaming\npm"),
]


#: 자식 CLI에게 넘기지 않을 환경변수. CLI는 파일을 읽을 수 있으므로
#: (실측: --allowedTools 로도 Read 를 막지 못한다) 최소한 환경에서는
#: 키가 보이지 않게 한다.
SECRET_ENV = ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY",
              "ANTHROPIC_API_KEY", "AI_GATEWAY_API_KEY", "HF_TOKEN")


def _env(strip_secrets: bool = False) -> dict:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([env.get("PATH", "")] + EXTRA_PATH)
    if strip_secrets:
        for k in SECRET_ENV:
            env.pop(k, None)
    return env


def _which(name: str) -> str | None:
    return shutil.which(name, path=_env()["PATH"])


def _kill(p: subprocess.Popen) -> None:
    """자식까지 확실히 죽인다. CLI가 또 다른 프로세스를 띄우기 때문이다."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=15)
        else:
            p.terminate()
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


class Cancel:
    """한 대화의 중단 신호. 실행 중인 CLI 프로세스를 들고 있다가 죽인다.

    두 종류를 구분한다.
      cancel()  — 대화 전체를 끝낸다 (중단 버튼)
      abort()   — 지금 턴만 버린다 (끼어들기 steer 모드). 대화는 계속된다

    이 구분이 없으면 끼어들기가 대화를 통째로 죽인다.
    """

    def __init__(self) -> None:
        self._flag = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        return self._flag.is_set()

    def abort(self) -> None:
        """실행 중인 프로세스만 죽인다. 중단 플래그는 세우지 않는다."""
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            _kill(p)

    def attach(self, p: subprocess.Popen) -> None:
        with self._lock:
            if self._flag.is_set():   # 이미 취소된 뒤 시작한 프로세스
                _kill(p)
                return
            self._procs.add(p)

    def detach(self, p: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(p)

    def cancel(self) -> None:
        with self._lock:
            self._flag.set()
            procs = list(self._procs)
        for p in procs:
            _kill(p)


def is_error(text: str) -> bool:
    """참여자 응답이 실제 발언이 아니라 오류 표시인지."""
    return bool(text) and text.startswith("[") and text.rstrip().endswith("]")


class Inbox:
    """대화 중 사용자가 끼어든 말을 담는다.

    업계 표준(Codex CLI·Claude Code·Copilot CLI)은 두 모드다.
      queue — 지금 턴이 끝나면 다음 발언자에게 전달
      steer — 지금 턴을 버리고 바로 반영해 다시 말하게 한다

    steer 플래그와 그 텍스트를 한 덩어리로 묶는다. 따로 두면
    앞선 drain 이 텍스트만 가져가고 플래그만 남아, 그 지시를 이미
    반영한 멀쩡한 턴을 버리게 된다.
    """

    def __init__(self) -> None:
        self._q: list[tuple[str, str]] = []   # (텍스트, 모드)
        self._lock = threading.Lock()
        self._steer_pending = False           # drain 이 아직 안 가져간 steer

    def put(self, text: str, mode: str = "queue") -> None:
        with self._lock:
            self._q.append((text, mode))
            if mode == "steer":
                self._steer_pending = True

    def drain(self) -> tuple[list[str], bool]:
        """(끼어든 말들, 이번에 steer가 있었나)를 함께 돌려준다."""
        with self._lock:
            items, self._q = self._q, []
            steered = self._steer_pending
            self._steer_pending = False
        return [t for t, _ in items], steered

    def pending(self) -> int:
        with self._lock:
            return len(self._q)


class Member:
    """한 참여자. CLI 하나를 감싼다."""

    #: 이 CLI로 고를 수 있는 모델. 첫 항목이 기본값.
    models: list[tuple[str, str]] = []

    def __init__(self, key: str, name: str, cli: str, color: str, persona: str):
        self.key, self.name, self.cli = key, name, cli
        self.color, self.persona = color, persona
        self.model: str | None = None  # None이면 CLI 기본 설정을 따른다

    @property
    def available(self) -> bool:
        return _which(self.cli) is not None

    def account(self) -> dict:
        """로그인한 계정 정보. 실패해도 예외를 던지지 않는다."""
        return {"loggedIn": False, "reason": "확인 방법 없음"}

    def build(self, prompt: str, workdir: str, images: list[str],
              json_mode: bool = False) -> list[str]:
        raise NotImplementedError

    def ask(self, prompt: str, workdir: str, images: list[str],
            timeout: int = 300, cancel: "Cancel | None" = None,
            long: bool = False, json_mode: bool = False,
            schema: dict | None = None) -> str:
        """한 번 묻고 답을 통째로 받는다.

        CLI가 죽어도 예외를 던지지 않는다 — 한 명이 실패해도 대화는
        이어져야 한다. cancel이 눌리면 프로세스를 죽인다.

        long은 CLI 참여자에게 쓰이지 않는다(길이는 프롬프트로 조절).
        json_mode는 제어·채점 턴에만 쓴다. 의논 턴에 스키마를 걸면
        추론 품질이 떨어진다는 반론이 있다(Let Me Speak Freely?).

        schema는 **계약의 일부지만 CLI 참여자는 무시한다.** API 참여자는
        서버에 스키마를 걸어 형식을 강제할 수 있지만 CLI에는 그런 통로가
        없다. 그래도 인자를 받는 이유는, 호출부가 참여자 종류를 알고
        분기하게 만들면 참여자를 추가할 때마다 호출부를 고쳐야 하기
        때문이다. 형식 강제가 안 되는 쪽은 `orchestrator.parse_json()`이
        군말 섞인 응답에서 JSON을 건져내는 것으로 처리한다.
        """
        cmd = self.build(prompt, workdir, images, json_mode)
        # Windows의 npm 전역 설치는 .CMD 래퍼라 절대경로로 직접 실행해야 한다
        exe = _which(cmd[0])
        if exe:
            cmd = [exe] + cmd[1:]
        try:
            p = subprocess.Popen(
                # CLI 참여자에게는 API 키를 넘기지 않는다. 이들은 구독
                # 로그인으로 인증하므로 키가 필요 없다.
                cmd, cwd=workdir, env=_env(strip_secrets=True),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # stdin 을 반드시 닫는다. 서버의 stdin 을 물려받으면 입력을
                # 끝까지 읽는 CLI 가 시간 초과까지 멈춘다(가짜 CLI 로 재현,
                # tests/test_cli_runtime.py). 수집 trace 의 실패 배너는 이것이
                # 아니라 사용 한도 초과였고, 발언으로 남은 원인은 parse 였다
                # (docs/decisions/001).
                stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            return f"[{self.cli} 를 찾을 수 없습니다]"

        if cancel is not None:
            cancel.attach(p)
        try:
            stdout, stderr = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(p)
            return f"[{self.name} 응답 시간 초과]"
        finally:
            if cancel is not None:
                cancel.detach(p)

        if cancel is not None and cancel.is_set():
            return f"[{self.name} 중단됨]"
        out = self.parse(stdout, stderr, p.returncode)
        return out or f"[{self.name} 가 빈 응답을 보냈습니다]"

    def parse(self, stdout: str, stderr: str, code: int) -> str:
        return stdout.strip()


class Codex(Member):
    """ChatGPT 구독으로 로그인된 codex CLI."""

    models = [
        ("", "설정값 따름"),
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-6-astra", "GPT-6 Astra"),
    ]

    def account(self) -> dict:
        """~/.codex/auth.json의 id_token을 로컬에서 읽는다.

        토큰을 어디로도 보내지 않는다. 서명 검증 없이 페이로드만 디코딩해
        이메일·요금제·만료를 꺼낸다.
        """
        path = Path.home() / ".codex" / "auth.json"
        if not path.exists():
            return {"loggedIn": False, "reason": "로그인 기록 없음"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            token = (data.get("tokens") or {}).get("id_token", "")
            if not token:
                mode = data.get("auth_mode") or "unknown"
                return {"loggedIn": bool(data.get("OPENAI_API_KEY")),
                        "method": mode, "reason": "토큰 없음"}
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            auth = claims.get("https://api.openai.com/auth", {}) or {}
            # id_token은 짧게 만료되지만 CLI가 refresh_token으로 자동 갱신한다.
            # 만료 자체는 로그아웃이 아니므로 경고로 쓰지 않는다.
            return {
                "loggedIn": True,
                "method": "ChatGPT 구독",
                "email": claims.get("email", ""),
                "plan": auth.get("chatgpt_plan_type", ""),
                "refreshedAt": (data.get("last_refresh") or "")[:16].replace("T", " "),
                "canRefresh": bool((data.get("tokens") or {}).get("refresh_token")),
            }
        except Exception as exc:
            return {"loggedIn": False, "reason": f"읽기 실패 ({type(exc).__name__})"}

    def build(self, prompt, workdir, images, json_mode=False):
        cmd = ["codex", "exec", "--skip-git-repo-check", "-C", workdir,
               "--sandbox", "read-only"]
        if json_mode:
            # 사람용 출력을 긁지 않고 마지막 메시지를 파일로 받는다.
            # 출력 형식이 바뀌어도 조용히 틀린 텍스트를 쓰지 않는다.
            self._out = Path(workdir) / f".codex_out_{os.getpid()}.txt"
            cmd += ["-o", str(self._out)]
        if self.model:
            cmd += ["-m", self.model]
        for img in images:
            cmd += ["-i", img]
        cmd.append(prompt)
        return cmd

    _out: "Path | None" = None

    def parse(self, stdout, stderr, code):
        """codex exec는 헤더와 메타를 함께 찍는다. 본문만 꺼낸다.

        json_mode 에서는 -o 로 받은 파일을 읽는다. 사람용 출력을 긁는
        경로는 CLI 형식이 바뀌면 조용히 틀린 텍스트를 쓰므로 위험하다.
        """
        if self._out is not None:
            try:
                body = self._out.read_text(encoding="utf-8").strip()
                self._out.unlink(missing_ok=True)
                self._out = None
                if body:
                    return body
            except OSError:
                self._out = None
        text = stdout.strip()
        if "tokens used" in text:
            tail = text.rsplit("tokens used", 1)[1]
            # "tokens used\n5,875\n<답>" 형태 — 숫자 줄을 버린다
            lines = [l for l in tail.splitlines() if l.strip()]
            if len(lines) > 1:
                return "\n".join(lines[1:]).strip()
        # 반복분이 없으면 codex 마커 뒤를 쓴다
        if "\ncodex\n" in text:
            body = text.split("\ncodex\n", 1)[1]
            return body.split("\ntokens used", 1)[0].strip()
        # 본문을 못 찾았으면 **stderr 를 발언으로 돌려주지 않는다.**
        # 예전에는 `text or stderr` 였는데, codex 가 실패하면 시작 배너
        # ("Reading additional input from stdin...", 버전, workdir …)가
        # 그대로 참여자 발언으로 기록됐다. 실측에서 그 배너가 세 턴
        # 연속 남아 사회자가 그걸 두고 의논을 이어갔다.
        if text:
            return text
        why = (stderr or "").strip().splitlines()
        first = next((l for l in why if l.strip()), "이유 불명")
        return f"[{self.name} 응답 없음 — {first[:80]}]"


class ClaudeCode(Member):
    """Claude Code CLI. 이 세션과 같은 구독을 쓴다."""

    models = [
        ("", "설정값 따름"),
        ("opus", "Opus 5"),
        ("sonnet", "Sonnet 5"),
        ("haiku", "Haiku 4.5"),
    ]

    def account(self) -> dict:
        """`claude auth status`가 JSON을 준다. 그대로 읽는다."""
        exe = _which(self.cli)
        if not exe:
            return {"loggedIn": False, "reason": "CLI 없음"}
        try:
            p = subprocess.run([exe, "auth", "status"], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               env=_env(), timeout=30)
            d = json.loads(p.stdout[p.stdout.find("{"):p.stdout.rfind("}") + 1])
        except Exception as exc:
            return {"loggedIn": False, "reason": f"확인 실패 ({type(exc).__name__})"}
        out = {
            "loggedIn": bool(d.get("loggedIn")),
            "method": d.get("authMethod", ""),
            "email": d.get("email", ""),
            "org": d.get("orgName", ""),
        }
        # 요금제는 별도 설정 파일에만 있다
        try:
            acc = json.loads(
                (Path.home() / ".claude.json").read_text(encoding="utf-8")
            ).get("oauthAccount") or {}
            out["plan"] = acc.get("organizationType", "")
            out["tier"] = acc.get("organizationRateLimitTier", "")
        except Exception:
            pass
        return out

    def build(self, prompt, workdir, images, json_mode=False):
        # 이미지는 경로를 알려주면 Read 도구로 직접 본다
        if images:
            prompt += "\n\n첨부 파일: " + ", ".join(images)
        # 판정·채점 턴은 주어진 텍스트만 보면 된다. 도구를 끊어 빠르게.
        if json_mode:
            cmd = ["claude", "-p", prompt, "--allowedTools", "none"]
            if self.model:
                cmd += ["--model", self.model]
            return cmd
        cmd = ["claude", "-p", prompt, "--add-dir", workdir,
               # 대화에 필요한 건 파일 읽기뿐이다. 검색·실행·편집을 막으면
               # 불필요한 탐색이 사라져 응답이 빨라진다.
               "--allowedTools", "Read", "Glob",
               # --add-dir 은 '추가 허용'일 뿐 상위 경로를 막지 않는다.
               # 실행·편집·네트워크 도구를 명시적으로 막아 키 유출 경로를 줄인다.
               "--disallowedTools", "Bash", "Write", "Edit", "WebFetch",
               "WebSearch", "NotebookEdit", "Task"]
        if self.model:
            cmd += ["--model", self.model]
        return cmd


class Gemini(Member):
    """Google 계정 로그인(OAuth)으로 쓰는 gemini CLI.

    개인 Google 계정 로그인이면 하루 1,000요청까지 무료다(API 키는 250회로
    더 적다). 출처: google-gemini.github.io/gemini-cli quota-and-pricing.
    """

    models = [
        ("", "설정값 따름"),
        ("gemini-3.1-pro", "Gemini 3.1 Pro"),
        ("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ]

    def account(self) -> dict:
        exe = _which(self.cli)
        if not exe:
            return {"loggedIn": False, "reason": "CLI 없음"}
        # 공식 문서가 인증 확인 명령을 따로 제공하지 않는다.
        # 설정 파일의 selectedAuthType 으로 판단한다.
        st = Path.home() / ".gemini" / "settings.json"
        if not st.exists():
            return {"loggedIn": False, "method": "",
                    "reason": "로그인 필요 — 터미널에서 gemini 실행 후 Google 로그인"}
        try:
            d = json.loads(st.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        auth = (d.get("security") or {}).get("auth") or {}
        kind = auth.get("selectedType") or d.get("selectedAuthType") or ""
        if not kind and not os.environ.get("GEMINI_API_KEY"):
            return {"loggedIn": False, "method": "",
                    "reason": "로그인 필요 — 터미널에서 gemini 실행"}
        label = {
            "oauth-personal": "Google 계정 (무료 1,000회/일)",
            "gemini-api-key": "API 키 (250회/일)",
        }.get(kind, kind or "API 키")
        return {"loggedIn": True, "method": label, "email": "", "plan": ""}

    def build(self, prompt, workdir, images):
        # 이미지 전용 플래그는 공식 문서에서 확인되지 않았다. 경로만 알려준다.
        if images:
            prompt += "\n\n첨부 파일: " + ", ".join(images)
        cmd = ["gemini", "-p", prompt, "--include-directories", workdir]
        if self.model:
            cmd += ["-m", self.model]
        return cmd


class OpenAIShaped(Member):
    """OpenAI 호환 HTTP API를 쓰는 참여자.

    Gemini와 Groq 둘 다 OpenAI 스펙을 그대로 받으므로 base_url과 키만
    바꿔 끼운다. CLI가 아니라 HTTP라 파일은 서버가 읽어 넣어 준다.
    """

    base_url = ""
    env_key = ""
    key_url = ""      # 키 발급처. 화면에 안내로 띄운다
    reasoning_effort = ""   # 추론 모델이면 "low" 로 생각을 줄인다

    def _key(self) -> str:
        return (os.environ.get(self.env_key) or "").strip()

    @property
    def available(self) -> bool:
        return bool(self._key())

    def account(self) -> dict:
        key = self._key()
        if not key:
            return {"loggedIn": False,
                    "reason": f"{self.env_key} 없음 — {self.key_url} 에서 발급"}
        # 키 길이도 알려주지 않는다. 있다/없다만 말한다.
        return {"loggedIn": True, "method": "API 키",
                "email": self.env_key, "plan": ""}

    def ask(self, prompt, workdir, images, timeout=300, cancel=None,
            long=False, json_mode=False, schema: dict | None = None):
        if cancel is not None and cancel.is_set():
            return f"[{self.name} 중단됨]"
        key = self._key()
        if not key:
            return f"[{self.name} 키 없음]"
        payload = {
            "model": self.model or self.models[0][0],
            "messages": [{"role": "user", "content": prompt}],
            # 대화 턴은 짧게, 최종 결과물은 잘리지 않게.
            # gpt-oss 같은 추론 모델은 reasoning에 토큰을 먼저 쓰므로
            # 대화 턴도 넉넉히 줘야 본문이 빈 채로 끝나지 않는다.
            "max_tokens": 2400 if long else 1200,
            "temperature": 0.3 if json_mode else 0.8,
        }
        if json_mode:
            # 스키마가 있으면 strict 로 강제한다. Groq은 스키마 없는
            # json_object 만으로는 400을 내는 경우가 있다(실측).
            if schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "out", "strict": True,
                                    "schema": schema},
                }
            else:
                payload["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload).encode()
        req = Request(self.base_url.rstrip("/") + "/chat/completions", data=body,
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + key,
                               # Groq 앞단 Cloudflare가 UA 없는 요청을 1010으로 막는다
                               "User-Agent": "mav-talk/1.0",
                               "Accept": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            # 공급자가 준 usage 를 계측 통로에 넘긴다. ask() 의 반환형은
            # 그대로 str 이다(불변식 I7) — 여기서 값을 '흘려 둘' 뿐이라
            # 호출부는 아무것도 바뀌지 않는다. 실패해도 대화에 영향 없다.
            try:
                import telemetry
                u = d.get("usage") or {}
                telemetry.stash_usage(u.get("prompt_tokens"),
                                      u.get("completion_tokens"), self.cli)
            except Exception:
                pass
            choice = d["choices"][0]
            text = (choice["message"].get("content") or "").strip()
            if text:
                return text
            # 왜 비었는지 말해 준다. 추론 모델이 생각에 토큰을 다 쓰면
            # finish_reason이 length로 돌아온다.
            why = choice.get("finish_reason", "")
            if why == "length":
                return f"[{self.name} 응답이 길이 제한에 걸렸습니다 — 생각만 하고 끝남]"
            return f"[{self.name} 가 빈 응답을 보냈습니다 ({why or '이유 불명'})]"
        except HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("error", {}).get("message", "")[:80]
            except Exception:
                pass
            return f"[{self.name} 오류 {exc.code}{': ' + detail if detail else ''}]"
        except Exception as exc:
            return f"[{self.name} 실패: {type(exc).__name__}]"

    def build(self, prompt, workdir, images, json_mode=False):
        raise NotImplementedError  # ask()를 직접 구현했다


class GeminiAPI(OpenAIShaped):
    """Gemini를 API 키로 쓴다.

    CLI의 개인 무료 티어는 2026-09 기준 구글이 차단했다
    (IneligibleTierError: 'migrate to Antigravity'). API 키 경로는 유효하다.
    """

    base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    env_key = "GEMINI_API_KEY"
    key_url = "aistudio.google.com/apikey"
    models = [
        ("gemini-3.5-flash", "Gemini 3.5 Flash"),
        ("gemini-3.1-pro", "Gemini 3.1 Pro"),
    ]


class Groq(OpenAIShaped):
    """Groq. 무료 티어가 넉넉하고 응답이 빠르다."""

    base_url = "https://api.groq.com/openai/v1"
    env_key = "GROQ_API_KEY"
    key_url = "console.groq.com/keys"
    # gpt-oss는 추론 모델이다. 대화 한 마디에 생각을 길게 하면
    # reasoning에 토큰을 다 쓰고 본문이 비어서 돌아온다.
    reasoning_effort = "low"
    # 2026-09-21 이 키로 실제 조회한 목록. llama-3.3-70b 는 제공되지 않는다.
    models = [
        ("openai/gpt-oss-120b", "GPT-OSS 120B"),
        ("openai/gpt-oss-20b", "GPT-OSS 20B"),
        ("qwen/qwen3.8-27b", "Qwen 3.8 27B"),
    ]


class Local(Member):
    """로컬 Ollama. 구독 한도를 쓰지 않는다.

    CLI가 아니라 HTTP로 직접 부른다. 이 PC는 Vulkan에서 죽으므로
    num_gpu=0 으로 고정한다.
    """

    URL = "http://localhost:11434"
    models = [
        ("gemma3:4b", "Gemma 3 4B"),
        ("llama3.1:8b", "Llama 3.1 8B"),
        ("qwen3:4b", "Qwen 3 4B"),
    ]

    @property
    def available(self) -> bool:
        try:
            urlopen(f"{self.URL}/api/tags", timeout=3).read()
            return True
        except Exception:
            return False

    def account(self) -> dict:
        try:
            got = json.loads(urlopen(f"{self.URL}/api/tags", timeout=5).read())
            names = [m["name"] for m in got.get("models", [])]
        except Exception:
            return {"loggedIn": False, "reason": "Ollama 서버 꺼짐 — ollama serve"}
        return {"loggedIn": True, "method": "로컬 (요금 없음)",
                "email": f"모델 {len(names)}개", "plan": "offline"}

    def ask(self, prompt, workdir, images, timeout=300, cancel=None,
            long=False, json_mode=False, schema: dict | None = None):
        """HTTP로 직접 묻는다. 파일은 서버가 읽어서 프롬프트에 넣어 준다.

        schema는 받지만 쓰지 않는다 — Ollama의 format 옵션은 json 여부만
        받고 스키마를 강제하지 못한다. 계약을 맞추기 위해 인자만 둔다.
        """
        if cancel is not None and cancel.is_set():
            return f"[{self.name} 중단됨]"
        req_body = {
            "model": self.model or self.models[0][0],
            "messages": [{"role": "user", "content": prompt}],
            "stream": False, "think": False,
            "options": {"num_predict": 1600 if long else 320,
                        "num_gpu": 0,
                        "temperature": 0.3 if json_mode else 0.8},
        }
        if json_mode:
            req_body["format"] = "json"   # Ollama 는 format 으로 강제한다
        body = json.dumps(req_body).encode()
        req = Request(f"{self.URL}/api/chat", data=body,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            try:
                import telemetry
                telemetry.stash_usage(d.get("prompt_eval_count"),
                                      d.get("eval_count"), self.cli)
            except Exception:
                pass
            return (d.get("message", {}).get("content") or "").strip() \
                or f"[{self.name} 가 빈 응답을 보냈습니다]"
        except Exception as exc:
            return f"[{self.name} 실패: {type(exc).__name__}]"

    def build(self, prompt, workdir, images, json_mode=False):
        raise NotImplementedError  # ask()를 직접 구현했다


MEMBERS = {
    "A": Codex(
        "A", "Codex", "codex", "a",
        "너는 기획·분석 담당이다. 무엇을 왜 만드는지, 누가 쓰는지, 빠진 관점이 "
        "무엇인지 본다. 상대의 실행 계획에 빠진 전제를 짚는다.",
    ),
    "B": ClaudeCode(
        "B", "Claude", "claude", "b",
        "너는 설계·실행 담당이다. 실제로 만들 수 있는지, 순서가 어떻게 되는지 "
        "본다. 막연한 말을 구체적 단계로 바꾸고, 무리한 계획은 줄이자고 말한다.",
    ),
    "C": GeminiAPI(
        "C", "Gemini", "gemini-api", "c",
        "너는 검증 담당이다. 앞선 주장에서 근거가 약한 곳과 빠진 사실을 "
        "짚는다. 동의만 하지 말고 반례를 찾는다.",
    ),
    "E": Groq(
        "E", "Groq", "groq", "e",
        "너는 정리 담당이다. 오간 이야기에서 합의된 것과 갈린 것을 "
        "구분해 짧게 정리한다.",
    ),
    "D": Local(
        "D", "로컬", "ollama", "d",
        "너는 요약 담당이다. 오간 이야기에서 합의된 것과 갈린 것을 "
        "짧게 정리한다.",
    ),
}

#: 기본 참가자. 실측 응답속도(2026-09-21): Groq 0.6s · Codex 5.6s ·
#: Claude 8.7s · Gemini 10.5s · 로컬 17.6s. 빠른 둘을 기본으로 둔다.
DEFAULT_ACTIVE = ["E", "B"]

# ── 독립 초안 ─────────────────────────────────────────────
# 토론 전에 각자 안을 낸다. 서로 보지 않는다.
# 근거: 바닐라 토론이 다수결보다 못한 이유가 초기 답안이 같아서다.
# 다양성을 먼저 만들어야 토론이 값을 한다 (arXiv:2601.19921).

DRAFT_RULES = (
    "\n\n[초안 규칙]\n"
    "- 다른 사람 의견을 보기 전에 네 안을 먼저 낸다.\n"
    "- 한국어. 3~5문장. 결론부터.\n"
    "- 마지막 줄에 `확신도: 높음|보통|낮음` 을 붙인다.\n"
    "  근거가 확실하면 높음, 추측이 섞이면 낮음.\n"
    "- 서론·인사 없이 바로 본론."
)

CONFIDENCE_RULE = (
    "\n- 발언 끝에 `확신도: 높음|보통|낮음` 을 붙인다. "
    "확신이 낮은 주장에는 상대가 쉽게 동조하지 않게."
)


def split_confidence(text: str) -> tuple[str, str]:
    """발언에서 확신도를 떼어낸다. (본문, 확신도)"""
    import re
    m = re.search(r"확신도\s*[:：]\s*(높음|보통|낮음)", text)
    if not m:
        return text.strip(), ""
    body = (text[:m.start()] + text[m.end():]).strip()
    return body, m.group(1)

TALK_RULES = (
    "\n\n[대화 규칙]\n"
    "- 한국어로만, **두 문장 이내**로 말한다. 길면 대화가 느려진다.\n"
    "- 파일을 읽는 것 외에 도구를 쓰지 않는다. 검색·실행 금지.\n"
    "- 무엇을 하겠다고 예고하지 말고, 지금 바로 그 내용을 말한다.\n"
    "  (나쁜 예: '다음을 점검하겠습니다' / 좋은 예: '이 양식은 ~가 문제다')\n"
    "- 상대 말에 반드시 반응한다. 동의하면 이유를, 다르면 대안을 말한다.\n"
    "- 인사말·서론·체크리스트 나열 없이 바로 본론. 말하듯이 쓴다.\n"
    "- 파일을 읽어야 하면 직접 읽고 그 내용을 근거로 말한다.\n"
    "- 파일을 수정하지 마라. 의논만 한다.\n"
    "- 도구를 길게 쓰지 말고 바로 의견을 말한다."
    + CONFIDENCE_RULE
)


def judge_for(author_key: str, active: list[str]) -> str | None:
    """작성자가 아닌 채점자를 고른다.

    같은 모델이 쓰고 채점하면 자기 글을 후하게 본다
    (자기선호 편향, arXiv:2404.13076). 벤더까지 다르게 고른다.
    고를 사람이 없으면 None — 채점을 건너뛰고 그 사실을 알린다.
    """
    others = [k for k in active if k != author_key]
    if not others:
        return None
    # CLI 참여자가 토큰 상한이 없어 채점 설명이 잘리지 않는다
    for k in ("B", "A", "E", "C", "D"):
        if k in others:
            return k
    return others[0]

#: 기계끼리 주고받는 압축 모드. 사람은 결과물만 보므로 중간 대화는
#: 읽기 좋을 필요가 없다. 존댓말·연결어·수사를 걷어내면 토큰과 시간이 준다.
#: 최종 결과물은 사람이 읽으므로 이 규칙을 적용하지 않는다.
TERSE_RULES = (
    "\n\n[대화 규칙 — 압축 모드]\n"
    "이 대화는 사람이 읽지 않는다. 기계끼리 결론만 주고받는다.\n"
    "- 한국어 명사구·단문. 존댓말·인사·연결어·수사 전부 금지.\n"
    "- **40자 이내.** 한 줄. 마침표도 생략 가능.\n"
    "- 형식: `동의:이유` 또는 `반대:대안` 또는 `보완:내용`\n"
    "- 예) `반대:합의/이견은 회의록 양식. 계획대비실적이 회고`\n"
    "- 예) `보완:지난주 할일 O/X 대조칸 최상단`\n"
    "- 설명하지 말고 결론만. 근거는 한 구절로 압축.\n"
    "- 파일 읽기 외 도구 금지. 파일 수정 금지."
)

FINAL_RULES = (
    "\n\n[최종 결과물]\n"
    "지금까지의 의논을 반영해 결과물만 작성한다.\n"
    "- 한국어. 서론·맺음말 없이 결과물 자체만.\n"
    "- 합의된 내용을 반영하고, 빈칸을 남기지 말고 실제 내용으로 채운다.\n"
    "- 파일을 새로 만들거나 고치지 마라. 답변 본문으로만 낸다.\n"
    "- **반드시 끝까지 완결하라.** 길이보다 완결이 중요하다. "
    "예시를 여러 개 늘리지 말고 하나만 넣어서, 중간에 잘리지 않게 한다.\n"
    "- 표는 행을 3개 이하로 제한한다."
)


#: 계정 조회 결과 캐시. Ollama 조회 2회(4초)와 claude auth status(0.4초)가
#: 매번 도는 걸 막는다. 체크박스 토글 때마다 5초씩 멈추던 원인.
_CACHE: dict = {"at": 0.0, "data": None}
CACHE_SEC = 60


def status(with_account: bool = True, fresh: bool = False) -> dict:
    """누가 쓸 수 있고, 어떤 계정으로 로그인돼 있는지 확인한다.

    느린 조회(Ollama HTTP, CLI 서브프로세스)라 60초 캐시한다.
    로그인 직후처럼 즉시 반영이 필요하면 fresh=True.
    """
    if with_account and not fresh and _CACHE["data"] is not None:
        if time.time() - _CACHE["at"] < CACHE_SEC:
            # 모델 설정은 실시간이어야 하므로 캐시 위에 덮어쓴다
            data = _CACHE["data"]
            for k, m in MEMBERS.items():
                if k in data:
                    data[k]["model"] = m.model or ""
            return data
    out = _status_now(with_account)
    if with_account:
        _CACHE.update(at=time.time(), data=out)
    return out


def _status_now(with_account: bool = True) -> dict:
    out = {}
    for k, m in MEMBERS.items():
        row = {
            "name": m.name, "cli": m.cli, "ready": m.available,
            "path": _which(m.cli) or "",
            "model": m.model or "",
            "models": [{"id": i, "label": l} for i, l in m.models],
        }
        # available=False 여도 account()가 정확한 사유를 말해 준다
        # (CLI 없음 / 키 없음 / 서버 꺼짐). 덮어쓰지 않는다.
        row["account"] = m.account() if with_account else {
            "loggedIn": False, "reason": ""
        }
        out[k] = row
    return out


def set_model(key: str, model: str) -> bool:
    """참여자의 모델을 바꾼다. 목록에 없는 값은 거부한다.

    주의: MEMBERS 는 전역이라 탭을 여러 개 열면 설정을 공유한다.
    1인용 도구라 그대로 두되, 진행 중인 대화에는 다음 턴부터 적용된다.
    """
    m = MEMBERS.get(key)
    if not m or model not in {i for i, _ in m.models}:
        return False
    m.model = model or None
    _CACHE["data"] = None        # 화면에 바로 반영되게 캐시를 비운다
    return True
