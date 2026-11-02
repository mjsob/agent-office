"""디스크 줄이기 뒤 Office 전용 콘솔만 골라 닫는지 확인해요. 실제 프로세스는 닫지 않고 가짜 목록으로만 확인해요.
실행: python3 -I tests/test_storage_console_cleanup.py
"""
import base64, json, shutil, subprocess, unittest
from pathlib import Path

WINDOWS = Path.home() / "projects/wsl-compact/src/windows"


def ps(script):
    code = f". '{subprocess.check_output(['wslpath','-w',str(WINDOWS/'common.ps1')],text=True).strip()}'\n" + script
    encoded = base64.b64encode(code.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                       capture_output=True, timeout=60)
    if r.returncode:
        raise AssertionError(r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace"))
    return r.stdout.decode("utf-8", errors="replace").strip()


FAKE = r'''
$cfg=[pscustomobject]@{distro='U';script_linux='/mnt/c/L/wsl-compact/reopen-office.sh';console_rc='/home/u/.local/share/office-app/bin/office-console.bashrc'}
function P($id,$name,$parent,$cmd='',$created='20260101000000.000000+540'){[pscustomobject]@{ProcessId=$id;Name=$name;ParentProcessId=$parent;CommandLine=$cmd;CreationDate=$created}}
'''


@unittest.skipUnless(shutil.which("powershell.exe") and shutil.which("wslpath") and (WINDOWS/"common.ps1").is_file(), "Windows 시험 도구가 없어요")
class ConsoleCleanup(unittest.TestCase):
    def select(self, procs, exclude=0):
        out = ps(FAKE + f"$p=@({procs}); ConvertTo-Json -Compress @(Select-OfficeConsoles $p $cfg {exclude})")
        data = json.loads(out or "[]")
        return [data] if isinstance(data, dict) else data

    def test_shortcut_console_with_own_host_is_selected(self):
        got = self.select("(P 10 'wsl.exe' 1 'wsl.exe -d U --cd ~ -e bash --rcfile /home/u/.local/share/office-app/bin/office-console.bashrc -i'),(P 11 'conhost.exe' 10)")
        self.assertEqual([(c["WslPid"], c["HostPid"]) for c in got], [(10, 11)])

    def test_reopened_console_is_selected(self):
        got = self.select("(P 20 'wsl.exe' 1 'wsl.exe -d U -u chan --exec /bin/bash /mnt/c/L/wsl-compact/reopen-office.sh'),(P 21 'OpenConsole.exe' 20)")
        self.assertEqual(len(got), 1)

    def test_other_wsl_terminal_is_not_selected(self):
        self.assertEqual(self.select("(P 30 'wsl.exe' 1 'wsl.exe -d U'),(P 31 'conhost.exe' 30)"), [])

    def test_shared_host_like_windows_terminal_tab_is_not_selected(self):
        self.assertEqual(self.select("(P 40 'wsl.exe' 5 'wsl.exe -d U -e bash --rcfile /home/u/.local/share/office-app/bin/office-console.bashrc'),(P 5 'WindowsTerminal.exe' 1)"), [])

    def test_console_with_extra_client_is_not_selected(self):
        self.assertEqual(self.select("(P 50 'wsl.exe' 1 'wsl.exe -d U x /home/u/.local/share/office-app/bin/office-console.bashrc'),(P 51 'conhost.exe' 50),(P 52 'notepad.exe' 50)"), [])

    def test_real_layout_with_inner_wsl_is_selected_once(self):
        got = self.select("(P 70 'wsl.exe' 1 'wsl.exe -d U x /mnt/c/L/wsl-compact/reopen-office.sh'),(P 71 'conhost.exe' 70),(P 72 'wsl.exe' 70 'wsl.exe -d U x /mnt/c/L/wsl-compact/reopen-office.sh'),(P 73 'chrome.exe' 72)")
        self.assertEqual([(c["WslPid"], c["HostPid"]) for c in got], [(70, 71)])

    def test_new_console_can_be_excluded(self):
        self.assertEqual(self.select("(P 60 'wsl.exe' 1 'wsl.exe -d U x /mnt/c/L/wsl-compact/reopen-office.sh'),(P 61 'conhost.exe' 60)", exclude=60), [])

    def test_other_distro_is_not_selected(self):
        self.assertEqual(self.select("(P 80 'wsl.exe' 1 'wsl.exe -d Other --exec /bin/bash /mnt/c/L/wsl-compact/reopen-office.sh'),(P 81 'conhost.exe' 80)"), [])

    def test_other_office_install_is_not_selected(self):
        self.assertEqual(self.select("(P 82 'wsl.exe' 1 'wsl.exe -d U -e bash --rcfile /home/other/office-console.bashrc -i'),(P 83 'conhost.exe' 82)"), [])
        self.assertEqual(self.select("(P 84 'wsl.exe' 1 'wsl.exe -d U --exec /bin/bash /mnt/c/Other/wsl-compact/reopen-office.sh'),(P 85 'conhost.exe' 84)"), [])

    def test_path_must_be_a_whole_token(self):
        for tail in ("reopen-office.sh.backup", "reopen-office.sh2", "x-reopen-office.sh"):
            cmd = "wsl.exe -d U --exec /bin/bash /mnt/c/L/wsl-compact/" + tail
            self.assertEqual(self.select(f"(P 86 'wsl.exe' 1 '{cmd}'),(P 87 'conhost.exe' 86)"), [], tail)
        quoted = "wsl.exe -d U --exec /bin/bash \"/mnt/c/L/wsl-compact/reopen-office.sh\""
        self.assertEqual(len(self.select(f"(P 88 'wsl.exe' 1 '{quoted}'),(P 89 'conhost.exe' 88)")), 1)

    def close(self, before, after):
        out = ps(FAKE + f"""
$a=@({before}); $t=@(Select-OfficeConsoles $a $cfg)
$b=@({after})
'{{0}}|{{1}}' -f $t.Count,@(Close-OfficeConsoles $t $cfg $b).Count""")
        return out

    LAYOUT = "(P 90 'wsl.exe' 1 'wsl.exe -d U x /mnt/c/L/wsl-compact/reopen-office.sh'),(P 91 'conhost.exe' 90)"

    def test_close_skips_when_another_program_attached_after_snapshot(self):
        # 실제 Stop-Process는 없는 PID라 항상 0을 돌려줘요. 여기서는 '닫기 후보에서 빠지는지'를 Select 결과로 확인해요.
        self.assertEqual(self.close(self.LAYOUT, self.LAYOUT + ",(P 92 'notepad.exe' 90)"), "1|0")

    def test_close_skips_when_pid_is_reused_or_time_changed(self):
        self.assertEqual(self.close(self.LAYOUT, "(P 90 'wsl.exe' 1 'wsl.exe -d U x /mnt/c/L/wsl-compact/reopen-office.sh'),(P 91 'conhost.exe' 90 '' '20270101000000.000000+540')"), "1|0")
        self.assertEqual(self.close(self.LAYOUT, "(P 90 'wsl.exe' 1 'cmd.exe'),(P 91 'conhost.exe' 90)"), "1|0")

    def test_close_requires_unchanged_layout(self):
        # 같은 배치면 후보로 인정돼 Stop-Process까지 가지만, 없는 PID 오류는 삼켜서 0이에요.
        self.assertEqual(self.close(self.LAYOUT, self.LAYOUT), "1|0")


if __name__ == "__main__":
    unittest.main()
