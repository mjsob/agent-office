"""열린 요청에 끝난 작업이 수백 개 쌓여도 에이전트에게 주는 작업판 글이 한도를 넘지 않는지 확인해요.
실행: python3 -I tests/test_board_size.py [payload/office.py 경로]
"""
import atexit, importlib.util, os, shutil, sys, tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="office-board-size-")
atexit.register(shutil.rmtree, TMP)
for key in list(os.environ):
    if key.startswith("OFFICE_"):
        del os.environ[key]
os.environ.update(OFFICE_DIR=TMP, OFFICE_AS="claude", OFFICE_SESSION="t1")
src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "payload" / "office.py")
spec = importlib.util.spec_from_file_location("office_board_size_test", src)
o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)

long = "긴 작업 설명 " * 120
jobs = [{"id": 20, "status": "working", "lead": "codex", "text": "열린 요청"},
        {"id": 21, "status": "done", "lead": "codex", "text": "끝난 요청"}]
tasks = [{"id": i, "job": 20, "title": long, "owner": "claude", "status": "done", "note": long, "created_at": 1}
         for i in range(1, 301)]
tasks += [{"id": 400, "job": 20, "title": long, "owner": "codex", "status": "doing", "note": long, "created_at": 1},
          {"id": 401, "job": 21, "title": "다른 끝난 요청", "owner": "codex", "status": "done", "note": "", "created_at": 1}]
text = o.board_text({"jobs": jobs, "tasks": tasks, "msgs": []})
size = len(text.encode())
assert size < 20_000, size
assert "끝난 작업 288개 생략" in text, text[:300]
assert "#300 " in text and "#289 " in text and "#288 " not in text
assert "#400 " in text and "#401 " not in text   # 진행 중은 항상, 닫힌 요청의 끝난 작업은 안 보임
print(f"ok: board_text {size} bytes with 300 done tasks")
