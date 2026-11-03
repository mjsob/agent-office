using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class OfficeVisibleConsole {
 [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct StartupInfo {
  public uint cb; public string reserved, desktop, title;
  public uint x,y,width,height,columns,rows,fill,flags;
  public ushort show,reservedBytes;
  public IntPtr reservedData,input,output,error;
 }
 [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr process,thread; public uint id,threadId; }
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern bool CreateProcessW(string application,StringBuilder command,IntPtr processSecurity,IntPtr threadSecurity,bool inheritHandles,uint flags,IntPtr environment,string directory,ref StartupInfo startup,out ProcessInfo process);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
 public static uint Start(string application,string arguments) {
  var startup=new StartupInfo();startup.cb=(uint)Marshal.SizeOf(typeof(StartupInfo));
  startup.desktop="winsta0\\default";startup.title="Office";startup.flags=1;startup.show=1;
  ProcessInfo process;
  // A fresh visible console with its own standard handles. No inherited pipes or hidden server.
  if(!CreateProcessW(application,new StringBuilder("\""+application+"\" "+arguments),IntPtr.Zero,IntPtr.Zero,false,0x00000010,IntPtr.Zero,Environment.SystemDirectory,ref startup,out process))
   throw new Win32Exception(Marshal.GetLastWin32Error());
  try{return process.id;}finally{CloseHandle(process.thread);CloseHandle(process.process);}
 }
}
