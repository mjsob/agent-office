"""설치기가 직접 고친 부분을 덮어쓰지 않는지 확인해요.
실행: python3 -I tests/test_install_preserve.py [install.sh 경로]
가짜 HOME 에서 codex·claude 를 흉내 내는 명령으로 돌려요. 실제 설치와 네트워크는 쓰지 않아요.
"""
import hashlib, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

SRC = Path(sys.argv.pop(1) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "install.sh").resolve()
REPO = SRC.parent


class InstallPreserve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="office-install-")
        t = Path(self.tmp.name)
        self.home, self.pkg = t / "home", t / "pkg"
        self.home.mkdir(); (t / "stub").mkdir()
        for n in ("codex", "claude"):
            f = t / "stub" / n
            f.write_text("#!/bin/sh\necho stub\n"); f.chmod(0o755)
        self.pkg.mkdir()
        shutil.copy(SRC, self.pkg / "install.sh")
        shutil.copytree(REPO / "payload", self.pkg / "payload")
        self.app = self.home / ".local/share/office-app/app"
        self.bin = self.home / ".local/share/office-app/bin"

    def tearDown(self):
        self.tmp.cleanup()

    def sums(self):
        pay = self.pkg / "payload"
        names = ("office.py", "office.html", "office.ico", "office-icon.png", "VERSION")
        (pay / "SHA256SUMS").write_text("".join(
            "%s  %s\n" % (hashlib.sha256((pay / n).read_bytes()).hexdigest(), n) for n in names))

    def run_install(self):
        self.sums()
        env = {k: v for k, v in os.environ.items() if not k.startswith("OFFICE_")}
        env.update(HOME=str(self.home), PATH=f"{self.tmp.name}/stub:{os.environ['PATH']}")
        return subprocess.run(["bash", str(self.pkg / "install.sh"), "--apply", "--skip-login", "--no-launch", "--no-shortcut"],
                              env=env, capture_output=True, text=True, timeout=120)

    def bump(self, version):
        (self.pkg / "payload" / "VERSION").write_text(version + "\n")
        (self.pkg / "payload" / "office.py").write_text((self.pkg / "payload" / "office.py").read_text() + f"\n# {version}\n")

    def first(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_clean_merge_keeps_custom(self):
        self.first()
        mine = self.app / "office.html"
        mine.write_text("<!--mine-->\n" + mine.read_text())
        (self.pkg / "payload" / "office.py").write_text((self.pkg / "payload" / "office.py").read_text() + "\n# 새 줄\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(mine.read_text().startswith("<!--mine-->"))

    def test_conflict_stops_before_touching(self):
        self.first()
        py = self.app / "office.py"
        text = py.read_text().replace("\n", "\n#내수정\n", 1)
        py.write_text(text)
        (self.pkg / "payload" / "office.py").write_text(text.replace("#내수정", "#다른수정"))
        before = {p.name: p.read_bytes() for p in self.app.iterdir() if p.is_file()}
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("멈췄어요", r.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.app.iterdir() if p.is_file()})
        self.assertFalse((self.home / ".local/share/office-app/backups").exists())

    def test_deleted_file_stops(self):
        self.first()
        (self.app / "office.html").unlink()
        self.bump("9.9.9")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.app / "office.html").exists())

    def test_symlinked_file_stops(self):
        self.first()
        (self.app / "office.ico").unlink(); (self.app / "office.ico").symlink_to("/etc/hostname")
        self.bump("9.9.9")
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertTrue((self.app / "office.ico").is_symlink())

    def test_edited_launcher_stops(self):
        self.first()
        (self.bin / "office").write_text((self.bin / "office").read_text() + "export MY=1\n")
        kept = (self.bin / "office").read_text()
        self.bump("9.9.9")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.bin / "office").read_text(), kept)

    def test_local_version_not_replaced_by_public(self):
        (self.pkg / "payload" / "VERSION").write_text("1.0.0+local\n")
        self.first()
        (self.pkg / "payload" / "VERSION").write_text("1.0.1\n")
        before = (self.app / "VERSION").read_text()
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.app / "VERSION").read_text(), before)

    def test_legacy_edited_install_stops(self):
        self.first()
        (self.app / ".office-manifest.json").unlink()
        (self.app / "office.html").write_text("custom")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.app / "office.html").read_text(), "custom")

    def test_unedited_upgrade_works(self):
        self.first()
        self.bump("9.9.9")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual((self.app / "VERSION").read_text().strip(), "9.9.9")


if __name__ == "__main__":
    unittest.main()
