#!/usr/bin/env python3
"""옛 버전(0.10 미만) 사용자가 업데이트 충돌을 고를 수 있게 하는 일회성 선택 화면이에요.

설치기(install.sh)가 충돌을 찾았는데 실행 중인 앱에 선택 화면이 없을 때만 열려요.
이 프로그램은 127.0.0.1에서만 듣고, 설치를 대신하지 않아요. 선택이 끝나면 새 설치기를 다시 실행해요.
종료 코드: 0 설치 성공 / 3 보류·취소·시간 만료·화면 닫힘(설치는 그대로)
"""
import argparse
import html
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import office_updates as updates   # noqa: E402  (같은 묶음의 점검 보관 모듈을 그대로 써요)

ACTIONS = {"keep", "merge", "verify", "upstream", "merged", "ping"}
IDLE_LIMIT = int(os.environ.get("OFFICE_BRIDGE_IDLE", "60"))            # 화면이 닫혔다고 보는 핑 공백(초)
FIRST_PING_LIMIT = int(os.environ.get("OFFICE_BRIDGE_FIRST_PING", "120"))   # 브라우저가 처음 열릴 때까지 기다리는 시간(초)

PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Office 업데이트</title>
<style>
body{font-family:'Noto Sans KR','Malgun Gothic','Apple SD Gothic Neo','Noto Sans CJK KR',system-ui,sans-serif;max-width:640px;margin:32px auto;padding:0 16px;color:#222;word-break:keep-all;text-wrap:pretty;line-height:1.6}
h1{font-family:'Gothic A1','Malgun Gothic','Apple SD Gothic Neo','Noto Sans CJK KR',system-ui,sans-serif;font-size:22px;margin:0 0 8px}
.box{border:1px solid #d8dbe0;border-radius:12px;padding:16px;margin:16px 0}
button{font:inherit;padding:10px 14px;border-radius:10px;border:1px solid #c5c9d0;background:#fff;cursor:pointer;margin:4px 6px 4px 0}
button.main{background:#3182f6;color:#fff;border-color:#3182f6}
button:disabled{opacity:.45;cursor:not-allowed}
.msg{background:#f2f4f6;border-radius:10px;padding:10px 12px;margin:12px 0}
.err{background:#fff0f0}
small{color:#666}
@media(prefers-color-scheme:dark){body{background:#17181c;color:#eee}.box{border-color:#3a3d44}button{background:#24262b;color:#eee;border-color:#3a3d44}.msg{background:#24262b}.err{background:#3a2326}small{color:#aaa}}
</style>
<h1>내 수정과 새 버전이 겹쳐요</h1>
<div id="root">불러오는 중이에요…</div>
<script>
const base=location.pathname.replace(/\\/$/,'');
let view=null,busy=false,confirmed=false;
async function call(action,extra){
  const res=await fetch(base+'/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,...extra})});
  return res.json();
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function render(){
  const v=view; if(!v){return}
  const root=document.getElementById('root');
  if(v.finished){root.innerHTML='<div class="msg">'+esc(v.finished)+'</div><small>이 창은 닫아도 돼요.</small>';return}
  let h='<div class="box"><b>겹치는 부분</b><br>'+(v.labels.length?v.labels.map(esc).join(' · '):'파일 상태 확인이 필요해요')+'<br><small>새 버전 '+esc(v.version||'')+' · 설치는 아직 바뀌지 않았어요</small></div>';
  if(v.message)h+='<div class="msg '+(v.error?'err':'')+'">'+esc(v.message)+'</div>';
  const off=(v.working||busy)?' disabled':'';
  h+='<div class="box"><b>어떻게 할까요?</b><br>';
  h+='<button onclick="act(\\'keep\\')"'+(busy?' disabled':'')+'>내 수정 유지</button> <small>업데이트를 미루고 지금 버전을 그대로 써요.</small><br>';
  h+='<button onclick="act(\\'merge\\')"'+(v.can_merge&&!off?'':' disabled')+'>사본에서 합치기</button> <small>'+esc(v.merge_note)+'</small><br>';
  if(v.can_verify)h+='<button onclick="act(\\'verify\\')"'+off+'>합친 결과 확인</button> <small>에이전트가 끝내면 눌러요.</small><br>';
  if(v.can_apply_merged)h+='<button class="main" onclick="act(\\'merged\\')"'+off+'>합친 사본 적용</button> <small>검사를 통과했어요.</small><br>';
  h+='<label><input type="checkbox" id="ok" onchange="confirmed=this.checked"'+(confirmed?' checked':'')+'> 겹치는 내 수정이 새 버전으로 바뀌는 걸 확인했어요.</label><br>';
  h+='<button onclick="act(\\'upstream\\')"'+(v.can_upstream&&!off?'':' disabled')+'>새 버전 사용</button> <small>내 수정은 백업해 두고, 겹치지 않는 수정은 그대로 둬요.</small></div>';
  root.innerHTML=h;
}
async function refresh(){ try{ view=await (await fetch(base+'/state')).json(); render(); }catch(e){} }
async function act(action){
  const sure=confirmed===true;
  busy=true; render();
  const r=await call(action,{confirmed: sure});
  busy=false; await refresh(); if(r.error){view.message=r.error;view.error=true;render()}
}
refresh(); setInterval(refresh,3000); setInterval(()=>fetch(base+'/act',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'ping'})}),5000);
</script></html>"""


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


SERVER_MISSING = "실행 중인 Office 서버를 확인하지 못해서 지금은 적용하지 않아요. Office를 다시 켠 뒤 업데이트를 다시 눌러요."


class Server:
    """실행 중인 옛 Office 서버의 로컬 API. 없으면 합치기를 끄고 이유를 보여 줘요."""

    def __init__(self, data_dirs):
        self.info = None
        self.data_dir = None
        for folder in data_dirs:
            try:
                info = json.loads((Path(folder) / "server.json").read_text())
                port = int(info["port"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if pid_alive(info.get("pid")):
                self.info = {"pid": info["pid"], "port": port}
                self.data_dir = Path(folder)      # 실제로 살아 있는 서버가 쓰는 폴더
                break

    def call(self, path, payload=None):
        if not self.info:
            raise RuntimeError("실행 중인 Office 서버를 찾지 못했어요.")
        url = "http://127.0.0.1:%d%s" % (self.info["port"], path)
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read()).get("error", "")
            except ValueError:
                message = ""
            raise RuntimeError(message or "Office 서버가 요청을 받지 않았어요.")
        except (OSError, ValueError):
            raise RuntimeError("Office 서버와 연결하지 못했어요.")

    def state(self, session=None):
        return self.call("/api/state" + ("?session=" + session if session else ""))

    def busy(self):
        state = self.state()
        return any(row.get("busy") for row in state.get("sessions", []))

    def session_ids(self):
        return [row["id"] for row in self.state().get("sessions", []) if not row.get("deleted")]

    def lead(self, session=None):
        """합치기를 맡길 에이전트. 서버가 알려 주는 상태(멈춤·로그인 오류)만 보고 골라요(느린 CLI 확인은 하지 않아요).
        (이름 또는 None, 맡길 수 없는 이유)"""
        agents = self.state(session).get("agents", {})
        logged_out = [n for n, row in agents.items() if row.get("error_kind") == "auth"]
        stopped = [n for n, row in agents.items() if row.get("paused") and n not in logged_out]
        for name in ("claude", "codex"):
            row = agents.get(name)
            if row and name not in logged_out and name not in stopped:
                return name, ""
        if logged_out and len(logged_out) == len(agents):
            return None, "에이전트가 모두 로그아웃돼 있어요. Office에서 로그인한 뒤 눌러요."
        return None, "합칠 수 있는 에이전트가 없어요. 멈춘 에이전트를 재개하거나 로그인한 뒤 눌러요."

def process_table():
    """{pid: (ppid, 명령)} — /proc 없이 ps만 써서 맥에서도 같아요."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True, timeout=20).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def subtree(table, roots):
    found, todo = set(), list(roots)
    while todo:
        pid = todo.pop()
        if pid in found:
            continue
        found.add(pid)
        todo.extend(p for p, (parent, _) in table.items() if parent == pid)
    return found


class Bridge:
    def __init__(self, args):
        self.args = args
        self.token = secrets.token_urlsafe(24)
        self.prefix = str(Path(args.prefix).resolve())
        self.workspace = args.workspace
        dirs = []
        if os.environ.get("OFFICE_DIR"):
            dirs.append(Path(os.environ["OFFICE_DIR"]) / ".office")
        dirs.append(Path(self.workspace) / ".office")
        self.legacy_parent = bool(args.legacy_parent)
        self.server = Server(dirs)
        self.data = self.server.data_dir or dirs[-1]
        self.store = updates.ReviewStore(self.data / "update-review")
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.exit_code = 3
        self.reason = ""
        self.stdout = ""
        self.message = ""
        self.error = False
        self.installing = False
        self.review = None
        self.last_ping = None
        self.started = time.time()
        self.port = None

    # ---- 상태
    def view(self):
        saved = self.store.load(self.review)
        view = self.store.view() or {}
        working = False
        if self.server.info:
            try:
                working = self.server.busy()
            except RuntimeError:
                working = False
        merge_note = "에이전트가 사본에서 합쳐요. 설치는 그대로예요."
        can_merge = bool(view.get("can_merge"))
        if not self.server.info:
            can_merge, merge_note = False, "합치려면 Office를 켜 둬야 해요. 지금은 Office 서버를 찾지 못했어요."
        elif view.get("merge_job"):
            if self.merge_finished(saved):
                can_merge, merge_note = True, "지난 합치기가 끝났어요. 처음부터 다시 합칠 수 있어요."
            else:
                merge_note = "에이전트가 합치는 중이에요."
        if can_merge and self.server.info:
            try:
                lead, why = self.server.lead()
            except RuntimeError:
                lead, why = "claude", ""
            if not lead:
                can_merge, merge_note = False, why
        if view.get("state") == "blocked":
            can_merge = False
        can_upstream = view.get("state") in {"conflict", "ready", "failed"} and bool(view.get("resolvable"))
        if self.legacy_parent and not self.server.info:
            can_upstream = False
            self.message = self.message or SERVER_MISSING
        view.update(can_merge=can_merge, merge_note=merge_note, can_upstream=can_upstream, working=working or self.installing,
                    message=self.message or view.get("message") or ("에이전트가 일하는 중이면 적용이 미뤄져요." if working else ""),
                    error=self.error, finished=None if not self.done.is_set() else (self.message or "끝났어요."))
        return view

    def reusable(self, package):
        """지난번에 점검하고 골라 둔 같은 업데이트가 있으면 그대로 이어가요(합치기 사본·선택 보존)."""
        try:
            saved = self.store.load()
            if (not saved or saved.get("prefix") != self.prefix or saved.get("state") not in {"conflict", "merging", "ready", "failed"}
                    or updates.tree_hashes(package) != saved.get("package_hashes")):
                return None
            self.store.recheck(saved)
            return saved
        except (updates.ReviewError, OSError, ValueError, KeyError):
            return None

    def merge_finished(self, saved):
        """맡겼던 합치기 작업이 끝났거나 취소됐는지(실패해도 다시 맡길 수 있게)."""
        try:
            jobs = self.server.state(saved["merge_session"]).get("jobs", [])
        except (RuntimeError, KeyError, TypeError):
            return False
        job = next((j for j in jobs if j.get("id") == saved.get("merge_job")), None)
        return bool(job) and (job.get("status") == "done" or bool(job.get("cancelled")))

    def need_server(self):
        if self.legacy_parent and not self.server.info:
            raise RuntimeError(SERVER_MISSING)

    def finish(self, code, reason="", stdout=""):
        self.exit_code, self.reason, self.stdout = code, reason, stdout
        self.message = reason or "업데이트를 마쳤어요."
        self.done.set()

    # ---- 동작
    def act(self, action, confirmed):
        if action == "ping":
            self.last_ping = time.time()
            return {"ok": True}
        if not self.lock.acquire(blocking=False):
            return 409, {"error": "이미 처리하고 있어요."}
        try:
            self.error = False
            self.message = ""
            if action == "keep":
                try:
                    self.store.defer(self.review)
                except updates.ReviewError:
                    pass          # 합치는 중에도 보류할 수 있어야 해요. 설치는 어차피 바뀌지 않아요
                self.finish(3, "업데이트를 보류했어요. 내 수정과 현재 버전은 그대로예요.")
                return {"ok": True}
            if action == "merge":
                return self.merge()
            if action == "verify":
                return self.verify()
            if action in ("upstream", "merged"):
                if action == "upstream" and confirmed is not True:
                    return 400, {"error": "겹치는 내 수정이 새 버전으로 바뀌는지 확인한 뒤 눌러요."}
                return self.install(action)
            return 400, {"error": "알 수 없는 선택이에요."}
        except (updates.ReviewError, RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            self.error, self.message = True, str(error)
            return 400, {"error": str(error)}
        finally:
            self.lock.release()

    def merge(self):
        if not self.server.info:
            raise RuntimeError("합치려면 Office를 켜 둬야 해요. 지금은 Office 서버를 찾지 못했어요.")
        current = self.store.load(self.review)
        retry = bool(current and current.get("merge_job"))
        if retry and not self.merge_finished(current):
            raise RuntimeError("에이전트가 아직 합치는 중이에요. 끝난 뒤 다시 눌러요.")
        session = self.server.state().get("session")
        lead, why = self.server.lead(session)
        if not lead:
            raise RuntimeError(why)
        saved = self.store.prepare_merge(self.review, retry=retry)
        try:
            prompt = self.store.merge_prompt(saved)
            self.server.call("/api", {"action": "job", "session": session, "text": prompt, "lead": lead, "verification": "thorough"})
            jobs = self.server.state(session).get("jobs", [])
            job = next((j for j in reversed(jobs) if j.get("text") == prompt), None)
            if not job:
                raise RuntimeError("에이전트에게 합치기를 맡기지 못했어요.")
        except RuntimeError:
            shutil_rmtree(self.store.candidate(saved))     # 맡기지 못했으면 사본을 지워 다시 시도할 수 있게 해요
            self.store.save(saved, state="conflict")
            raise
        self.store.attach_job(saved, session, job["id"])
        self.message = "에이전트가 사본에서 합치고 있어요. 끝나면 결과 확인을 눌러요."
        return {"ok": True}

    def verify(self):
        saved = self.store.load(self.review)
        if not saved or not saved.get("merge_job"):
            raise RuntimeError("먼저 사본 합치기를 시작하세요.")
        if not self.server.info:
            raise RuntimeError("Office 서버를 찾지 못해서 에이전트 작업 상태를 확인할 수 없어요.")
        jobs = self.server.state(saved["merge_session"]).get("jobs", [])
        job = next((j for j in jobs if j.get("id") == saved["merge_job"]), None)
        if not job or job.get("status") != "done" or job.get("cancelled"):
            raise RuntimeError("에이전트의 합치기가 아직 끝나지 않았어요.")
        if self.server.busy():
            raise RuntimeError("에이전트가 일하는 중이에요. 끝난 뒤 눌러요.")
        self.store.verify_candidate(self.review)
        self.message = "합친 사본의 기본 실행 검사를 통과했어요. 적용할지 골라요."
        return {"ok": True}

    def live_office(self):
        """지금 이 설치의 Office가 켜져 있는지. 서버 기록이 없어도 실행 중인 앱 프로세스가 있으면 켜진 걸로 봐요."""
        if self.server.info:
            return True
        needle = str(Path(self.prefix) / "app" / "office.py")
        try:
            table = process_table()
        except (OSError, subprocess.SubprocessError):
            return True          # 확인하지 못하면 켜져 있다고 보고 적용하지 않아요
        return any(needle in command and "office_update_bridge" not in command and "install.sh" not in command
                   for pid, (parent, command) in table.items() if pid != os.getpid())

    def apply_command(self, saved):
        import shlex
        return "bash %s --apply --review --prefix %s --workspace %s" % (
            shlex.quote(str(self.store.package(saved) / "install.sh")), shlex.quote(self.prefix), shlex.quote(self.workspace))

    def install(self, choice):
        self.need_server()
        saved, command = self.store.install_command(self.review, choice)
        self.store.validate_install(saved, choice)
        if self.live_office():
            # 옛 서버에는 새 작업을 막는 장치가 없어요. 켜져 있는 동안은 파일을 바꾸지 않고, 고른 방법만 저장해요.
            self.store.save(saved, pending_choice=choice)
            self.finish(3, "고른 방법을 저장했어요. Office를 닫은 뒤 터미널에서 `%s` 를 실행해 적용하세요. 내 수정과 현재 버전은 아직 그대로예요." % self.apply_command(saved))
            return {"ok": True}
        self.installing = True
        self.message = "설치하고 있어요. 창을 닫지 마세요."
        try:
            threading.Thread(target=self.run_install, args=(command,), daemon=True).start()
        except RuntimeError:
            self.installing = False
            raise
        return {"ok": True}

    def run_install(self, command):
        try:
            env = dict(os.environ, OFFICE_BRIDGE_ACTIVE="1")
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900, env=env)
        except (OSError, subprocess.SubprocessError):
            self.installing = False
            self.error, self.message = True, "설치를 마치지 못했어요. 기존 설치는 그대로일 수 있어요. 다시 점검하세요."
            return
        self.installing = False
        if result.returncode:
            text = updates_failure_text(result.stderr or result.stdout)
            self.error, self.message = True, text
            return
        self.finish(0, "", result.stdout)


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def updates_failure_text(output):
    lines = [x.strip() for x in (output or "").splitlines() if x.strip()]
    reasons = [x[2:].strip() for x in lines if x.startswith("- ")]
    return " ".join(reasons + ["기존 설치는 그대로예요."]) if reasons else ("설치에 실패했어요. " + (lines[-1] if lines else "")).strip()


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OfficeBridge"

        def log_message(self, *args):
            pass

        def allowed(self):
            hosts = {"127.0.0.1:%d" % bridge.port, "localhost:%d" % bridge.port}
            if self.headers.get("Host") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if self.command == "POST":
                return origin in {"http://" + h for h in hosts}
            return not origin or origin in {"http://" + h for h in hosts}

        def reply(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def route(self):
            path = self.path.split("?", 1)[0]
            prefix = "/" + bridge.token
            if path == prefix or path == prefix + "/":
                return "page"
            if path == prefix + "/state":
                return "state"
            if path == prefix + "/act":
                return "act"
            return None

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, {"error": "접근할 수 없어요."})
            route = self.route()
            if route == "page":
                return self.reply(200, PAGE, "text/html")
            if route == "state":
                try:
                    return self.reply(200, bridge.view())
                except (updates.ReviewError, OSError, ValueError) as error:
                    return self.reply(200, {"labels": [], "message": str(error), "error": True, "finished": None})
            self.reply(404, {"error": "주소를 찾을 수 없어요."})

        def do_POST(self):
            if not self.allowed() or self.route() != "act":
                return self.reply(403 if not self.allowed() else 404, {"error": "접근할 수 없어요."})
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= 4096:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                action = payload.get("action")
                if action not in ACTIONS:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                return self.reply(400, {"error": "요청 형식이 올바르지 않아요."})
            result = bridge.act(action, payload.get("confirmed"))
            if isinstance(result, tuple):
                return self.reply(result[0], result[1])
            self.reply(200, result)
    return Handler


def open_browser(url):
    """WSL은 윈도우 기본 브라우저, 맥은 open, 그 밖에는 xdg-open·webbrowser 순서로 열어요."""
    candidates = []
    if os.environ.get("WSL_DISTRO_NAME") or Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
        candidates.append(["powershell.exe", "-NoProfile", "-Command", "Start-Process '%s'" % url])
        candidates.append(["cmd.exe", "/c", "start", "", url])
    if sys.platform == "darwin":
        candidates.append(["open", url])
    else:
        candidates.append(["xdg-open", url])
    for command in candidates:
        try:
            if subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    try:
        return bool(webbrowser.open(url))
    except webbrowser.Error:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--package", required=True, help="install.sh와 payload/가 든 묶음 폴더")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--legacy-parent", action="store_true", help="옛 Office 앱이 설치기를 불렀어요(서버를 확인하지 못하면 적용하지 않아요)")
    args = parser.parse_args(argv)

    def stop(reason):
        sys.stderr.write("설치를 멈췄어요. 기존 설치는 그대로예요.\n- %s\n" % reason)
        return 3

    try:
        bridge = Bridge(args)
        saved = bridge.reusable(Path(args.package))
        if saved is None:
            saved = bridge.store.create(Path(args.package), bridge.prefix, bridge.workspace)
        bridge.review = saved["id"]
        if saved.get("pending_choice"):
            bridge.message = "지난번에 고른 방법을 저장해 뒀어요. 같은 방법을 다시 눌러 적용하세요."
    except (updates.ReviewError, OSError, ValueError, subprocess.SubprocessError) as error:
        return stop("업데이트 선택 화면을 준비하지 못했어요: %s" % error)
    if saved["state"] == "ready" and not saved["plan"].get("conflicts") and not saved["plan"].get("blocked"):
        return stop("선택이 필요한 겹침이 없어요.")
    if saved["state"] == "blocked":
        return stop("자동으로 풀 수 없는 변경이 있어요: " + " ".join(saved["plan"].get("blocked", [])))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(bridge))
    bridge.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/%s/" % (bridge.port, bridge.token)
    announce = os.environ.get("OFFICE_BRIDGE_ANNOUNCE")
    if announce:      # 시험용: 가짜 브라우저가 주소를 읽어 가요
        Path(announce).write_text(url)
    elif not args.no_browser and not open_browser(url):
        httpd.shutdown()
        return stop("브라우저를 열지 못했어요. 터미널에서 `bash install.sh --review`를 직접 실행해 보세요.")
    if (sys.stderr.isatty() or sys.stdout.isatty()) and not announce:
        sys.stderr.write("선택 화면: %s\n" % url)
    deadline = time.time() + args.timeout
    while not bridge.done.wait(0.5):
        now = time.time()
        if now > deadline and not bridge.installing:
            bridge.finish(3, "선택 시간이 지나 업데이트를 미뤘어요. 내 수정과 현재 버전은 그대로예요.")
        elif bridge.last_ping is None and now - bridge.started > FIRST_PING_LIMIT and not bridge.installing:
            bridge.finish(3, "선택 화면이 열리지 않아 업데이트를 미뤘어요. 내 수정과 현재 버전은 그대로예요.")
        elif bridge.last_ping is not None and now - bridge.last_ping > IDLE_LIMIT and not bridge.installing:
            bridge.finish(3, "선택 화면이 닫혀서 업데이트를 미뤘어요. 내 수정과 현재 버전은 그대로예요.")
    time.sleep(0.7)    # 화면이 마지막 상태를 한 번 받아 가게 해요
    httpd.shutdown()
    if bridge.exit_code == 0:
        sys.stdout.write(bridge.stdout)
        return 0
    return stop(bridge.reason)


if __name__ == "__main__":
    sys.exit(main())
