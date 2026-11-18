// proccap: record ONE process's audio (Windows process loopback, Win10 2004+) and mute/unmute it in the mixer.
//
//   proccap capture <pid> <seconds> <out.wav>   record that process (and its children) as 48 kHz 16-bit stereo
//   proccap mute <pid> <0|1>                     mute/unmute that process's sessions in the Windows volume mixer
//   proccap route <pid> <endpoint|default>      send that process to an output device (default = undo)
//   proccap getroute <pid>                       show the routing
//   proccap devices                              list active outputs: id, default flag, name
//
// Silence is padded by wall clock, so the WAV always lasts exactly <seconds> and stays in sync with video.
// Build: C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /out:proccap.exe proccap.cs
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Diagnostics;

static class Program
{
    // ------------------------------------------------------------------ COM interfaces
    [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        int Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity, IntPtr format, IntPtr sessionGuid);
        int GetBufferSize(out uint frames);
        int GetStreamLatency(out long latency);
        int GetCurrentPadding(out uint padding);
        int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closest);
        int GetMixFormat(out IntPtr format);
        int GetDevicePeriod(out long def, out long min);
        int Start();
        int Stop();
        int Reset();
        int SetEventHandle(IntPtr handle);
        int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }

    [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devPos, out ulong qpcPos);
        int ReleaseBuffer(uint frames);
        int GetNextPacketSize(out uint frames);
    }

    [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceAsyncOperation
    {
        int GetActivateResult(out int hr, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    }

    [ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceCompletionHandler
    {
        int ActivateCompleted(IActivateAudioInterfaceAsyncOperation op);
    }

    [ComImport, Guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAgileObject { }

    [ClassInterface(ClassInterfaceType.None)]
    class Handler : IActivateAudioInterfaceCompletionHandler, IAgileObject
    {
        public readonly ManualResetEvent Done = new ManualResetEvent(false);
        public int Hr;
        public object Iface;
        public int ActivateCompleted(IActivateAudioInterfaceAsyncOperation op)
        {
            int hr; object o;
            op.GetActivateResult(out hr, out o);
            Hr = hr; Iface = o;
            Done.Set();
            return 0;
        }
    }

    [StructLayout(LayoutKind.Explicit)]
    struct PropVariantBlob
    {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public uint cbSize;
        [FieldOffset(16)] public IntPtr pBlobData;
    }

    [DllImport("Mmdevapi.dll", ExactSpelling = true, PreserveSig = true)]
    static extern int ActivateAudioInterfaceAsync([MarshalAs(UnmanagedType.LPWStr)] string path, ref Guid riid,
        ref PropVariantBlob activationParams, IActivateAudioInterfaceCompletionHandler handler,
        out IActivateAudioInterfaceAsyncOperation op);

    // mixer (session) interfaces
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorCom { }

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator
    {
        int EnumAudioEndpoints(int flow, int mask, out IMMDeviceCollection devices);
        int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice device);
    }

    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceCollection
    {
        int GetCount(out int n);
        int Item(int i, out IMMDevice device);
    }

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice
    {
        int Activate(ref Guid iid, int clsCtx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        int OpenPropertyStore(int access, out IPropertyStore store);
        int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        int GetState(out int state);
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PropertyKey { public Guid fmtid; public int pid; }

    [StructLayout(LayoutKind.Explicit)]
    struct PropVariantPtr { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; [FieldOffset(16)] public IntPtr pad; }

    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore
    {
        int GetCount(out int n);
        int GetAt(int i, out PropertyKey key);
        int GetValue(ref PropertyKey key, out PropVariantPtr value);
    }

    // devices: one line per active output: "<id>	<default|->	<name>"
    static int Devices()
    {
        IMMDeviceEnumerator en = (IMMDeviceEnumerator)new MMDeviceEnumeratorCom();
        IMMDevice def; string defId = "";
        if (en.GetDefaultAudioEndpoint(0, 1, out def) == 0) def.GetId(out defId);
        IMMDeviceCollection col;
        en.EnumAudioEndpoints(0, 1, out col);   // eRender, DEVICE_STATE_ACTIVE
        int n; col.GetCount(out n);
        PropertyKey nameKey = new PropertyKey();
        nameKey.fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"); nameKey.pid = 14;
        for (int i = 0; i < n; i++)
        {
            IMMDevice d; col.Item(i, out d);
            string id; d.GetId(out id);
            IPropertyStore ps; d.OpenPropertyStore(0, out ps);
            PropVariantPtr v; ps.GetValue(ref nameKey, out v);
            string name = v.vt == 31 ? Marshal.PtrToStringUni(v.p) : "?";
            Console.WriteLine(id + "	" + (id == defId ? "default" : "-") + "	" + name);
        }
        return 0;
    }

    [ComImport, Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionManager2
    {
        int GetAudioSessionControl(IntPtr a, int b, out IntPtr c);
        int GetSimpleAudioVolume(IntPtr a, int b, out IntPtr c);
        int GetSessionEnumerator(out IAudioSessionEnumerator e);
    }

    [ComImport, Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionEnumerator
    {
        int GetCount(out int n);
        int GetSession(int i, [MarshalAs(UnmanagedType.IUnknown)] out object s);
    }

    [ComImport, Guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionControl2
    {
        int GetState(out int s);
        int GetDisplayName(out IntPtr n);
        int SetDisplayName(IntPtr n, IntPtr c);
        int GetIconPath(out IntPtr p);
        int SetIconPath(IntPtr p, IntPtr c);
        int GetGroupingParam(out Guid g);
        int SetGroupingParam(IntPtr g, IntPtr c);
        int RegisterAudioSessionNotification(IntPtr n);
        int UnregisterAudioSessionNotification(IntPtr n);
        int GetSessionIdentifier(out IntPtr s);
        int GetSessionInstanceIdentifier(out IntPtr s);
        int GetProcessId(out uint pid);
    }

    [ComImport, Guid("87CE5498-68D6-44E5-9215-6DA47EF883D8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface ISimpleAudioVolume
    {
        int SetMasterVolume(float v, IntPtr ctx);
        int GetMasterVolume(out float v);
        int SetMute(int mute, IntPtr ctx);
        int GetMute(out int mute);
    }

    // per-app output device routing (same as Settings > Sound > Volume mixer > app > Output device).
    // Undocumented WinRT class Windows.Media.Internal.AudioPolicyConfig (Windows 10 21H2+ / 11 layout).
    [ComImport, Guid("ab3d4648-e242-459f-b02f-541c70306324"), InterfaceType(ComInterfaceType.InterfaceIsIInspectable)]
    interface IAudioPolicyConfig
    {
        int m01(); int m02(); int m03(); int m04(); int m05(); int m06(); int m07(); int m08(); int m09(); int m10();
        int m11(); int m12(); int m13(); int m14(); int m15(); int m16(); int m17(); int m18(); int m19();
        [PreserveSig] int SetPersistedDefaultAudioEndpoint(uint processId, int flow, int role, IntPtr deviceId);
        [PreserveSig] int GetPersistedDefaultAudioEndpoint(uint processId, int flow, int role, out IntPtr deviceId);
    }

    [DllImport("combase.dll", PreserveSig = true)]
    static extern int WindowsCreateString([MarshalAs(UnmanagedType.LPWStr)] string s, int len, out IntPtr h);
    [DllImport("combase.dll", PreserveSig = true)]
    static extern int WindowsDeleteString(IntPtr h);
    [DllImport("combase.dll", PreserveSig = true)]
    static extern IntPtr WindowsGetStringRawBuffer(IntPtr h, out int len);
    [DllImport("combase.dll", PreserveSig = true)]
    static extern int RoGetActivationFactory(IntPtr classId, ref Guid iid, [MarshalAs(UnmanagedType.IInspectable)] out object factory);

    const string RENDER_PREFIX = "\\\\?\\SWD#MMDEVAPI#", RENDER_SUFFIX = "#{e6327cad-dcec-4949-ae8a-991e976a79d2}";

    static IAudioPolicyConfig PolicyConfig()
    {
        string cls = "Windows.Media.Internal.AudioPolicyConfig";
        IntPtr h; WindowsCreateString(cls, cls.Length, out h);
        Guid iid = typeof(IAudioPolicyConfig).GUID;
        object f;
        int hr = RoGetActivationFactory(h, ref iid, out f);
        WindowsDeleteString(h);
        if (hr != 0) throw new Exception("AudioPolicyConfig unavailable 0x" + hr.ToString("X8"));
        return (IAudioPolicyConfig)f;
    }

    // route <pid> <endpoint id like {0.0.0.00000000}.{GUID} | default>
    static int Route(uint pid, string endpoint)
    {
        IAudioPolicyConfig cfg = PolicyConfig();
        IntPtr h = IntPtr.Zero;
        if (endpoint != "default")
        {
            string full = RENDER_PREFIX + endpoint + RENDER_SUFFIX;
            WindowsCreateString(full, full.Length, out h);
        }
        int bad = 0;
        foreach (int role in new int[] { 0, 1 })   // eConsole, eMultimedia
        {
            int hr = cfg.SetPersistedDefaultAudioEndpoint(pid, 0, role, h);   // eRender
            if (hr != 0) { Console.Error.WriteLine("set role " + role + ": 0x" + hr.ToString("X8")); bad++; }
        }
        if (h != IntPtr.Zero) WindowsDeleteString(h);
        Console.WriteLine("route: " + RouteOf(cfg, pid));
        return bad == 0 ? 0 : 1;
    }

    static string RouteOf(IAudioPolicyConfig cfg, uint pid)
    {
        IntPtr h;
        if (cfg.GetPersistedDefaultAudioEndpoint(pid, 0, 1, out h) != 0 || h == IntPtr.Zero) return "default";
        int len; IntPtr raw = WindowsGetStringRawBuffer(h, out len);
        string s = Marshal.PtrToStringUni(raw, len);
        WindowsDeleteString(h);
        return string.IsNullOrEmpty(s) ? "default" : s;
    }

    // ------------------------------------------------------------------ main
    const int RATE = 48000, CH = 2, BITS = 16, BLOCK = CH * BITS / 8;

    [MTAThread]
    static int Main(string[] args)
    {
        try
        {
            if (args.Length == 4 && args[0] == "capture")
                return Capture(uint.Parse(args[1]), double.Parse(args[2], System.Globalization.CultureInfo.InvariantCulture), args[3]);
            if (args.Length == 3 && args[0] == "mute")
                return Mute(uint.Parse(args[1]), args[2] == "1");
            if (args.Length == 3 && args[0] == "route")
                return Route(uint.Parse(args[1]), args[2]);
            if (args.Length == 1 && args[0] == "devices")
                return Devices();
            if (args.Length == 2 && args[0] == "getroute")
            {
                Console.WriteLine(RouteOf(PolicyConfig(), uint.Parse(args[1])));
                return 0;
            }
            Console.Error.WriteLine("usage: proccap capture <pid> <seconds> <out.wav> | mute <pid> <0|1> | route <pid> <endpoint|default> | getroute <pid> | devices");
            return 2;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("error: " + e.Message);
            return 1;
        }
    }

    static int Capture(uint pid, double seconds, string outPath)
    {
        // AUDIOCLIENT_ACTIVATION_PARAMS { ActivationType = PROCESS_LOOPBACK, { TargetProcessId, INCLUDE_TARGET_PROCESS_TREE } }
        IntPtr p = Marshal.AllocHGlobal(12);
        Marshal.WriteInt32(p, 0, 1);
        Marshal.WriteInt32(p, 4, (int)pid);
        Marshal.WriteInt32(p, 8, 0);
        PropVariantBlob pv = new PropVariantBlob();
        pv.vt = 65; // VT_BLOB
        pv.cbSize = 12;
        pv.pBlobData = p;

        Guid iidClient = typeof(IAudioClient).GUID;
        Handler h = new Handler();
        IActivateAudioInterfaceAsyncOperation op;
        int hr = ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref iidClient, ref pv, h, out op);
        if (hr != 0) throw new Exception("ActivateAudioInterfaceAsync 0x" + hr.ToString("X8"));
        if (!h.Done.WaitOne(5000)) throw new Exception("activation timed out");
        if (h.Hr != 0) throw new Exception("activation failed 0x" + h.Hr.ToString("X8"));
        IAudioClient client = (IAudioClient)h.Iface;

        IntPtr fmt = Marshal.AllocHGlobal(18);
        Marshal.WriteInt16(fmt, 0, 1);            // PCM
        Marshal.WriteInt16(fmt, 2, CH);
        Marshal.WriteInt32(fmt, 4, RATE);
        Marshal.WriteInt32(fmt, 8, RATE * BLOCK);
        Marshal.WriteInt16(fmt, 12, BLOCK);
        Marshal.WriteInt16(fmt, 14, BITS);
        Marshal.WriteInt16(fmt, 16, 0);
        const uint LOOPBACK = 0x00020000, AUTOCONVERT = 0x80000000, SRC_QUALITY = 0x08000000;
        hr = client.Initialize(0, LOOPBACK | AUTOCONVERT | SRC_QUALITY, 2000000, 0, fmt, IntPtr.Zero);
        if (hr != 0) throw new Exception("Initialize 0x" + hr.ToString("X8"));
        Guid iidCap = typeof(IAudioCaptureClient).GUID;
        object capObj;
        hr = client.GetService(ref iidCap, out capObj);
        if (hr != 0) throw new Exception("GetService 0x" + hr.ToString("X8"));
        IAudioCaptureClient cap = (IAudioCaptureClient)capObj;

        long totalFrames = (long)(seconds * RATE);
        // Every packet is written at the position its capture time stamp says (time since our start, both on
        // the performance counter), not appended: while the game is paused or silent Windows may deliver no
        // packets and later deliver several late at once, which appended would stretch the file's timeline.
        byte[] pcm = new byte[totalFrames * BLOCK];
        long written = 0;
        Stopwatch sw = Stopwatch.StartNew();
        client.Start();
        double t0 = (double)Stopwatch.GetTimestamp() / Stopwatch.Frequency;
        // the wav's time 0 on the system's performance counter (same clock as Python's time.perf_counter)
        Console.WriteLine("start " + Stopwatch.GetTimestamp() + " " + Stopwatch.Frequency);
        Console.Out.Flush();
        long placed = 0, late = 0;
        while (sw.Elapsed.TotalSeconds < seconds + 0.25)
        {
            uint packet;
            cap.GetNextPacketSize(out packet);
            while (packet > 0)
            {
                IntPtr data; uint frames, flags; ulong dp, qp;
                cap.GetBuffer(out data, out frames, out flags, out dp, out qp);
                long pos = (long)Math.Round((qp * 1e-7 - t0) * RATE);  // qp: 100 ns units of the same counter
                if (qp == 0 || (flags & 0x4) != 0) pos = written;        // no time stamp: right after the last one
                if (pos < 0) pos = 0;
                long n = Math.Min((long)frames, totalFrames - pos);
                if (n > 0 && (flags & 0x2) == 0)                         // AUDCLNT_BUFFERFLAGS_SILENT: leave zeros
                    Marshal.Copy(data, pcm, (int)(pos * BLOCK), (int)(n * BLOCK));
                if (n > 0) { written = Math.Max(written, pos + n); placed++; }
                if (Math.Abs(pos - written + n) > RATE / 50) late++;
                cap.ReleaseBuffer(frames);
                cap.GetNextPacketSize(out packet);
            }
            Thread.Sleep(5);
        }
        client.Stop();
        Console.WriteLine("packets " + placed + " out_of_order " + late);
        using (FileStream fs = new FileStream(outPath, FileMode.Create))
        using (BinaryWriter w = new BinaryWriter(fs))
        {
            long dataBytes = totalFrames * BLOCK;
            w.Write(new char[] { 'R', 'I', 'F', 'F' }); w.Write((int)(36 + dataBytes));
            w.Write(new char[] { 'W', 'A', 'V', 'E', 'f', 'm', 't', ' ' }); w.Write(16);
            w.Write((short)1); w.Write((short)CH); w.Write(RATE); w.Write(RATE * BLOCK);
            w.Write((short)BLOCK); w.Write((short)BITS);
            w.Write(new char[] { 'd', 'a', 't', 'a' }); w.Write((int)dataBytes);
            w.Write(pcm, 0, (int)dataBytes);
        }
        return 0;
    }

    static int Mute(uint pid, bool mute)
    {
        IMMDeviceEnumerator en = (IMMDeviceEnumerator)new MMDeviceEnumeratorCom();
        IMMDevice dev;
        en.GetDefaultAudioEndpoint(0, 1, out dev); // eRender, eMultimedia
        Guid iid = typeof(IAudioSessionManager2).GUID;
        object o;
        dev.Activate(ref iid, 23, IntPtr.Zero, out o); // CLSCTX_ALL
        IAudioSessionManager2 mgr = (IAudioSessionManager2)o;
        IAudioSessionEnumerator se;
        mgr.GetSessionEnumerator(out se);
        int n; se.GetCount(out n);
        int changed = 0;
        for (int i = 0; i < n; i++)
        {
            object s; se.GetSession(i, out s);
            IAudioSessionControl2 c2 = (IAudioSessionControl2)s;
            uint spid; c2.GetProcessId(out spid);
            if (spid != pid) continue;
            ISimpleAudioVolume vol = (ISimpleAudioVolume)s;
            vol.SetMute(mute ? 1 : 0, IntPtr.Zero);
            changed++;
        }
        Console.WriteLine("sessions changed: " + changed);
        return changed > 0 ? 0 : 3;
    }
}
