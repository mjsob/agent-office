"""요청 118: 디스크 줄이기를 막아 둔 동안에는 시작할 수 없고 화면 상태도 쓸 수 없다고 알려요."""
import importlib.util, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("office_blocked_test", ROOT / "payload/office.py")
o = importlib.util.module_from_spec(spec); spec.loader.exec_module(o)


class StorageBlockedTest(unittest.TestCase):
    def test_blocked_by_default(self):
        self.assertTrue(o.STORAGE_COMPACT_BLOCKED)

    def test_start_refused_before_touching_anything(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(o, "STORAGE_COMPACT_DIR", Path(tmp) / "state"), \
                patch.object(o.subprocess, "run", side_effect=AssertionError("must not run")):
            for manual in (False, True):
                with self.assertRaises(o.UserError):
                    o.start_storage_compact(True, manual)
            self.assertFalse((Path(tmp) / "state").exists())

    def test_view_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"WSL_DISTRO_NAME": "x"}), \
                patch.object(o, "STORAGE_COMPACT_DIR", Path(tmp)):
            view = o.storage_compact_view()
        self.assertFalse(view["available"]); self.assertFalse(view["can_start"]); self.assertTrue(view["blocked"])


if __name__ == "__main__":
    unittest.main()
