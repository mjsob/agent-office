using System;using System.IO;using System.Runtime.InteropServices;using System.Windows.Forms;
[StructLayout(LayoutKind.Sequential)] struct Key {public Guid id;public uint pid;}
[StructLayout(LayoutKind.Explicit,Size=24)]struct Value{[FieldOffset(0)]public ushort vt;[FieldOffset(8)]public IntPtr text;}
[ComImport,Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]interface Store{
[PreserveSig]int Count(out uint n);[PreserveSig]int At(uint i,out Key k);[PreserveSig]int Get(ref Key k,out Value v);[PreserveSig]int Set(ref Key k,ref Value v);[PreserveSig]int Commit();}
class QuietForm:Form {protected override bool ShowWithoutActivation{get{return true;}}}
class WindowsFixture{
[DllImport("shell32.dll")]static extern int SHGetPropertyStoreForWindow(IntPtr h,ref Guid id,out Store s);
[DllImport("user32.dll")]static extern IntPtr GetForegroundWindow();
static void Brand(Form f,string id){Store s;Guid iid=typeof(Store).GUID;Marshal.ThrowExceptionForHR(SHGetPropertyStoreForWindow(f.Handle,ref iid,out s));try{Key k=new Key{id=new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),pid=5};Value v=new Value{vt=31,text=Marshal.StringToCoTaskMemUni(id)};try{Marshal.ThrowExceptionForHR(s.Set(ref k,ref v));s.Commit();}finally{Marshal.FreeCoTaskMem(v.text);}}finally{Marshal.ReleaseComObject(s);}}
[STAThread]static void Main(string[] a){Application.EnableVisualStyles();var forms=new Form[int.Parse(a[3])];for(int i=0;i<forms.Length;i++){forms[i]=new QuietForm{Text=a[1],WindowState=FormWindowState.Minimized,ShowInTaskbar=true};Brand(forms[i],a[2]);forms[i].Show();}File.WriteAllText(a[0],GetForegroundWindow().ToInt64().ToString());var deadline=DateTime.UtcNow.AddSeconds(15);var timer=new Timer{Interval=100};timer.Tick+=(s,e)=>{if(File.Exists(a[0]+".stop") || DateTime.UtcNow>deadline)Application.Exit();};timer.Start();Application.Run();}
}
