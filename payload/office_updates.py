"""업데이트 점검 결과와 사용자 선택을 설치본 밖에 보관해요.

설치·병합 정책은 office_customizations.py가 맡아요. 이 모듈은 받은 묶음과
후보 사본을 분리하고, 같은 점검에 대한 선택만 적용하도록 확인해요.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


class ReviewError(Exception):
    pass


ACTIVE_STATES = {"conflict", "blocked", "ready", "merging", "checking", "applying", "failed"}
LABELS = {"office.py": "앱 동작", "office.html": "화면", "office_updates.py": "업데이트 화면과 처리",
          "office_customizations.py": "수정 보존", "office.ico": "앱 아이콘", "office-icon.png": "앱 아이콘",
          "VERSION": "버전 표시"}


def atomic_json(path, data):
    path = Path(path)
    if path.is_symlink():
        raise ReviewError("점검 기록 경로를 확인할 수 없어요. 기존 설치를 유지해요.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as output:
            os.chmod(temp, 0o600)
            json.dump(data, output, ensure_ascii=False, allow_nan=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def tree_hashes(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ReviewError("업데이트 사본을 찾지 못했어요. 다시 점검하세요.")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ReviewError("업데이트 사본에 확인할 수 없는 경로가 있어요.")
        if path.is_file() and "__pycache__" not in path.parts:
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def installed_hashes(root):
    """관리 밖의 사용자 링크는 따라가지 않고 변경 여부만 기록해요."""
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if "__pycache__" in path.parts:
            continue
        name = str(path.relative_to(root))
        if path.is_symlink():
            result[name] = "link:" + os.readlink(path)
        elif path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            result[name] = "special"
    return result


def clean_environment(home):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("OFFICE_") and key not in {"PYTHONPATH", "PYTHONHOME", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION"}}
    env.update(HOME=str(home), OFFICE_DIR=str(home / "workspace"), CODEX_HOME=str(home / ".codex"),
               CLAUDE_CONFIG_DIR=str(home / ".claude"), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), PYTHONDONTWRITEBYTECODE="1")
    return env


def smoke_candidate(candidate):
    """후보를 격리된 작업·설정 폴더에서 불러 기본 API와 CLI를 확인해요."""
    candidate = Path(candidate)
    if not (candidate / "office.py").is_file():
        raise ReviewError("합친 사본에 앱 실행 파일이 없어요.")
    for path in candidate.rglob("*.py"):
        if path.is_symlink():
            raise ReviewError("합친 사본에 링크가 있어 적용하지 않았어요.")
        try:
            compile(path.read_bytes(), str(path), "exec")
        except (SyntaxError, ValueError) as error:
            raise ReviewError("합친 코드의 문법을 확인하지 못했어요. 기존 설치는 그대로예요.") from error
    with tempfile.TemporaryDirectory(prefix="office-update-smoke-") as folder:
        home = Path(folder)
        (home / "workspace").mkdir()
        env = clean_environment(home)
        expected_version = (candidate / "VERSION").read_text().strip()
        for argument in ("--help", "--version"):
            result = subprocess.run([sys.executable, str(candidate / "office.py"), argument],
                                    cwd=home, env=env, capture_output=True, timeout=30)
            output = result.stdout.decode(errors="replace").strip()
            if result.returncode or not output or (argument == "--version" and output != "Office " + expected_version):
                raise ReviewError("합친 사본의 기본 실행 검사에 실패했어요. 기존 설치는 그대로예요.")
        probe = '''import importlib.util, json, pathlib, sys
source=pathlib.Path(sys.argv[1])
spec=importlib.util.spec_from_file_location("_office_update_probe",source)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.office_version()
module.D.mkdir(parents=True,exist_ok=True)
office=module.Office()
assert office.handle({"action":"ui_settings","theme":"dark"})["ui"]["theme"]=="dark"
assert json.loads((module.D/"ui-settings.json").read_text())["theme"]=="dark"
print(sys.argv[2],flush=True)
'''
        marker = "OFFICE_UPDATE_PROBE_OK:" + uuid.uuid4().hex
        result = subprocess.run([sys.executable, "-I", "-c", probe, str(candidate / "office.py"), marker],
                                cwd=home, env=env, capture_output=True, timeout=30)
        if result.returncode or marker not in result.stdout.decode(errors="replace").splitlines():
            raise ReviewError("합친 사본의 설정 저장 검사에 실패했어요. 기존 설치는 그대로예요.")
    return ["Python 문법", "도움말과 버전 실행", "설정 API와 저장"]


class ReviewStore:
    def __init__(self, root):
        self.root = Path(root)
        self.record = self.root / "current.json"

    def _directory(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise ReviewError("업데이트 선택이 오래됐어요. 다시 점검하세요.")
        directory = self.root / token
        if self.root.is_symlink() or directory.is_symlink():
            raise ReviewError("점검 폴더를 확인하지 못했어요. 기존 설치를 유지해요.")
        return directory

    def load(self, token=None):
        if self.root.is_symlink() or self.record.is_symlink():
            raise ReviewError("점검 기록을 확인하지 못했어요. 기존 설치를 유지해요.")
        try:
            saved = json.loads(self.record.read_text())
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as error:
            raise ReviewError("점검 기록을 읽지 못했어요. 다시 점검하세요.") from error
        if not isinstance(saved, dict) or saved.get("schema") != 1:
            raise ReviewError("점검 기록 형식이 달라요. 다시 점검하세요.")
        self._directory(saved.get("id"))
        if token is not None and saved["id"] != token:
            raise ReviewError("다른 업데이트의 선택이에요. 다시 점검하세요.")
        if saved.get("state") not in ACTIVE_STATES | {"deferred", "applied", "restored"}:
            raise ReviewError("점검 상태를 확인하지 못했어요. 다시 점검하세요.")
        return saved

    def save(self, saved, **changes):
        self._directory(saved.get("id"))
        saved = {**saved, **changes, "updated_at": time.time()}
        atomic_json(self._directory(saved["id"]) / "review.json", saved)
        atomic_json(self.record, saved)
        return saved

    def package(self, saved):
        return self._directory(saved["id"]) / "package"

    def candidate(self, saved):
        return self._directory(saved["id"]) / "candidate"

    def plan_path(self, saved):
        return self._directory(saved["id"]) / "plan.json"

    def verify_package(self, saved):
        if tree_hashes(self.package(saved)) != saved.get("package_hashes"):
            raise ReviewError("점검한 설치 묶음이 바뀌었어요. 다시 점검하세요.")

    def helper_plan(self, saved, output=None, candidate=False):
        self.verify_package(saved)
        package = self.package(saved)
        command = [sys.executable, str(package / "payload/office_customizations.py"), "--plan",
                   "--prefix", saved["prefix"], "--incoming", str(package / "payload"),
                   "--output", str(output or self.plan_path(saved))]
        if candidate:
            command += ["--candidate-dir", str(self.candidate(saved) if candidate is True else candidate)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        destination = Path(output or self.plan_path(saved))
        if result.returncode not in (0, 3) or not destination.is_file() or destination.is_symlink():
            raise ReviewError("사용자 수정 점검에 실패했어요. 기존 설치는 그대로예요.")
        try:
            plan = json.loads(destination.read_text())
        except (OSError, ValueError) as error:
            raise ReviewError("수정 점검 결과를 읽지 못했어요. 기존 설치는 그대로예요.") from error
        if not isinstance(plan, dict) or plan.get("schema") != 1 or not isinstance(plan.get("id"), str):
            raise ReviewError("수정 점검 결과가 올바르지 않아요. 기존 설치는 그대로예요.")
        if not isinstance(plan.get("conflicts"), list) or not isinstance(plan.get("blocked"), list):
            raise ReviewError("수정 점검 결과가 올바르지 않아요. 기존 설치는 그대로예요.")
        return plan

    def create(self, package, prefix, workspace):
        if self.root.is_symlink():
            raise ReviewError("점검 폴더가 링크라 업데이트를 멈췄어요.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = uuid.uuid4().hex
        directory = self._directory(token)
        directory.mkdir(mode=0o700)
        (directory / ".office-update-review").write_text("office-update-review-v1\n")
        try:
            tree_hashes(package)
            shutil.copytree(package, directory / "package")
            saved = {"schema": 1, "id": token, "prefix": str(Path(prefix).resolve()),
                     "workspace": str(Path(workspace).resolve()), "state": "checking", "created_at": time.time(),
                     "package_hashes": tree_hashes(directory / "package")}
            plan = self.helper_plan(saved)
            saved.update(plan=plan, version=plan.get("version"), plan_id=plan["id"])
            state = "conflict" if plan["blocked"] and plan.get("resolvable") else "blocked" if plan["blocked"] else "conflict" if plan["conflicts"] else "ready"
            result = self.save(saved, state=state)
            try:
                self.prune(result["id"])
            except (OSError, UnicodeError):
                pass
            return result
        except Exception:
            shutil.rmtree(directory)
            raise

    def prune(self, active, keep=3):
        """이 모듈이 만든 옛 다운로드만 정리하고 합치기 원고는 보존해요."""
        entries = []
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
                continue
            marker = directory / ".office-update-review"
            if marker.is_symlink() or not marker.is_file() or marker.read_text() != "office-update-review-v1\n":
                continue
            if (directory / "candidate").exists() or any(directory.glob("previous-candidate-*")):
                continue
            entries.append(directory)
        entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for directory in entries[keep:]:
            if directory.name != active:
                shutil.rmtree(directory, ignore_errors=True)

    def recheck(self, saved):
        destination = self._directory(saved["id"]) / "recheck.json"
        try:
            plan = self.helper_plan(saved, output=destination)
            expected = saved.get("plan", {})
            fields = ("current", "incoming", "files", "blocked", "problem_files", "resolvable")
            if plan["id"] != saved["plan_id"] or any(plan.get(key) != expected.get(key) for key in fields):
                raise ReviewError("점검 뒤 파일이 바뀌었어요. 다시 점검하세요.")
            return plan
        finally:
            destination.unlink(missing_ok=True)

    def prepare_merge(self, token, retry=False):
        saved = self.load(token)
        if retry and saved and saved.get("merge_job"):
            self.recheck(saved)
            candidate = self.candidate(saved)
            if candidate.exists():
                candidate.rename(self._directory(saved["id"]) / ("previous-candidate-" + uuid.uuid4().hex))
            saved = self.save(saved, state="conflict", merge_job=None, merge_session=None, candidate_hashes=None, message=None)
        if saved is None or saved["state"] not in {"conflict", "ready", "failed"} or not saved["plan"].get("conflicts") or not saved["plan"].get("resolvable", not saved["plan"].get("blocked")) or any(row.get("status") == "blocked" for row in saved["plan"].get("files", [])):
            raise ReviewError("이 업데이트는 사본에서 합칠 수 없어요. 점검 결과를 확인하세요.")
        self.recheck(saved)
        candidate = self.candidate(saved)
        if candidate.exists():
            raise ReviewError("이미 만든 합치기 사본이 있어요. 먼저 결과를 확인하세요.")
        plan = self.helper_plan(saved, candidate=True)
        if plan["id"] != saved["plan_id"] or not candidate.is_dir():
            raise ReviewError("합칠 사본을 만들지 못했어요. 다시 점검하세요.")
        return self.save(saved, state="merging", checks=[], candidate_hashes=None, candidate_baseline=tree_hashes(candidate / "files"))

    def merge_prompt(self, saved):
        directory = self._directory(saved["id"])
        return ("업데이트 충돌을 사본에서 합쳐 줘. 실제 설치는 건드리지 마.\n"
                f"대상 버전: {saved.get('version')}\n점검 결과: {self.plan_path(saved)}\n"
                f"새 버전 원본: {self.package(saved) / 'payload'}\n"
                f"현재 수정과 예전 원본의 사본: {self.candidate(saved) / 'conflicts'}\n"
                f"수정할 후보 사본: {self.candidate(saved) / 'files'}\n"
                f"충돌별 3종 원본: {self.candidate(saved) / 'conflicts'}\n"
                f"충돌 파일: {', '.join(saved['plan']['conflicts'])}\n"
                "점검 결과에 적힌 충돌 파일만 후보 사본에서 수정해. 다른 파일의 변경이 필요하면 이유를 설명하고 자동 적용하지 마. "
                "기존 사용자 수정과 새 버전 동작을 모두 보존해. 의도가 양립할 수 없으면 임의로 한쪽을 버리지 말고 사용자에게 설명해. "
                "후보 수정 전 office reserve, /tmp 원고 수정 뒤 office write, 끝나면 release를 사용해. "
                "후보에서 해당 기능을 시험하고 결과를 보고해. 이 작업이 끝나도 설치하거나 서버를 재시작하지 마. "
                "사용자가 업데이트 창에서 결과 확인을 누르면 앱이 별도 검사를 하고 적용 여부를 고르게 할 거야.")

    def attach_job(self, saved, sid, jid):
        return self.save(saved, merge_session=sid, merge_job=jid)

    def verify_candidate(self, token):
        saved = self.load(token)
        if saved is None or saved["state"] not in {"merging", "ready", "failed"} or not saved.get("merge_job"):
            raise ReviewError("먼저 에이전트에게 사본 합치기를 맡기세요.")
        self.recheck(saved)
        candidate = self.candidate(saved) / "files"
        hashes = tree_hashes(candidate)
        incoming = saved["plan"]["incoming"]
        conflicts = set(saved["plan"]["conflicts"])
        # 새 파일을 슬쩍 추가하거나 서로 의존하는 비충돌 파일까지 바꾸지 않아요.
        if set(hashes) != set(incoming):
            raise ReviewError("합친 사본의 파일 목록이 바뀌었어요. 에이전트의 확인이 필요해요.")
        # 후보의 비충돌 부분은 엔진이 만든 내용을 그대로 유지해야 해요.
        baseline = saved.get("candidate_baseline")
        if baseline and any(hashes[name] != digest for name, digest in baseline.items() if name not in conflicts):
            raise ReviewError("충돌 밖의 파일도 바뀌었어요. 자동 적용하지 않았어요.")
        for name in conflicts:
            path = candidate / name
            if not path.is_file() or path.is_symlink():
                raise ReviewError("합친 파일을 찾지 못했어요.")
            data = path.read_bytes()
            if b"<<<<<<< " in data or b"\n=======\n" in data or b">>>>>>> " in data:
                raise ReviewError("아직 합치지 않은 부분이 있어요. 기존 설치는 그대로예요.")
        checks = smoke_candidate(candidate)
        if tree_hashes(candidate) != hashes:
            raise ReviewError("검사 중 합친 사본이 바뀌었어요. 다시 확인하세요.")
        return self.save(saved, state="ready", candidate_hashes=hashes, checks=checks)

    def defer(self, token):
        saved = self.load(token)
        if saved is None or saved["state"] not in {"conflict", "blocked", "ready", "failed", "deferred"}:
            raise ReviewError("처리 중에는 보류할 수 없어요. 끝난 뒤 다시 선택하세요.")
        return self.save(saved, state="deferred")

    def install_command(self, token, choice=None):
        saved = self.load(token)
        if saved is None or saved["state"] not in {"conflict", "ready", "failed"} or (saved["plan"].get("blocked") and not saved["plan"].get("resolvable")):
            raise ReviewError("이 점검 결과로는 업데이트할 수 없어요. 다시 점검하세요.")
        if choice not in (None, "upstream", "merged"):
            raise ReviewError("업데이트 선택이 올바르지 않아요.")
        if (saved["plan"]["conflicts"] or saved["plan"]["blocked"]) and choice is None:
            raise ReviewError("겹치는 수정의 처리 방법을 먼저 선택하세요.")
        self.recheck(saved)
        command = ["bash", str(self.package(saved) / "install.sh"), "--apply", "--skip-login", "--no-launch",
                   "--prefix", saved["prefix"], "--workspace", saved["workspace"],
                   "--review-plan", str(self.plan_path(saved))]
        if choice is not None:
            resolution = {"schema": 1, "plan_id": saved["plan_id"], "choice": choice}
            if choice == "merged":
                if saved["state"] != "ready" or not saved.get("candidate_hashes"):
                    raise ReviewError("합친 사본을 먼저 검사하세요.")
                if tree_hashes(self.candidate(saved) / "files") != saved["candidate_hashes"]:
                    raise ReviewError("검사 뒤 합친 사본이 바뀌었어요. 다시 확인하세요.")
                resolution.update(candidate_dir=str(self.candidate(saved)), candidate_sha={name: saved["candidate_hashes"][name] for name in saved["plan"]["conflicts"]})
            decision = self._directory(saved["id"]) / "resolution.json"
            atomic_json(decision, resolution)
            command += ["--resolution", str(decision)]
        return saved, command

    def validate_install(self, saved, choice=None):
        """실제로 설치할 전체 내용을 별도 사본에서 실행해요."""
        self.recheck(saved)
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=self._directory(saved["id"])) as folder:
            folder = Path(folder)
            plan = self.helper_plan(saved, output=folder / "plan.json", candidate=folder / "candidate")
            if plan["id"] != saved["plan_id"]:
                raise ReviewError("검사 중 설치 상태가 바뀌었어요. 다시 점검하세요.")
            files = folder / "candidate/files"
            problems = plan.get("problem_files", plan["conflicts"])
            if choice == "upstream":
                for name in problems:
                    shutil.copyfile(self.package(saved) / "payload" / name, files / name)
            elif choice == "merged":
                if tree_hashes(self.candidate(saved) / "files") != saved.get("candidate_hashes"):
                    raise ReviewError("합친 사본이 바뀌었어요. 다시 검사하세요.")
                for name in problems:
                    shutil.copyfile(self.candidate(saved) / "files" / name, files / name)
            elif problems:
                raise ReviewError("겹치는 변경을 먼저 선택하세요.")
            hashes = tree_hashes(files)
            if set(hashes) != set(plan["incoming"]):
                raise ReviewError("실행할 사본에 필요한 파일이 없어요. 기존 설치는 그대로예요.")
            checks = smoke_candidate(files)
            if tree_hashes(files) != hashes:
                raise ReviewError("검사 중 사본이 바뀌었어요. 다시 검사하세요.")
            return checks, hashes

    def installed(self, saved, output):
        line = next((row for row in reversed(output.splitlines()) if row.startswith("OFFICE_LOCAL:")), "")
        fields = dict(item.split("=", 1) for item in line.split() if "=" in item)
        structured = next((row[len("OFFICE_LOCAL_JSON: "):] for row in reversed(output.splitlines()) if row.startswith("OFFICE_LOCAL_JSON: ")), None)
        if structured is not None:
            try:
                value = json.loads(structured)
                if isinstance(value, dict): fields = value
            except ValueError:
                raise ReviewError("설치 결과를 확인하지 못했어요. 설치 위치를 다시 점검하세요.")
        backup = fields.get("backup")
        return self.save(saved, state="applied", backup=None if backup in (None, "-") else backup,
                         installed_at=time.time(), installed_hashes=installed_hashes(Path(saved["prefix"]) / "app"))

    def restore_command(self, token):
        saved = self.load(token)
        if saved is None or saved["state"] not in {"applied", "failed"} or not saved.get("backup"):
            raise ReviewError("되돌릴 업데이트 백업이 없어요.")
        backup = Path(saved["backup"])
        expected = Path(saved["prefix"]) / "backups"
        if backup.is_symlink() or not backup.is_dir() or backup.parent.resolve() != expected.resolve():
            raise ReviewError("되돌릴 백업 경로를 확인하지 못했어요.")
        if installed_hashes(Path(saved["prefix"]) / "app") != saved.get("installed_hashes"):
            raise ReviewError("업데이트 뒤 앱을 다시 고쳤어요. 덮어쓰지 않고 되돌리기를 멈췄어요.")
        self.verify_package(saved)
        return saved, ["bash", str(self.package(saved) / "install.sh"), "--restore-backup", str(backup),
                       "--prefix", saved["prefix"], "--workspace", saved["workspace"], "--skip-login", "--no-launch"]

    def view(self):
        saved = self.load()
        if not saved:
            return None
        plan = saved.get("plan", {})
        conflicts = plan.get("conflicts", [])
        labels = list(dict.fromkeys(LABELS.get(name, name) for name in conflicts))
        return {"id": saved["id"], "state": saved["state"], "version": saved.get("version"),
                "conflicts": conflicts, "labels": labels, "blocked": plan.get("blocked", []), "resolvable": bool(plan.get("resolvable")),
                "can_merge": bool(conflicts) and plan.get("resolvable") and not any(row.get("status") == "blocked" for row in plan.get("files", [])) and not saved.get("merge_job") and saved["state"] == "conflict",
                "can_verify": bool(saved.get("merge_job")) and saved["state"] in {"merging", "failed", "ready"},
                "can_apply_merged": saved["state"] == "ready" and bool(saved.get("candidate_hashes")),
                "can_restore": saved["state"] in {"applied", "failed"} and bool(saved.get("backup")),
                "merge_job": saved.get("merge_job"), "merge_session": saved.get("merge_session"), "merge_available": saved.get("merge_available"), "can_retry_merge": False,
                "checks": saved.get("checks", []), "message": saved.get("message")}
