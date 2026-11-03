using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

// No AttachVirtualDisk call: a cancelled or terminated worker cannot leave a mounted disk behind.
public sealed class CompactOperation : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct StorageType { public uint DeviceId; public Guid VendorId; }
    [StructLayout(LayoutKind.Sequential)] struct OpenParameters { public uint Version; public uint RWDepth; }
    [StructLayout(LayoutKind.Sequential)] struct Overlapped { public UIntPtr Internal, InternalHigh; public uint Offset, OffsetHigh; public IntPtr Event; }
    [StructLayout(LayoutKind.Sequential)] struct Progress { public uint Status; public ulong Current, Total; }
    [DllImport("virtdisk.dll", CharSet=CharSet.Unicode)] static extern uint OpenVirtualDisk(ref StorageType type, string path, uint access, uint flags, ref OpenParameters parameters, out IntPtr handle);
    [DllImport("virtdisk.dll")] static extern uint CompactVirtualDisk(IntPtr handle, uint flags, IntPtr parameters, IntPtr overlapped);
    [DllImport("virtdisk.dll")] static extern uint GetVirtualDiskOperationProgress(IntPtr handle, IntPtr overlapped, out Progress progress);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetOverlappedResult(IntPtr handle, IntPtr overlapped, out uint bytes, bool wait);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CancelIoEx(IntPtr handle, IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateEvent(IntPtr attributes, bool manualReset, bool initialState, string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
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
            // Completion can still be obtained when the progress query itself fails.
            uint bytes;
            bool success = GetOverlappedResult(disk, overlap, out bytes, false);
            int error = success ? 0 : Marshal.GetLastWin32Error();
            if (error == 996) return; // ERROR_IO_INCOMPLETE: retain all native memory.
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
        // CancelIoEx only requests cancellation. Never free OVERLAPPED before terminal completion.
        if (!Complete) throw new InvalidOperationException("Wait for operation completion before releasing its handle.");
        if (disk != IntPtr.Zero) { CloseHandle(disk); disk = IntPtr.Zero; }
        if (eventHandle != IntPtr.Zero) { CloseHandle(eventHandle); eventHandle = IntPtr.Zero; }
        if (overlap != IntPtr.Zero) { Marshal.FreeHGlobal(overlap); overlap = IntPtr.Zero; }
    }
}
