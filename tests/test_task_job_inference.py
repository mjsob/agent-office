"""작업을 추가할 때 기존 작업의 요청을 이어가는지 확인해요.
실행: python3 -I tests/test_task_job_inference.py [payload/office.py 경로]
실제 작업판 대신 임시 상태만 사용해요.
"""
import argparse, atexit, importlib.util, json, os, shutil, sys, tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="office-task-job-")
atexit.register(shutil.rmtree, TMP)
for key in list(os.environ):
    if key.startswith("OFFICE_"):
        del os.environ[key]
os.environ.update(OFFICE_DIR=TMP, OFFICE_AS="codex", OFFICE_SESSION="t1", OFFICE_JOB="20")
src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
spec = importlib.util.spec_from_file_location("office_task_job_test", src)
o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)
d = o.sdir("t1")
d.mkdir(parents=True, exist_ok=True)
assert d.is_relative_to(TMP)

def reset():
    jobs = [{"id": i, "status": status, "lead": "codex", "text": str(i)}
            for i, status in [(20, "working"), (36, "working"), (37, "review"), (38, "done"), (39, "working")]]
    jobs[-1]["cancelled"] = True
    tasks = [{"id": i, "job": job, "title": f"작업{i}", "owner": "claude", "status": status,
              "note": "", "created_at": 1}
             for i, job, status in [(74, 36, "doing"), (186, 36, "done"), (194, 36, "done"),
                                    (92, 37, "todo"), (31, 37, "done"), (80, 38, "done"), (81, 39, "todo")]]
    with (d / "state.json").open("w") as f:
        json.dump({"jobs": jobs, "tasks": tasks, "msgs": [], "seq": 200}, f)
    os.environ["OFFICE_JOB"] = "20"
    os.environ.pop("OFFICE_DIRECT", None)

def load():
    with o.state(sid="t1") as s:
        return s

def add(title, expected, level=None):
    o.cmd_add(argparse.Namespace(text=title, to="claude", level=level))
    s = load()
    assert s["tasks"][-1]["job"] == expected, (title, s["tasks"][-1])
    assert s["msgs"][-1]["job"] == expected, (title, s["msgs"][-1])
    assert s["tasks"][-1]["owner"] == "claude"
    if level:
        assert s["tasks"][-1]["level"] == level
    return s

# 후속·검수 작업은 이미 끝낸 작업을 가리켜도 그 요청이 열려 있으면 이어가요.
for text in ["#74 잔여30 보완", "74 잔여31 국소 확인", "186 후보 독립판정", "194 새실거래 독립검수",
             "194 새 실거래 검수", "후속 결과는 #74에서 이어받아 반영", "#74·194 잔여 보완", "#194: 검수"]:
    reset()
    add(text, 36, "heavy")

# 선두의 명확한 부모 작업이 본문의 검수·참고 작업보다 우선해요.
reset()
add("#74 잔여 보완, #92는 참고만", 36)
reset()
add("194 새실거래 검수, #92 이후 비교", 36)
# 여러 본문 참조가 같은 요청이면 연결하고, 서로 다른 요청이면 기존 문맥을 유지해요.
reset()
add("후보 #74와 #194 검수", 36)
reset()
add("후보 #74와 #92 비교", 20)

# 영상·시각·용량 숫자를 작업 번호로 해석하지 않아요.
for text in ["31번 905~965초 번역 자막", "31 905~965초 번역 자막", "194초 화면 확인", "194MB 결과 확인",
             "후보 194개 검수", "파일 work/#74.json 확인", "#74backup 검수", "새 작업", "999 새실거래 검수"]:
    reset()
    add(text, 20)

# 없는 작업, 완료된 요청, 취소된 요청의 참조는 다른 요청을 되살리지 않아요.
for text in ["#999 후보 확인", "#80 후속 검수", "#81 후보 확인", "#999 후속 #74 참고"]:
    reset()
    s = add(text, 20)
    assert next(j for j in s["jobs"] if j["id"] == 38)["status"] == "done"
    assert next(j for j in s["jobs"] if j["id"] == 39)["cancelled"]

# 중복 억제도 추론한 요청으로 처리해요. 다른 요청의 같은 제목은 합치지 않아요.
reset()
s = add("#74 잔여30 보완", 36)
n = len(s["tasks"])
o.cmd_add(argparse.Namespace(text="  #74  잔여30  보완 ", to="claude", level=None))
assert len(load()["tasks"]) == n
reset()
with o.state(True) as s:
    s["tasks"].append({"id": 199, "job": 20, "title": "#74 잔여30 보완", "owner": "claude",
                       "status": "todo", "note": "", "created_at": 1})
s = add("#74 잔여30 보완", 36)
assert s["seq"] == 201

# 검수 중인 요청은 후속 작업 때문에 다시 진행 중으로 바뀌어요.
reset()
s = add("#92 후속 검수", 37)
j = next(j for j in s["jobs"] if j["id"] == 37)
assert j["status"] == "working" and j["rework_count"] == 1
# 직접 지시에서도 명시적인 작업 참조만 연결해요.
reset()
os.environ.pop("OFFICE_JOB")
os.environ["OFFICE_DIRECT"] = "1"
add("#74 후속 검수", 36)
reset()
os.environ.pop("OFFICE_JOB")
os.environ["OFFICE_DIRECT"] = "1"
add("새 독립 작업", None)
# 취소된 현재 요청을 우회하지 않아요.
reset()
os.environ["OFFICE_JOB"] = "39"
try:
    o.cmd_add(argparse.Namespace(text="#74 후속 검수", to="claude", level=None))
except SystemExit:
    pass
else:
    raise AssertionError("취소된 현재 요청에서 작업 추가를 허용했어요.")
assert len(load()["tasks"]) == 7
print("ok: task references, ambiguous numbers, closed jobs, events, deduplication, review and direct contexts")
