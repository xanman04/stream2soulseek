using System;
using System.IO;
using System.Text;
using System.Diagnostics;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Web.Script.Serialization;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public class Entry { public string path; public long size; public string sha256; }
public static class RuntimeComparer {
    [StructLayout(LayoutKind.Sequential)]
    struct FileInformation {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle file, out FileInformation info);
    public static int Main(string[] args) {
        if (args.Length != 3) return 1;
        string partial = args[2] + ".part";
        try {
            string root = Path.GetFullPath(args[0]).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            var parser = new JavaScriptSerializer(); parser.MaxJsonLength = 32 * 1024 * 1024;
            Entry[] entries = parser.Deserialize<Entry[]>(File.ReadAllText(args[1], Encoding.UTF8));
            var keep = new List<string>(); var clock = Stopwatch.StartNew(); long last = -1000;
            using (var hash = new SHA256Cng()) {
                for (int i = 0; i < entries.Length; i++) {
                    Entry entry = entries[i];
                    string path = Path.GetFullPath(Path.Combine(root, entry.path));
                    if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) return 1;
                    try {
                        using (var stream = File.OpenRead(path)) {
                            if (stream.Length == entry.size) {
                                string digest = BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
                                if (digest == entry.sha256) {
                                    FileInformation info;
                                    if (GetFileInformationByHandle(stream.SafeFileHandle, out info))
                                        keep.Add(i + "|" + info.SizeHigh + "|" + info.SizeLow + "|" +
                                            (uint)info.Write.dwHighDateTime + "|" + (uint)info.Write.dwLowDateTime);
                                }
                            }
                        }
                    } catch (IOException) {} catch (UnauthorizedAccessException) {}
                    if (clock.ElapsedMilliseconds - last >= 100 || i == entries.Length - 1) {
                        Console.WriteLine("PROGRESS " + (i + 1)); last = clock.ElapsedMilliseconds;
                    }
                }
            }
            File.WriteAllLines(partial, keep, Encoding.ASCII);
            if (File.Exists(args[2])) File.Delete(args[2]);
            File.Move(partial,args[2]); return 0;
        } catch { try { File.Delete(partial); } catch {} return 1; }
    }
}
