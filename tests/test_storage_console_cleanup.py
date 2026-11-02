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
function P($id,$name,$parent,$cmd='',$created='20260101000000.000000+540'){[pscustomobject]@{ProcessId=$id;Name=$name;ParentProcessId=$parent;CommandLine=$cmd;CreationDate=$created}}
'''


@unittest.skipUnless(shutil.which("powershell.exe") and shutil.which("wslpath") and (WINDOWS/"common.ps1").is_file(), "Windows 시험 도구가 없어요")
class ConsoleCleanup(unittest.TestCase):
    def select(self, procs, exclude=0):
        out = ps(FAKE + f"$p=@({procs}); ConvertTo-Json -Compress @(Select-OfficeConsoles $p {exclude})")
        data = json.loads(out or "[]")
        return [data] if isinstance(data, dict) else data

    def test_shortcut_console_with_own_host_is_selected(self):
        got = self.select("(P 10 'wsl.exe' 1 'wsl.exe -d U --cd ~ -e bash --rcfile C:\\x\\office-console.bashrc -i'),(P 11 'conhost.exe' 10)")
        self.assertEqual([(c["WslPid"], c["HostPid"]) for c in got], [(10, 11)])

    def test_reopened_console_is_selected(self):
        got = self.select("(P 20 'wsl.exe' 1 'wsl.exe -d U -u chan --exec /bin/bash C:\\x\\reopen-office.sh'),(P 21 'OpenConsole.exe' 20)")
        self.assertEqual(len(got), 1)

    def test_other_wsl_terminal_is_not_selected(self):
        self.assertEqual(self.select("(P 30 'wsl.exe' 1 'wsl.exe -d U'),(P 31 'conhost.exe' 30)"), [])

    def test_shared_host_like_windows_terminal_tab_is_not_selected(self):
        self.assertEqual(self.select("(P 40 'wsl.exe' 5 'wsl.exe -e bash --rcfile office-console.bashrc'),(P 5 'WindowsTerminal.exe' 1)"), [])

    def test_console_with_extra_client_is_not_selected(self):
        self.assertEqual(self.select("(P 50 'wsl.exe' 1 'x office-console.bashrc'),(P 51 'conhost.exe' 50),(P 52 'notepad.exe' 50)"), [])

    def test_real_layout_with_inner_wsl_is_selected_once(self):
        got = self.select("(P 70 'wsl.exe' 1 'x reopen-office.sh'),(P 71 'conhost.exe' 70),(P 72 'wsl.exe' 70 'x reopen-office.sh'),(P 73 'chrome.exe' 72)")
        self.assertEqual([(c["WslPid"], c["HostPid"]) for c in got], [(70, 71)])

    def test_new_console_can_be_excluded(self):
        self.assertEqual(self.select("(P 60 'wsl.exe' 1 'x reopen-office.sh'),(P 61 'conhost.exe' 60)", exclude=60), [])

    def test_close_requires_same_pid_and_creation_time(self):
        out = ps(FAKE + r'''
$t=[pscustomobject]@{WslPid=10;HostPid=999999;HostName='conhost.exe';HostCreated='A'}
$same=@(P 999999 'conhost.exe' 10 '' 'B')
$other=@(P 999999 'notepad.exe' 10 '' 'A')
'{0}|{1}' -f @(Close-OfficeConsoles @($t) $same).Count,@(Close-OfficeConsoles @($t) $other).Count''')
        self.assertEqual(out, "0|0")


if __name__ == "__main__":
    unittest.main()
