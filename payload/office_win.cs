// Office Windows 도우미 (PowerShell 없이 쓰는 작은 실행 파일). C# 5(내장 csc)로 빌드해요.
// 명령: probe icon focus measure status launch worker recover reopen install-recovery
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Management;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

public static class OfficeWin {
    public const int Api = 1;
    [DllImport("kernel32.dll")] static extern bool FreeConsole();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern uint GetCompressedFileSizeW(string path, out uint high);
    [DllImport("kernel32.dll")] static extern void SetLastError(uint error);

    static string dataDir;
    static List<string> rest;

    [STAThread]
    public static int Main(string[] args) {
        try { Console.OutputEncoding = new UTF8Encoding(false); } catch (IOException) { }
        string cmd = args.Length > 0 ? args[0] : "";
        rest = new List<string>();
        string dir = null;
        for (int i = 1; i < args.Length; i++) {
            if (args[i] == "--dir" && i + 1 < args.Length) { dir = args[++i]; } else rest.Add(args[i]);
        }
        dataDir = dir ?? Path.Combine(Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? "", "wsl-compact");
        try {
            switch (cmd) {
                case "probe": return Probe();
                case "icon": return Icon();
                case "focus": return Focus();
                case "measure": return Measure();
                case "status": return Status();
                case "launch": return Launch();
                case "worker": return Worker.Run(dataDir, rest);
                case "recover": return Recover();
                case "reopen": return Reopen();
                case "install-recovery": return InstallRecovery();
                case "selftest-make-vhdx": return SelfTest.MakeVhdx(rest);
                case "selftest-compact": return SelfTest.Compact(rest);
                default: Console.Error.WriteLine("알 수 없는 명령이에요: " + cmd); return 64;
            }
        } catch (Exception e) {
            Console.Error.WriteLine(OneLine(e.Message));
            return 1;
        }
    }

    // ---------- 공통 ----------
    public static string OneLine(string s) { return Regex.Replace(s ?? "", @"\s+", " ").Trim(); }
    public static string Quote(string v) { return Regex.IsMatch(v, @"\s") ? "\"" + v + "\"" : v; }
    public static long Now() { return (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }

    public static string Json(object v) {
        var sb = new StringBuilder(); WriteJson(sb, v); return sb.ToString();
    }
    static void WriteJson(StringBuilder sb, object v) {
        if (v == null) { sb.Append("null"); return; }
        if (v is bool) { sb.Append((bool)v ? "true" : "false"); return; }
        if (v is string) { WriteString(sb, (string)v); return; }
        if (v is double) { sb.Append(((double)v).ToString("0.####", System.Globalization.CultureInfo.InvariantCulture)); return; }
        if (v is decimal) { sb.Append(((decimal)v).ToString(System.Globalization.CultureInfo.InvariantCulture)); return; }
        if (v is int || v is long || v is uint || v is ulong) { sb.Append(Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture)); return; }
        var map = v as IEnumerable<KeyValuePair<string, object>>;
        if (map != null) {
            sb.Append('{'); bool first = true;
            foreach (var kv in map) { if (!first) sb.Append(','); first = false; WriteString(sb, kv.Key); sb.Append(':'); WriteJson(sb, kv.Value); }
            sb.Append('}'); return;
        }
        throw new ArgumentException("지원하지 않는 JSON 값이에요: " + v.GetType());
    }
    static void WriteString(StringBuilder sb, string s) {
        sb.Append('"');
        foreach (char c in s) {
            switch (c) {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4")); else sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }
    public static List<KeyValuePair<string, object>> Obj(params object[] kv) {
        var list = new List<KeyValuePair<string, object>>();
        for (int i = 0; i + 1 < kv.Length; i += 2) list.Add(new KeyValuePair<string, object>((string)kv[i], kv[i + 1]));
        return list;
    }
    public static void Set(List<KeyValuePair<string, object>> o, string key, object value) {
        for (int i = 0; i < o.Count; i++) if (o[i].Key == key) { o[i] = new KeyValuePair<string, object>(key, value); return; }
        o.Add(new KeyValuePair<string, object>(key, value));
    }
    public static void Print(object v) { Console.Out.WriteLine(Json(v)); Console.Out.Flush(); }

    // 읽기는 사전 형태로 받아요. 파일이 없거나 깨졌으면 null.
    public static Dictionary<string, object> ReadJson(string dir, string name) {
        string path = Path.Combine(dir, name);
        try {
            if (!File.Exists(path)) return null;
            string text = File.ReadAllText(path, Encoding.UTF8).TrimStart('\uFEFF');
            return new JavaScriptSerializer().DeserializeObject(text) as Dictionary<string, object>;
        } catch (Exception) { return null; }
    }
    public static string Str(Dictionary<string, object> d, string key) {
        object v; if (d == null || !d.TryGetValue(key, out v) || v == null) return null; return Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture);
    }
    public static bool IsTrue(Dictionary<string, object> d, string key) {
        object v; return d != null && d.TryGetValue(key, out v) && v is bool && (bool)v;
    }
    public static long Num(Dictionary<string, object> d, string key) {
        object v; if (d == null || !d.TryGetValue(key, out v) || v == null) return 0;
        try { return Convert.ToInt64(v); } catch (Exception) { return 0; }
    }
    public static void WriteAtomic(string dir, string name, string text) {
        string target = Path.Combine(dir, name);
        string temp = target + "." + Process.GetCurrentProcess().Id + ".tmp";
        File.WriteAllText(temp, text, new UTF8Encoding(false));
        for (int i = 0; i < 5; i++) {
            try {
                if (File.Exists(target)) File.Delete(target);
                File.Move(temp, target); return;
            } catch (IOException) { if (i == 4) { try { File.Delete(temp); } catch (Exception) { } throw; } Thread.Sleep(50); }
            catch (UnauthorizedAccessException) { if (i == 4) { try { File.Delete(temp); } catch (Exception) { } throw; } Thread.Sleep(50); }
        }
    }
    public static void WriteStatus(string dir, string rid, string status, string message, List<KeyValuePair<string, object>> extra) {
        var d = Obj("schema_version", 1, "request_id", rid, "status", status, "message", message, "updated_at", Now());
        if (extra != null) foreach (var kv in extra) Set(d, kv.Key, kv.Value);
        WriteAtomic(dir, "status.json", Json(d));
    }
    public static bool IsAdmin() {
        return new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
    }
    // 잠금 파일이 다른 곳에서 열려 있으면 true
    public static bool IsLocked(string dir, string name) {
        string p = Path.Combine(dir, name);
        if (!File.Exists(p)) return false;
        try { using (new FileStream(p, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { } return false; }
        catch (IOException) { return true; }
    }
    static void HideConsole() { try { FreeConsole(); } catch (Exception) { } }

    // ---------- probe ----------
    static int Probe() { Print(Obj("ok", true, "api", Api, "admin", IsAdmin())); return 0; }

    // ---------- measure ----------
    static int Measure() {
        if (rest.Count < 1) { Console.Error.WriteLine("배포판 이름이 필요해요."); return 64; }
        string distro = rest[0];
        var found = new List<Microsoft.Win32.RegistryKey>();
        using (var root = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Lxss")) {
            if (root == null) throw new Exception("WSL 배포판 정보를 찾지 못했어요.");
            foreach (string name in root.GetSubKeyNames()) {
                var k = root.OpenSubKey(name);
                if (k != null && string.Equals((k.GetValue("DistributionName") as string) ?? "", distro, StringComparison.Ordinal)) found.Add(k); else if (k != null) k.Dispose();
            }
        }
        if (found.Count != 1) throw new Exception("배포판 하나를 찾지 못했어요.");
        var entry = found[0];
        object ver = entry.GetValue("Version");
        if (ver == null || Convert.ToInt32(ver) != 2) throw new Exception("WSL 2 배포판이 아니에요.");
        string baseDir = (entry.GetValue("BasePath") as string) ?? "";
        string vhd = (entry.GetValue("VhdFileName") as string);
        if (string.IsNullOrEmpty(vhd)) vhd = "ext4.vhdx";
        string full = Path.Combine(baseDir, vhd);
        if (full.StartsWith(@"\\?\")) full = full.Substring(4);
        var info = new FileInfo(full);
        if (!info.Exists) throw new Exception("디스크 파일을 찾지 못했어요.");
        uint high = 0; SetLastError(0);
        uint low = GetCompressedFileSizeW(info.FullName, out high);
        int err = Marshal.GetLastWin32Error();
        if (low == uint.MaxValue && err != 0) throw new Exception("실점유를 읽지 못했어요: " + err);
        var drive = new DriveInfo(Path.GetPathRoot(info.FullName));
        Print(Obj("distro", distro, "path", info.FullName, "logical_bytes", (ulong)info.Length,
            "allocated_bytes", (ulong)high * 4294967296UL + low, "free_bytes", drive.AvailableFreeSpace, "drive", drive.Name.TrimEnd('\\')));
        return 0;
    }

    // ---------- status ----------
    static int Status() {
        string cancelRid = null;
        for (int i = 0; i < rest.Count; i++) if (rest[i] == "--cancel" && i + 1 < rest.Count) cancelRid = rest[++i];
        var record = ReadJson(dataDir, "status.json");
        bool running = IsLocked(dataDir, "active.lock") || IsLocked(dataDir, "launch.lock");
        if (cancelRid != null) {
            if (!running || record == null || Str(record, "request_id") != cancelRid || !IsTrue(record, "can_cancel")) throw new Exception("중단할 수 있는 작업이 없거나 실행 정보가 달라요.");
            WriteAtomic(dataDir, "cancel.json", Json(Obj("request_id", cancelRid, "requested_at", Now())));
        }
        var data = Obj("running", running);
        if (record != null) { Set(data, "status", Str(record, "status")); Set(data, "request_id", Str(record, "request_id")); Set(data, "updated_at", record.ContainsKey("updated_at") ? record["updated_at"] : null); }
        Print(data);
        return 0;
    }

    // ---------- icon (작업 표시줄 아이콘) ----------
    [StructLayout(LayoutKind.Sequential)] public struct OfficeIconKey { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit, Size = 24)] public struct OfficeIconValue {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr value;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface OfficeIconStore {
        [PreserveSig] int GetCount(out uint n);
        [PreserveSig] int GetAt(uint n, out OfficeIconKey key);
        [PreserveSig] int GetValue(ref OfficeIconKey key, out OfficeIconValue value);
        [PreserveSig] int SetValue(ref OfficeIconKey key, ref OfficeIconValue value);
        [PreserveSig] int Commit();
    }
    delegate bool WindowCallback(IntPtr window, IntPtr unused);
    [DllImport("user32.dll")] static extern bool EnumWindows(WindowCallback callback, IntPtr unused);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr window, StringBuilder text, int size);
    [DllImport("shell32.dll")] static extern int SHGetPropertyStoreForWindow(IntPtr window, ref Guid iid, out OfficeIconStore store);
    [DllImport("ole32.dll")] static extern int PropVariantClear(ref OfficeIconValue value);

    static OfficeIconKey IconKey(uint pid) { return new OfficeIconKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = pid }; }
    static string IconRead(OfficeIconStore store, uint pid) {
        OfficeIconKey key = IconKey(pid); OfficeIconValue value;
        if (store.GetValue(ref key, out value) < 0) return "";
        try { return value.vt == 31 ? Marshal.PtrToStringUni(value.value) : ""; }
        finally { PropVariantClear(ref value); }
    }
    static int IconUpdate(string resource) {
        int changed = 0;
        EnumWindows(delegate(IntPtr window, IntPtr unused) {
            StringBuilder title = new StringBuilder(1024); GetWindowText(window, title, title.Capacity);
            string caption = title.ToString();
            if (caption != "Office" && !caption.EndsWith("— Office", StringComparison.Ordinal)) return true;
            OfficeIconStore store; Guid iid = typeof(OfficeIconStore).GUID;
            if (SHGetPropertyStoreForWindow(window, ref iid, out store) < 0) return true;
            try {
                string id = IconRead(store, 5);
                if (!(id.StartsWith("Chrome.localhost_/", StringComparison.Ordinal) || id.StartsWith("MSEdge.localhost_/", StringComparison.Ordinal))) return true;
                if (IconRead(store, 3) == resource) return true;
                OfficeIconKey key = IconKey(3);
                OfficeIconValue value = new OfficeIconValue { vt = 31, value = Marshal.StringToCoTaskMemUni(resource) };
                try {
                    Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value));
                    Marshal.ThrowExceptionForHR(store.Commit()); changed++;
                } finally { PropVariantClear(ref value); }
            } finally { Marshal.ReleaseComObject(store); }
            return true;
        }, IntPtr.Zero);
        return changed;
    }
    static int Icon() {
        string icon = Path.Combine(Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? "", @"Office\office.ico");
        if (!File.Exists(icon)) { Print(Obj("missing_icon", true)); return 0; }
        // 창이 뜨는 데 시간이 걸려서, 서버 쪽과 똑같이 6초 동안 0.2초마다 다시 봐요.
        int changed = 0;
        for (int i = 0; i < 30; i++) { changed += IconUpdate(icon + ",0"); Thread.Sleep(200); }
        Print(Obj("updated", changed));
        return 0;
    }

    // ---------- focus (탐색기 창 앞으로) ----------
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool ShowWindowAsync(IntPtr h, int n);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
    static string NormalizePath(string p) { return Regex.Replace(p ?? "", @"\\+", @"\").TrimEnd('\\'); }

    static List<IntPtr> FindExplorerWindows(dynamic shell, string want, string parent, bool folder) {
        var exact = new List<IntPtr>(); var near = new List<IntPtr>();
        foreach (dynamic w in shell.Windows()) {
            try {
                string path = NormalizePath((string)w.Document.Folder.Self.Path);
                IntPtr h = new IntPtr((long)w.HWND);
                if (folder) { if (string.Equals(path, want, StringComparison.OrdinalIgnoreCase)) exact.Add(h); }
                else {
                    bool selected = false;
                    foreach (dynamic item in w.Document.SelectedItems()) { if (string.Equals(NormalizePath((string)item.Path), want, StringComparison.OrdinalIgnoreCase)) { selected = true; break; } }
                    if (selected) exact.Add(h); else if (string.Equals(path, parent, StringComparison.OrdinalIgnoreCase)) near.Add(h);
                }
            } catch (Exception) { }
        }
        return exact.Count > 0 ? exact : near;
    }
    static int Focus() {
        if (rest.Count < 2) { Console.Error.WriteLine("경로와 종류(file|folder)가 필요해요."); return 64; }
        string target = rest[0]; bool folder = rest[1] == "folder";
        string want = NormalizePath(target);
        string parent = folder ? null : NormalizePath(Path.GetDirectoryName(target));
        dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("Shell.Application"));
        int tries = 0;
        for (int i = 0; i < 25; i++) {
            var hs = FindExplorerWindows(shell, want, parent, folder);
            if (hs.Count > 0) {
                if (hs.Contains(GetForegroundWindow())) { Print(Obj("result", "focused")); return 0; }
                if (tries >= 3) break;
                tries++;
                IntPtr h = hs[0];
                if (IsIconic(h)) ShowWindowAsync(h, 9);
                keybd_event(0x12, 0, 0, UIntPtr.Zero);
                keybd_event(0x12, 0, 2, UIntPtr.Zero);
                SetForegroundWindow(h);
            }
            Thread.Sleep(100);
        }
        Thread.Sleep(200);
        var fin = FindExplorerWindows(shell, want, parent, folder);
        string result = fin.Count > 0 && fin.Contains(GetForegroundWindow()) ? "focused" : (fin.Count > 0 ? "behind" : "no-match");
        Print(Obj("result", result));
        return 0;
    }

    // ---------- launch (관리자 승인 → 진행창) ----------
    public static string TempRoot() { return Path.GetFullPath(Path.GetTempPath()); }
    static int Launch() {
        HideConsole();
        string vhdx = null, distro = null, rid = "manual", testMode = "none", testSeconds = "1";
        for (int i = 0; i < rest.Count; i++) {
            if (rest[i] == "--test-mode" && i + 1 < rest.Count) { testMode = rest[++i]; continue; }
            if (rest[i] == "--test-seconds" && i + 1 < rest.Count) { testSeconds = rest[++i]; continue; }
            if (rest[i] == "--vhdx" && i + 1 < rest.Count) vhdx = rest[++i];
            else if (rest[i] == "--distro" && i + 1 < rest.Count) distro = rest[++i];
            else if (rest[i] == "--request-id" && i + 1 < rest.Count) rid = rest[++i];
        }
        if (vhdx == null && distro == null) {
            var t = ReadJson(dataDir, "target.json");
            if (t == null) throw new Exception("실행 정보를 찾지 못했어요. WSL은 끄지 않았어요.");
            vhdx = Str(t, "path"); distro = Str(t, "distro"); rid = Str(t, "request_id");
        }
        if (string.IsNullOrEmpty(vhdx) || string.IsNullOrEmpty(distro)) throw new Exception("대상 디스크와 배포판 정보를 확인하지 못했어요.");
        if (rid == "manual" || string.IsNullOrEmpty(rid)) rid = Guid.NewGuid().ToString("N");
        FileStream launchLock = null;
        try {
            if (!Regex.IsMatch(rid, "^[A-Za-z0-9_-]{1,80}$") || Regex.IsMatch(vhdx, "[\"\r\n]") || Regex.IsMatch(distro, "[\"\r\n]")) throw new Exception("실행 정보가 올바르지 않아요.");
            try { launchLock = new FileStream(Path.Combine(dataDir, "launch.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
            catch (IOException) { return 3; }
            if (IsLocked(dataDir, "active.lock")) return 3;
            var lease = Obj("request_id", rid, "expires_at", Now() + 600, "revoked", false);
            WriteAtomic(dataDir, "launch-request.json", Json(lease));
            WriteStatus(dataDir, rid, "awaiting_admin", "관리자 권한을 확인하고 있어요. 승인 전에는 WSL을 끄지 않아요.",
                Obj("phase", "permission", "can_cancel", false, "wsl_ready", true, "recovery_required", false));
            var psi = new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName,
                "worker --dir " + Quote(dataDir) + " --vhdx " + Quote(vhdx) + " --distro " + Quote(distro) + " --request-id " + Quote(rid));
            psi.UseShellExecute = true; psi.WindowStyle = ProcessWindowStyle.Normal;
            if (testMode != "none") {
                // 시험: 임시 폴더에서만 쓸 수 있고, 관리자 승인 창 없이 시험용 진행창을 띄워요.
                if (!Path.GetFullPath(dataDir).StartsWith(TempRoot(), StringComparison.OrdinalIgnoreCase)) throw new Exception("시험은 Windows 임시 폴더에서만 실행해요.");
                psi.Arguments += " --test-mode " + testMode + " --test-seconds " + testSeconds;
            } else if (!IsAdmin()) psi.Verb = "runas";
            Process worker = Process.Start(psi);
            // 진행창이 잠금을 쥘 때까지 기다려요. 프로세스가 떴다는 것만으로는 부족해요.
            for (int i = 0; i < 240; i++) {
                if (worker.HasExited) break;
                var record = ReadJson(dataDir, "status.json");
                if (IsLocked(dataDir, "active.lock") && Str(record, "request_id") == rid && Str(record, "status") != "awaiting_admin") return 0;
                Thread.Sleep(500);
            }
            var last = ReadJson(dataDir, "status.json");
            string st = Str(last, "status");
            if (Str(last, "request_id") == rid && (st == "cancelled" || st == "completed" || st == "failed")) return 0;
            Set(lease, "revoked", true);
            WriteAtomic(dataDir, "launch-request.json", Json(lease));
            throw new Exception("실행창 준비 시간이 지나 요청을 취소했어요. 뒤늦게 열린 실행창은 WSL을 끄지 않아요.");
        } catch (Exception e) {
            var w32 = e as Win32Exception;
            bool cancelled = w32 != null && w32.NativeErrorCode == 1223;
            try { WriteStatus(dataDir, rid, cancelled ? "cancelled" : "failed", "관리자 실행창을 시작하지 못했어요. WSL은 끄지 않았어요. " + OneLine(e.Message),
                Obj("phase", "permission", "can_cancel", false, "wsl_ready", true, "recovery_required", false)); } catch (Exception) { }
            return 4;
        } finally { if (launchLock != null) launchLock.Dispose(); }
    }

    // ---------- recover (남은 디스크 연결 해제) ----------
    static int Recover() {
        bool check = rest.Contains("--check");
        var target = ReadJson(dataDir, "target.json");
        string tpath = Str(target, "path");
        if (target == null || tpath == null || !File.Exists(tpath) || !string.Equals(Path.GetExtension(tpath), ".vhdx", StringComparison.OrdinalIgnoreCase))
            throw new Exception("복구할 디스크 정보를 찾지 못했어요.");
        string rid = Str(target, "request_id");
        if (check) {
            Print(Obj("target", tpath, "distro", Str(target, "distro"), "admin", IsAdmin(),
                "running", IsLocked(dataDir, "active.lock") || IsLocked(dataDir, "launch.lock")));
            return 0;
        }
        if (!IsAdmin()) {
            try {
                var psi = new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName, "recover --dir " + Quote(dataDir));
                psi.UseShellExecute = true; psi.Verb = "runas"; Process.Start(psi);
            } catch (Exception) {
                Console.WriteLine("관리자 권한을 받지 못했어요. 디스크는 건드리지 않았어요.");
                Console.Write("닫으려면 Enter를 누르세요"); Console.ReadLine();
            }
            return 0;
        }
        try {
            if (IsLocked(dataDir, "active.lock") || IsLocked(dataDir, "launch.lock")) throw new Exception("줄이기 실행창에서 먼저 중단하세요. 처리 중에는 복구하지 않아요.");
            if (Process.GetProcessesByName("diskpart").Length > 0) throw new Exception("DiskPart가 실행 중이에요. 그 작업이 끝난 뒤 다시 시도하세요.");
            var running = RunningDistros();
            if (running.Contains(Str(target, "distro"))) throw new Exception("이 배포판이 켜져 있어요. 켜진 WSL 디스크는 분리하지 않아요.");
            bool was = DiskImageAttached(tpath, true);
            string message = was ? "남은 디스크 연결을 해제했어요. WSL을 다시 켜세요." : "남은 디스크 연결이 없어요. WSL을 다시 켜세요.";
            if (was) Console.WriteLine("이전 줄이기가 남긴 디스크 연결을 해제해요. 파일을 지우거나 줄이지 않아요.");
            WriteStatus(dataDir, rid, "cancelled", message, Obj("phase", "recovered", "wsl_ready", true, "recovery_required", false, "can_cancel", false));
            Console.WriteLine(message);
        } catch (Exception e) {
            Console.WriteLine(OneLine(e.Message));
        } finally {
            Console.Write("닫으려면 Enter를 누르세요"); Console.ReadLine();
        }
        return 0;
    }
    static List<string> RunningDistros() {
        var psi = new ProcessStartInfo(Path.Combine(Environment.GetEnvironmentVariable("WINDIR") ?? @"C:\Windows", @"System32\wsl.exe"), "--list --running --quiet");
        psi.UseShellExecute = false; psi.RedirectStandardOutput = true; psi.RedirectStandardError = true; psi.CreateNoWindow = true;
        psi.StandardOutputEncoding = Encoding.Unicode;
        using (var p = Process.Start(psi)) {
            if (!p.WaitForExit(15000)) { try { p.Kill(); } catch (Exception) { } throw new Exception("WSL 상태를 확인하지 못했어요. 디스크는 건드리지 않았어요."); }
            if (p.ExitCode != 0) throw new Exception("WSL 상태를 확인하지 못했어요. 디스크는 건드리지 않았어요.");
            string text = p.StandardOutput.ReadToEnd().Replace("\0", "");
            var list = new List<string>();
            foreach (string line in Regex.Split(text, "[\r\n]+")) { string t = line.Trim(); if (t.Length > 0) list.Add(t); }
            return list;
        }
    }
    // 이미지가 연결돼 있는지 보고, dismount가 true면 연결을 해제해요(파일은 그대로).
    static bool DiskImageAttached(string path, bool dismount) {
        var scope = new ManagementScope(@"\\.\root\Microsoft\Windows\Storage");
        scope.Connect();
        string escaped = path.Replace("\\", "\\\\").Replace("'", "\\'");
        var q = new ObjectQuery("SELECT * FROM MSFT_DiskImage WHERE ImagePath='" + escaped + "'");
        using (var searcher = new ManagementObjectSearcher(scope, q)) {
            foreach (ManagementObject image in searcher.Get()) {
                bool attached = Convert.ToBoolean(image["Attached"]);
                if (!attached) return false;
                if (!dismount) return true;
                image.InvokeMethod("Dismount", null);
                // 해제됐는지 다시 확인해요.
                using (var again = new ManagementObjectSearcher(scope, q)) {
                    foreach (ManagementObject i2 in again.Get()) if (Convert.ToBoolean(i2["Attached"])) throw new Exception("디스크 연결이 아직 남아 있어요.");
                }
                return true;
            }
        }
        throw new Exception("디스크 이미지 정보를 읽지 못했어요.");
    }

    // ---------- install-recovery (시작 메뉴 바로가기) ----------
    static int InstallRecovery() {
        string folder = null;
        for (int i = 0; i + 1 < rest.Count; i++) if (rest[i] == "--shortcut-dir") folder = rest[i + 1];
        if (string.IsNullOrEmpty(folder)) folder = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
        string self = Process.GetCurrentProcess().MainModule.FileName;
        dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
        dynamic lnk = shell.CreateShortcut(Path.Combine(folder, "Office 디스크 복구.lnk"));
        lnk.TargetPath = self;
        lnk.Arguments = "recover --dir \"" + dataDir + "\"";
        lnk.WorkingDirectory = dataDir;
        lnk.Description = "WSL을 켜지 않고 이전 디스크 줄이기의 연결을 확인해요.";
        lnk.Save();
        Print(Obj("ok", true));
        return 0;
    }

    // ---------- reopen (줄이기 끝나면 Office 새 콘솔로 열기) ----------
    static void SetReopen(string rid, string state, string message) {
        var cur = ReadJson(dataDir, "restore-office.json");
        if (Str(cur, "request_id") != rid) return;
        WriteAtomic(dataDir, "reopen-status.json", Json(Obj("request_id", rid, "status", state, "message", message, "updated_at", Now())));
    }
    static bool OfficeReady(string serverPath) {
        try {
            var server = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(serverPath, Encoding.UTF8).TrimStart('\uFEFF')) as Dictionary<string, object>;
            int port = (int)Num(server, "port");
            if (port < 1 || port > 65535) return false;
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/api/window");
            req.Proxy = null; req.Timeout = 2000;
            using (var resp = req.GetResponse())
            using (var reader = new StreamReader(resp.GetResponseStream(), Encoding.UTF8)) {
                var data = new JavaScriptSerializer().DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
                return IsTrue(data, "open");
            }
        } catch (Exception) { return false; }
    }
    class ProcInfo { public int Pid, Parent; public string Name, Cmd, Created; }
    static List<ProcInfo> SnapshotProcesses() {
        var list = new List<ProcInfo>();
        using (var s = new ManagementObjectSearcher("SELECT ProcessId,ParentProcessId,Name,CommandLine,CreationDate FROM Win32_Process"))
            foreach (ManagementObject o in s.Get()) {
                list.Add(new ProcInfo { Pid = Convert.ToInt32(o["ProcessId"]), Parent = Convert.ToInt32(o["ParentProcessId"]), Name = Convert.ToString(o["Name"]), Cmd = Convert.ToString(o["CommandLine"]), Created = Convert.ToString(o["CreationDate"]) });
            }
        return list;
    }
    static bool ConsoleCommandMatches(string cmd, Dictionary<string, object> cfg) {
        string distro = Str(cfg, "distro");
        if (string.IsNullOrEmpty(cmd) || cfg == null || string.IsNullOrEmpty(distro)) return false;
        if (!Regex.IsMatch(cmd, @"(^|\s)-d\s+""?" + Regex.Escape(distro) + @"""?(\s|$)")) return false;
        foreach (string path in new string[] { Str(cfg, "script_linux"), Str(cfg, "console_rc") }) {
            if (!string.IsNullOrEmpty(path) && Regex.IsMatch(cmd, @"(^|[\s""'])" + Regex.Escape(path) + @"([""']|\s|$)")) return true;
        }
        return false;
    }
    class ConsoleTarget { public int WslPid, HostPid; public string HostName, HostCreated; }
    static List<ConsoleTarget> SelectConsoles(List<ProcInfo> all, Dictionary<string, object> cfg, int excludePid) {
        var result = new List<ConsoleTarget>();
        foreach (var wsl in all) {
            if (!string.Equals(wsl.Name, "wsl.exe", StringComparison.OrdinalIgnoreCase) || wsl.Pid == excludePid) continue;
            if (!ConsoleCommandMatches(wsl.Cmd, cfg)) continue;
            var children = all.FindAll(delegate(ProcInfo p) { return p.Parent == wsl.Pid; });
            var hosts = children.FindAll(delegate(ProcInfo p) { return p.Name == "conhost.exe" || p.Name == "OpenConsole.exe"; });
            var inner = children.FindAll(delegate(ProcInfo p) { return string.Equals(p.Name, "wsl.exe", StringComparison.OrdinalIgnoreCase) && ConsoleCommandMatches(p.Cmd, cfg); });
            if (hosts.Count != 1 || children.Count != hosts.Count + inner.Count) continue;
            result.Add(new ConsoleTarget { WslPid = wsl.Pid, HostPid = hosts[0].Pid, HostName = hosts[0].Name, HostCreated = hosts[0].Created });
        }
        return result;
    }
    static List<ConsoleTarget> ConsoleSnapshot(Dictionary<string, object> cfg) {
        try { return SelectConsoles(SnapshotProcesses(), cfg, 0); } catch (Exception) { return new List<ConsoleTarget>(); }
    }
    static void CloseConsoles(List<ConsoleTarget> targets, Dictionary<string, object> cfg) {
        List<ConsoleTarget> fresh;
        try { fresh = SelectConsoles(SnapshotProcesses(), cfg, 0); } catch (Exception) { return; }
        foreach (var t in targets) {
            var same = fresh.FindAll(delegate(ConsoleTarget f) { return f.WslPid == t.WslPid && f.HostPid == t.HostPid && f.HostName == t.HostName && f.HostCreated == t.HostCreated; });
            if (same.Count != 1) continue;
            try { Process.GetProcessById(t.HostPid).Kill(); } catch (Exception) { }
        }
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct StartupInfo {
        public uint cb; public string reserved, desktop, title;
        public uint x, y, width, height, columns, rows, fill, flags;
        public ushort show, reservedBytes;
        public IntPtr reservedData, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process, thread; public uint id, threadId; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcessW(string application, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity, bool inheritHandles, uint flags, IntPtr environment, string directory, ref StartupInfo startup, out ProcessInfo process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    // 새 보이는 콘솔을 따로 만들어요. 파이프나 숨김 창을 물려받지 않아요.
    static uint StartVisibleConsole(string application, string arguments) {
        var startup = new StartupInfo(); startup.cb = (uint)Marshal.SizeOf(typeof(StartupInfo));
        startup.desktop = "winsta0\\default"; startup.title = "Office"; startup.flags = 1; startup.show = 1;
        ProcessInfo process;
        if (!CreateProcessW(application, new StringBuilder("\"" + application + "\" " + arguments), IntPtr.Zero, IntPtr.Zero, false, 0x00000010, IntPtr.Zero, Environment.SystemDirectory, ref startup, out process))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        try { return process.id; } finally { CloseHandle(process.thread); CloseHandle(process.process); }
    }
    static int Reopen() {
        HideConsole();
        string rid = rest.Count > 0 ? rest[0] : "";
        int waitSeconds = 28800;
        if (rest.Count > 1) int.TryParse(rest[1], out waitSeconds);
        if (waitSeconds < 1 || waitSeconds > 28800) waitSeconds = 28800;
        var cfg = ReadJson(dataDir, "restore-office.json");
        if (!Regex.IsMatch(rid, "^[A-Za-z0-9_-]{1,80}$") || Str(cfg, "request_id") != rid) return 4;
        if (!IsTrue(cfg, "available")) { SetReopen(rid, "unavailable", "Office 실행 정보를 찾지 못했어요. WSL에서 직접 office를 실행하세요."); return 0; }
        foreach (string v in new string[] { Str(cfg, "distro"), Str(cfg, "linux_user"), Str(cfg, "script_linux") })
            if (v == null || Regex.IsMatch(v, "[\"\r\n]")) throw new Exception("Office 복귀 정보가 올바르지 않아요.");
        DateTime started = DateTime.Now;
        var old = ConsoleSnapshot(cfg);
        try {
            SetReopen(rid, "waiting", "디스크 줄이기가 끝나면 Office를 새 콘솔에서 열어요.");
            while ((DateTime.Now - started).TotalSeconds < waitSeconds) {
                if (Str(ReadJson(dataDir, "restore-office.json"), "request_id") != rid) return 0;
                var rec = ReadJson(dataDir, "status.json");
                if (Str(rec, "request_id") == rid) {
                    string st = Str(rec, "status");
                    if (st == "cancelled" || st == "failed") { SetReopen(rid, "skipped", "줄이기가 끝나지 않아 Office 자동 열기를 생략했어요."); return 0; }
                    if (st == "completed" && IsTrue(rec, "wsl_ready") && !IsLocked(dataDir, "active.lock")) break;
                }
                Thread.Sleep(1000);
            }
            var done = ReadJson(dataDir, "status.json");
            if (Str(done, "request_id") != rid || Str(done, "status") != "completed" || !IsTrue(done, "wsl_ready") || IsLocked(dataDir, "active.lock")) throw new Exception("완료와 디스크 사용 해제를 확인하지 못했어요.");
            if (Str(ReadJson(dataDir, "restore-office.json"), "request_id") != rid) return 0;
            string server = Str(cfg, "server_windows");
            if (OfficeReady(server)) { SetReopen(rid, "opened", "Office가 이미 열려 있어요. 기존 창을 사용하세요."); return 0; }
            SetReopen(rid, "launching", "Office를 새 콘솔에서 열고 있어요.");
            string args = "-d " + Quote(Str(cfg, "distro")) + " -u " + Quote(Str(cfg, "linux_user")) + " --exec /bin/bash " + Quote(Str(cfg, "script_linux"));
            StartVisibleConsole(Path.Combine(Environment.GetEnvironmentVariable("WINDIR") ?? @"C:\Windows", @"System32\wsl.exe"), args);
            for (int i = 0; i < 45; i++) {
                if (OfficeReady(server)) {
                    CloseConsoles(old, ReadJson(dataDir, "restore-office.json"));
                    SetReopen(rid, "opened", "Office 창을 열었어요. 새 콘솔은 계속 켜 두세요."); return 0;
                }
                Thread.Sleep(2000);
            }
            throw new Exception("Office 창을 확인하지 못했어요. 서버는 실행 중일 수 있으니 열린 콘솔의 안내를 확인하세요.");
        } catch (Exception e) {
            SetReopen(rid, "failed", OneLine(e.Message)); return 4;
        }
    }
}

// 가상 디스크 줄이기. AttachVirtualDisk를 부르지 않아서, 중단되거나 프로세스가 죽어도 디스크가 연결된 채 남지 않아요.
public sealed class CompactOperation : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct StorageType { public uint DeviceId; public Guid VendorId; }
    [StructLayout(LayoutKind.Sequential)] struct OpenParameters { public uint Version; public uint RWDepth; }
    [StructLayout(LayoutKind.Sequential)] struct Overlapped { public UIntPtr Internal, InternalHigh; public uint Offset, OffsetHigh; public IntPtr Event; }
    [StructLayout(LayoutKind.Sequential)] struct Progress { public uint Status; public ulong Current, Total; }
    [DllImport("virtdisk.dll", CharSet = CharSet.Unicode)] static extern uint OpenVirtualDisk(ref StorageType type, string path, uint access, uint flags, ref OpenParameters parameters, out IntPtr handle);
    [DllImport("virtdisk.dll")] static extern uint CompactVirtualDisk(IntPtr handle, uint flags, IntPtr parameters, IntPtr overlapped);
    [DllImport("virtdisk.dll")] static extern uint GetVirtualDiskOperationProgress(IntPtr handle, IntPtr overlapped, out Progress progress);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetOverlappedResult(IntPtr handle, IntPtr overlapped, out uint bytes, bool wait);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CancelIoEx(IntPtr handle, IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateEvent(IntPtr attributes, bool manualReset, bool initialState, string name);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr handle);
    IntPtr disk, eventHandle, overlap;
    public bool Complete { get; private set; }
    public uint ErrorCode { get; private set; }
    public int Percent { get; private set; }
    public bool CancelRequested { get; private set; }
    public CompactOperation(string path) {
        Percent = -1;
        var storage = new StorageType { DeviceId = 3, VendorId = new Guid("EC984AEC-A0F9-47e9-901F-71415A66345B") };
        var open = new OpenParameters { Version = 1, RWDepth = 1 };
        uint result = OpenVirtualDisk(ref storage, path, 0x00200000, 0, ref open, out disk);
        if (result != 0) { Complete = true; Dispose(); throw new Win32Exception((int)result); }
        eventHandle = CreateEvent(IntPtr.Zero, true, false, null);
        if (eventHandle == IntPtr.Zero) { Complete = true; Dispose(); throw new Win32Exception(); }
        overlap = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(Overlapped)));
        Marshal.StructureToPtr(new Overlapped { Event = eventHandle }, overlap, false);
        result = CompactVirtualDisk(disk, 0, IntPtr.Zero, overlap);
        if (result != 997) { Complete = true; ErrorCode = result; Percent = result == 0 ? 100 : -1; }
    }
    public void Poll() {
        if (Complete) return;
        Progress progress;
        uint result = GetVirtualDiskOperationProgress(disk, overlap, out progress);
        if (result != 0) {
            uint bytes;
            bool success = GetOverlappedResult(disk, overlap, out bytes, false);
            int error = success ? 0 : Marshal.GetLastWin32Error();
            if (error == 996) return; // ERROR_IO_INCOMPLETE: 아직 진행 중이라 메모리를 그대로 둬요.
            if (!success && WaitForSingleObject(eventHandle, 0) != 0) throw new Win32Exception((int)result);
            Complete = true; ErrorCode = (uint)error; if (success) Percent = 100;
            return;
        }
        if (progress.Total > 0) Percent = (int)Math.Min(100, (double)progress.Current * 100 / progress.Total);
        if (progress.Status != 997) { Complete = true; ErrorCode = progress.Status; if (ErrorCode == 0) Percent = 100; }
    }
    public void Cancel() {
        if (Complete || CancelRequested) return;
        if (!CancelIoEx(disk, overlap)) {
            int error = Marshal.GetLastWin32Error();
            if (error != 1168) throw new Win32Exception(error);
        }
        CancelRequested = true;
    }
    public void Dispose() {
        // 취소는 요청일 뿐이라, 끝나기 전에는 OVERLAPPED를 해제하지 않아요.
        if (!Complete) throw new InvalidOperationException("작업이 끝나기 전에는 핸들을 해제할 수 없어요.");
        if (disk != IntPtr.Zero) { CloseHandle(disk); disk = IntPtr.Zero; }
        if (eventHandle != IntPtr.Zero) { CloseHandle(eventHandle); eventHandle = IntPtr.Zero; }
        if (overlap != IntPtr.Zero) { Marshal.FreeHGlobal(overlap); overlap = IntPtr.Zero; }
    }
}

// compact.ps1을 옮긴 진행창. 상태 파일 이름과 키는 그대로예요.
public static class Worker {
    [DllImport("kernel32.dll")] static extern bool FreeConsole();
    static string dir, vhdx, distro, rid, testMode = "none";
    static int testSeconds = 1;
    static bool testing, noDialog;
    static FileStream lockStream;
    static CompactOperation operation;
    static Process shutdownProcess;
    static string phase = "countdown";
    static bool terminal, cancelRequested, shutdownStarted, tickBusy;
    static DateTime started, changed, shutdownAt, operationStarted;
    static DateTime? finalizingStarted;
    static int lastPercent = -1, countdown;
    static double before, freeBefore;
    static DriveInfo drive;
    static string errorText = "";
    static Form form;
    static Label heading, detail, progressLabel, timing, hint;
    static ProgressBar bar;
    static Button cancelButton, startNow;
    static System.Windows.Forms.Timer timer, reopenTimer;

    static void Publish(string state, string message, List<KeyValuePair<string, object>> extra) {
        var f = OfficeWin.Obj("phase", phase, "elapsed_seconds", (int)(DateTime.Now - started).TotalSeconds,
            "can_cancel", (!terminal && !cancelRequested), "wsl_ready", !shutdownStarted, "recovery_required", false, "test_mode", testing);
        if (extra != null) foreach (var kv in extra) OfficeWin.Set(f, kv.Key, kv.Value);
        OfficeWin.WriteStatus(dir, rid, state, message, f);
    }
    static double Round2(double v) { return Math.Round(v, 2); }

    static void Finish(string state, string message) {
        if (operation != null) { operation.Dispose(); operation = null; }
        terminal = true; phase = "finished"; timer.Stop();
        progressLabel.Text = state == "completed" ? "완료" : (state == "cancelled" ? "중단됨" : "오류");
        double after = testing ? (state == "completed" ? 8.0 : 10.0) : new FileInfo(vhdx).Length / 1073741824.0;
        double freeAfter = testing ? 22.0 : drive.AvailableFreeSpace / 1073741824.0;
        var info = OfficeWin.Obj("wsl_ready", true, "can_cancel", false, "before_gb", Round2(before), "after_gb", Round2(after),
            "reclaimed_gb", Round2(Math.Max(0.0, before - after)), "c_free_before_gb", Round2(freeBefore), "c_free_after_gb", Round2(freeAfter));
        bar.Style = ProgressBarStyle.Continuous;
        if (state == "completed") { OfficeWin.Set(info, "percent", 100); bar.Value = 100; heading.Text = "디스크 줄이기가 끝났어요."; }
        else if (state == "cancelled") heading.Text = "중단하고 디스크 사용을 해제했어요.";
        else heading.Text = "디스크를 줄이지 못했어요.";
        detail.Text = message + "\r\n" + string.Format("디스크 파일 {0:N2}GiB → {1:N2}GiB", before, after);
        var reopen = OfficeWin.ReadJson(dir, "reopen-status.json");
        string rs = OfficeWin.Str(reopen, "status");
        bool auto = state == "completed" && OfficeWin.Str(reopen, "request_id") == rid && (rs == "waiting" || rs == "launching" || rs == "opened");
        hint.Text = auto ? "Office를 새 콘솔에서 자동으로 열어요. 이 창은 닫아도 돼요." : "WSL 터미널을 열고 Office를 다시 켜세요. 이 창을 닫아도 돼요.";
        timing.Text = string.Format("걸린 시간: {0:N0}초", (DateTime.Now - started).TotalSeconds);
        cancelButton.Text = "닫기"; cancelButton.Enabled = true; startNow.Visible = false;
        Publish(state, message, info);
        // 결과는 status.json 내용을 그대로 복사해 둬요.
        OfficeWin.WriteAtomic(dir, "result.json", File.ReadAllText(Path.Combine(dir, "status.json"), Encoding.UTF8).TrimStart('\uFEFF'));
        // 결과 창이 열려 있어도 이 창은 더 이상 작업 잠금을 쥐지 않아요.
        if (lockStream != null) { lockStream.Dispose(); lockStream = null; }
        if (auto && !testing) reopenTimer.Start();
        if (testing) form.Close();
    }
    static void RequestCancel() {
        if (terminal) { form.Close(); return; }
        if (cancelRequested) return;
        cancelRequested = true; cancelButton.Enabled = false; startNow.Enabled = false; heading.Text = "안전하게 중단하고 있어요."; progressLabel.Text = "중단 중";
        hint.Text = "중단 완료를 확인할 때까지 창을 유지해요. WSL을 아직 켜지 마세요.";
        if (operation != null) operation.Cancel();
        Publish("cancelling", "중단을 요청했어요. 디스크 사용이 끝날 때까지 기다려요.", OfficeWin.Obj("can_cancel", false));
    }
    static bool LeaseValid() {
        var lease = OfficeWin.ReadJson(dir, "launch-request.json");
        return lease != null && OfficeWin.Str(lease, "request_id") == rid && !OfficeWin.IsTrue(lease, "revoked") && OfficeWin.Num(lease, "expires_at") > OfficeWin.Now();
    }

    public static int Run(string dataDir, List<string> args) {
        dir = dataDir;
        for (int i = 0; i < args.Count; i++) {
            string a = args[i]; string next = i + 1 < args.Count ? args[i + 1] : null;
            if (a == "--vhdx" && next != null) { vhdx = next; i++; }
            else if (a == "--distro" && next != null) { distro = next; i++; }
            else if (a == "--request-id" && next != null) { rid = next; i++; }
            else if (a == "--no-dialog") noDialog = true;
            else if (a == "--test-mode" && next != null) { testMode = next; i++; }
            else if (a == "--test-seconds" && next != null) { int.TryParse(next, out testSeconds); i++; }
        }
        testing = testMode != "none";
        if (string.IsNullOrEmpty(rid)) rid = "manual";
        try {
            if (string.IsNullOrEmpty(vhdx) || string.IsNullOrEmpty(distro)) throw new Exception("대상 디스크와 배포판 정보를 확인하지 못했어요.");
            if (testing && !Path.GetFullPath(dir).StartsWith(OfficeWin.TempRoot(), StringComparison.OrdinalIgnoreCase)) throw new Exception("시험은 Windows 임시 폴더에서만 실행해요.");
            if (!testing && !OfficeWin.IsAdmin()) throw new Exception("관리자 권한이 필요해요. WSL은 끄지 않았어요.");
            if (!testing && !LeaseValid()) throw new Exception("유효한 시작 요청이 없어요. WSL은 끄지 않았어요.");
            try { lockStream = new FileStream(Path.Combine(dir, "active.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
            catch (IOException) { return 3; }
            try { FreeConsole(); } catch (Exception) { }
            Application.EnableVisualStyles();
            started = DateTime.Now; changed = started;
            countdown = testing ? Math.Max(0, testSeconds) : 60;
            shutdownAt = DateTime.Now.AddSeconds(countdown);
            if (!testing) drive = new DriveInfo(Path.GetPathRoot(vhdx));
            before = testing ? 10.0 : new FileInfo(vhdx).Length / 1073741824.0;
            freeBefore = testing ? 20.0 : drive.AvailableFreeSpace / 1073741824.0;
            BuildForm();
            Application.Run(form);
            return 0;
        } catch (Exception e) {
            try { OfficeWin.WriteStatus(dir, rid, "failed", "실행창을 준비하지 못했어요. " + OfficeWin.OneLine(e.Message),
                OfficeWin.Obj("phase", "initialization", "wsl_ready", true, "can_cancel", false, "recovery_required", false, "test_mode", testing)); } catch (Exception) { }
            if (!testing && !noDialog) { try { MessageBox.Show(e.Message, "디스크 줄이기 오류"); } catch (Exception) { } }
            return 4;
        } finally {
            if (reopenTimer != null) reopenTimer.Dispose();
            if (lockStream != null) lockStream.Dispose();
            if (shutdownProcess != null) shutdownProcess.Dispose();
        }
    }

    static Label MakeLabel(int x, int y, int w, int h) { var l = new Label(); l.Location = new Point(x, y); l.Size = new Size(w, h); return l; }

    static void BuildForm() {
        form = new Form();
        form.Text = testing ? "WSL 디스크 줄이기 (시험)" : "WSL 디스크 줄이기";
        form.ClientSize = new Size(560, 330); form.StartPosition = FormStartPosition.CenterScreen; form.FormBorderStyle = FormBorderStyle.FixedDialog; form.MaximizeBox = false;
        form.Font = new Font("Malgun Gothic", 10); form.BackColor = Color.White;
        heading = MakeLabel(24, 22, 510, 32); heading.Font = new Font("Malgun Gothic", 14, FontStyle.Bold);
        detail = MakeLabel(24, 66, 510, 65);
        bar = new ProgressBar(); bar.Location = new Point(24, 145); bar.Size = new Size(420, 20); bar.Style = ProgressBarStyle.Continuous;
        progressLabel = MakeLabel(450, 140, 84, 30); progressLabel.TextAlign = ContentAlignment.MiddleCenter;
        timing = MakeLabel(24, 178, 510, 35);
        hint = MakeLabel(24, 215, 510, 45); hint.ForeColor = Color.DimGray;
        cancelButton = new Button(); cancelButton.Text = "중단하기"; cancelButton.Location = new Point(410, 275); cancelButton.Size = new Size(124, 35);
        startNow = new Button(); startNow.Text = "바로 실행"; startNow.Location = new Point(276, 275); startNow.Size = new Size(124, 35);
        form.Controls.AddRange(new Control[] { heading, detail, bar, progressLabel, timing, hint, startNow, cancelButton });
        startNow.Click += delegate { if (phase == "countdown" && !cancelRequested) { shutdownAt = DateTime.Now; startNow.Enabled = false; } };
        cancelButton.Click += delegate { RequestCancel(); };
        form.FormClosing += delegate(object s, FormClosingEventArgs e) { if (!terminal) { e.Cancel = true; RequestCancel(); } };
        reopenTimer = new System.Windows.Forms.Timer(); reopenTimer.Interval = 1000;
        reopenTimer.Tick += delegate {
            var r = OfficeWin.ReadJson(dir, "reopen-status.json");
            if (OfficeWin.Str(r, "request_id") != rid) return;
            string st = OfficeWin.Str(r, "status");
            if (st == "opened") { hint.Text = "Office 창을 열었어요. 새 콘솔은 계속 켜 두세요."; reopenTimer.Stop(); }
            else if (st == "failed" || st == "unavailable") { hint.Text = "Office를 자동으로 열지 못했어요. WSL 터미널에서 office를 실행하세요."; reopenTimer.Stop(); }
        };
        timer = new System.Windows.Forms.Timer(); timer.Interval = 500;
        timer.Tick += delegate { Tick(); };
        form.Shown += delegate {
            // 실행기가 잠금을 놓기 전에 상태를 먼저 알려요. 진행은 이 창에서만 보여줘요.
            Publish("countdown", "관리자 권한을 확인했어요. Windows 진행창에서 남은 시간을 확인하세요.",
                OfficeWin.Obj("shutdown_at", new DateTimeOffset(shutdownAt).ToUnixTimeSeconds()));
            form.Activate(); timer.Start();
        };
    }

    static void Tick() {
        if (tickBusy) return; tickBusy = true;
        try {
            var request = OfficeWin.ReadJson(dir, "cancel.json");
            if (OfficeWin.Str(request, "request_id") == rid) RequestCancel();
            int elapsed = (int)(DateTime.Now - started).TotalSeconds;
            timing.Text = "진행 시간: " + elapsed + "초";
            if (phase == "countdown") {
                if (cancelRequested) { Finish("cancelled", "WSL을 끄기 전에 취소했어요."); return; }
                int left = Math.Max(0, (int)Math.Ceiling((shutdownAt - DateTime.Now).TotalSeconds));
                heading.Text = left + "초 뒤에 WSL을 꺼요."; progressLabel.Text = "시작 대기 중";
                detail.Text = "관리자 권한을 확인했어요. Office를 포함한 WSL 작업이 모두 멈춰요.";
                hint.Text = "지금 중단하면 WSL은 켜진 상태로 유지돼요.";
                bar.Value = countdown == 0 ? 100 : Math.Min(100, (int)(100.0 * (countdown - left) / countdown));
                Publish("countdown", left + "초 뒤에 WSL을 꺼요. 중단하려면 [중단하기]를 누르세요.",
                    OfficeWin.Obj("shutdown_at", new DateTimeOffset(shutdownAt).ToUnixTimeSeconds(), "remaining_seconds", left));
                if (left > 0) return;
                if (!testing && !LeaseValid()) { Finish("cancelled", "시작 요청이 만료됐어요. WSL은 끄지 않았어요."); return; }
                startNow.Visible = false; bar.Value = 0; phase = "stopping"; shutdownStarted = true; changed = DateTime.Now;
                heading.Text = "WSL이 종료되기를 기다려요."; hint.Text = "이 Windows 창에서 작업 상태를 계속 보여줘요."; bar.Style = ProgressBarStyle.Marquee; progressLabel.Text = "진행 중";
                if (!testing) {
                    var psi = new ProcessStartInfo(Path.Combine(Environment.GetEnvironmentVariable("WINDIR") ?? @"C:\Windows", @"System32\wsl.exe"), "--shutdown");
                    psi.UseShellExecute = false; psi.CreateNoWindow = true;
                    shutdownProcess = Process.Start(psi);
                }
                Publish("stopping", "WSL이 종료되기를 기다리고 있어요.", null); return;
            }
            if (phase == "stopping") {
                if (shutdownProcess != null && !shutdownProcess.HasExited) {
                    if ((DateTime.Now - changed).TotalSeconds > 120) { shutdownProcess.Kill(); throw new Exception("WSL 종료가 2분 안에 끝나지 않았어요. 디스크 축소는 시작하지 않았어요."); }
                    Publish(cancelRequested ? "cancelling" : "stopping", "WSL 종료가 끝나기를 기다리고 있어요.", null); return;
                }
                if (shutdownProcess != null && shutdownProcess.ExitCode != 0) throw new Exception("WSL을 종료하지 못했어요. 디스크 축소는 시작하지 않았어요.");
                if (cancelRequested) { Finish("cancelled", "WSL 종료 뒤 축소를 시작하기 전에 취소했어요."); return; }
                phase = "running"; changed = DateTime.Now; operationStarted = DateTime.Now;
                if (!testing) operation = new CompactOperation(vhdx);
                heading.Text = "디스크를 줄이고 있어요."; hint.Text = "중단할 수 있어요. 중단이 끝난 뒤 WSL을 다시 켜세요."; return;
            }
            if (phase == "running" || phase == "finalizing") {
                int percent; bool complete; uint code;
                if (testing) {
                    double since = (DateTime.Now - operationStarted).TotalSeconds;
                    if (testMode == "cancel" && !cancelRequested) RequestCancel();
                    if (testMode == "unknown") percent = -1; else if (testMode == "pending100") percent = 100; else if (testMode == "stall") percent = 45; else percent = Math.Min(100, (int)(since * 35));
                    complete = (testMode == "unknown" && since >= 5) || (percent >= 100 && (testMode != "pending100" || since >= 5)) || cancelRequested;
                    code = cancelRequested ? 995u : (testMode == "failure" ? 5u : 0u);
                } else {
                    operation.Poll(); percent = operation.Percent; complete = operation.Complete; code = operation.ErrorCode;
                }
                if (complete) {
                    if (code == 0) Finish("completed", "줄이기를 마쳤어요. 실제 확보 용량은 아래 결과를 확인하세요.");
                    else if (code == 995) Finish("cancelled", "중단을 마쳤어요. 디스크 사용을 해제했어요.");
                    else Finish("failed", "축소가 끝나지 않았어요. Windows 오류 코드: " + code + ". 디스크 사용을 해제했어요.");
                    return;
                }
                if (percent != lastPercent) { lastPercent = percent; changed = DateTime.Now; }
                int stalled = (int)(DateTime.Now - changed).TotalSeconds;
                var fields = OfficeWin.Obj("stalled_seconds", stalled);
                if (percent >= 100) {
                    phase = "finalizing";
                    if (!finalizingStarted.HasValue) finalizingStarted = DateTime.Now;
                    heading.Text = cancelRequested ? "안전하게 중단하고 있어요." : "디스크 줄이기 진행 중이에요.";
                    bar.Value = 0; bar.Style = ProgressBarStyle.Marquee; progressLabel.Text = cancelRequested ? "중단 중" : "진행 중";
                    detail.Text = "";
                } else {
                    phase = "running"; finalizingStarted = null;
                    if (!cancelRequested) heading.Text = "디스크를 줄이고 있어요.";
                    if (percent >= 0) { bar.Style = ProgressBarStyle.Continuous; bar.Value = percent; progressLabel.Text = percent + "%"; detail.Text = ""; OfficeWin.Set(fields, "percent", percent); }
                    else { bar.Value = 0; bar.Style = ProgressBarStyle.Marquee; progressLabel.Text = "진행 중"; if (!cancelRequested) heading.Text = "디스크 줄이기 진행 중이에요."; detail.Text = ""; }
                    if (percent >= 0 && stalled >= 30) detail.Text += "\r\n" + stalled + "초 동안 같은 진행률이에요. 처리 중에도 숫자가 멈출 수 있어요.";
                }
                if (cancelRequested) progressLabel.Text = "중단 중";
                Publish(cancelRequested ? "cancelling" : "running", detail.Text, fields);
            }
        } catch (Exception e) {
            errorText = e.Message;
            if (operation != null && !operation.Complete) {
                try { RequestCancel(); } catch (Exception) { }
                heading.Text = "작업 상태를 확인하고 있어요."; detail.Text = errorText;
                Publish("uncertain", "상태 확인에 실패했어요. 이 창에서 중단 결과를 기다리세요. " + errorText, OfficeWin.Obj("can_cancel", false));
            } else Finish("failed", errorText);
        } finally { tickBusy = false; }
    }
}

// 시험용: 임시 폴더 안에 아주 작은 가상 디스크를 만들고 실제 CompactVirtualDisk를 돌려 봐요. WSL은 건드리지 않아요.
public static class SelfTest {
    [StructLayout(LayoutKind.Sequential)] struct StorageType { public uint DeviceId; public Guid VendorId; }
    [StructLayout(LayoutKind.Sequential)] struct CreateParameters {
        public uint Version; public Guid UniqueId; public ulong MaximumSize; public uint BlockSizeInBytes, SectorSizeInBytes;
        public IntPtr ParentPath, SourcePath;
    }
    [DllImport("virtdisk.dll", CharSet = CharSet.Unicode)] static extern uint CreateVirtualDisk(ref StorageType type, string path, uint access, IntPtr security, uint flags, uint provider, ref CreateParameters parameters, IntPtr overlapped, out IntPtr handle);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    static void RequireTemp(string path) {
        if (!Path.GetFullPath(path).StartsWith(OfficeWin.TempRoot(), StringComparison.OrdinalIgnoreCase) || !path.EndsWith(".vhdx", StringComparison.OrdinalIgnoreCase)) throw new Exception("시험 디스크는 임시 폴더의 .vhdx만 써요.");
    }
    public static int MakeVhdx(List<string> a) {
        if (a.Count < 1) return 64; RequireTemp(a[0]);
        var type = new StorageType { DeviceId = 3, VendorId = new Guid("EC984AEC-A0F9-47e9-901F-71415A66345B") };
        var p = new CreateParameters { Version = 1, MaximumSize = 64UL * 1024 * 1024 };
        IntPtr h; uint r = CreateVirtualDisk(ref type, a[0], 0, IntPtr.Zero, 0, 0, ref p, IntPtr.Zero, out h);
        if (r != 0) throw new Win32Exception((int)r);
        CloseHandle(h);
        OfficeWin.Print(OfficeWin.Obj("ok", true));
        return 0;
    }
    public static int Compact(List<string> a) {
        if (a.Count < 1) return 64; RequireTemp(a[0]);
        using (var op = new CompactOperation(a[0])) {
            for (int i = 0; i < 600 && !op.Complete; i++) { op.Poll(); if (!op.Complete) Thread.Sleep(100); }
            OfficeWin.Print(OfficeWin.Obj("complete", op.Complete, "error", (long)op.ErrorCode, "percent", op.Percent));
            return op.Complete && op.ErrorCode == 0 ? 0 : 2;
        }
    }
}
