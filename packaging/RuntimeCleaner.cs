using System;
using System.IO;
using System.Text;
using System.Linq;
using System.Collections.Generic;
using System.Web.Script.Serialization;

public class RuntimeLayout { public string[] files; public string[] directories; }
public static class RuntimeCleaner {
    static string Root;
    static HashSet<string> Files, Directories;
    static string Full(string path) { return Path.GetFullPath(path).TrimEnd('\\'); }
    static string Relative(string path) {
        string full = Full(path);
        if (!full.StartsWith(Root + "\\", StringComparison.OrdinalIgnoreCase))
            throw new IOException("Path is outside the S2S program folder");
        return full.Substring(Root.Length + 1);
    }
    static HashSet<string> Names(string[] names) {
        if (names == null) throw new IOException("Missing runtime layout");
        var result = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (string raw in names) {
            if (String.IsNullOrWhiteSpace(raw) || Path.IsPathRooted(raw))
                throw new IOException("Invalid runtime layout path");
            string name = raw.Replace('/', '\\');
            if (name.Split('\\').Any(p => p == "" || p == "." || p == ".." ||
                p.EndsWith(".") || p.EndsWith(" ") || p.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0))
                throw new IOException("Invalid runtime layout path");
            if (!result.Add(name)) throw new IOException("Duplicate runtime layout path");
        }
        return result;
    }
    static FileAttributes SafeAttributes(string path) {
        string full = Full(path);
        if (!String.Equals(full, Root, StringComparison.OrdinalIgnoreCase)) Relative(full);
        string at = full;
        while (true) {
            var attributes = File.GetAttributes(at);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
                throw new IOException("A linked file or folder prevents safe cleanup: " + at);
            if (String.Equals(at, Root, StringComparison.OrdinalIgnoreCase)) break;
            at = Path.GetDirectoryName(at);
        }
        return File.GetAttributes(full);
    }
    static void Walk(string directory, List<string> files, List<string> dirs) {
        SafeAttributes(directory);
        foreach (string path in Directory.GetFileSystemEntries(directory)) {
            if ((SafeAttributes(path) & FileAttributes.Directory) != 0) {
                Walk(path, files, dirs);
                dirs.Add(path); // children before parents, no recursive deletion
            } else files.Add(path);
        }
    }
    public static int Main(string[] args) {
        if (args.Length != 5) return 1;
        try {
            if (args[0] != "--check" && args[0] != "--clean") throw new IOException("Invalid cleanup mode");
            Root = Full(args[1]);
            if (!String.Equals(Root, Full(args[2]), StringComparison.OrdinalIgnoreCase) ||
                Path.GetDirectoryName(Root) == null || !String.Equals(Path.GetFileName(Root), "S2S", StringComparison.OrdinalIgnoreCase))
                throw new IOException("Install S2S into its standard program folder before cleaning up");
            var parser = new JavaScriptSerializer(); parser.MaxJsonLength = 32 * 1024 * 1024;
            var layout = parser.Deserialize<RuntimeLayout>(File.ReadAllText(args[3], Encoding.UTF8));
            Files = Names(layout.files); Directories = Names(layout.directories);
            if (!Files.Contains("S2S.exe") || Files.Overlaps(Directories)) throw new IOException("Invalid runtime layout");
            foreach (string name in Files.Concat(Directories)) {
                string parent = Path.GetDirectoryName(name);
                if (!String.IsNullOrEmpty(parent) && !Directories.Contains(parent))
                    throw new IOException("Runtime layout omits a parent directory");
            }
            var files = new List<string>(); var dirs = new List<string>();
            bool exists = true;
            try { SafeAttributes(Root); }
            catch (FileNotFoundException) { exists = false; }
            catch (DirectoryNotFoundException) { exists = false; }
            if (exists) Walk(Root, files, dirs);
            else if (args[0] == "--clean") throw new IOException("S2S program folder is unavailable");
            if (files.Concat(dirs).Select(Relative).Distinct(StringComparer.OrdinalIgnoreCase).Count() != files.Count + dirs.Count)
                throw new IOException("Ambiguous case-only program paths prevent safe cleanup");
            if (args[0] == "--clean") {
                // Prove the new runtime is present before removing anything from an older install.
                foreach (string name in Files) {
                    string path = Path.Combine(Root, name);
                    if ((SafeAttributes(path) & FileAttributes.Directory) != 0) throw new IOException("A required program file is missing");
                }
                foreach (string name in Directories)
                    if ((SafeAttributes(Path.Combine(Root, name)) & FileAttributes.Directory) == 0)
                        throw new IOException("A required program directory is missing");
                int removed = 0;
                foreach (string path in files) if (!Files.Contains(Relative(path))) {
                    var attributes = SafeAttributes(path);
                    File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
                    File.Delete(path); removed++;
                }
                foreach (string path in dirs) if (!Directories.Contains(Relative(path))) {
                    var attributes = SafeAttributes(path);
                    File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
                    Directory.Delete(path, false); removed++;
                }
                files.Clear(); dirs.Clear(); Walk(Root, files, dirs);
                if (files.Any(p => !Files.Contains(Relative(p))) || dirs.Any(p => !Directories.Contains(Relative(p))))
                    throw new IOException("Obsolete program files remain; close S2S and run setup again");
                Console.WriteLine("Removed obsolete program entries: " + removed);
            }
            File.WriteAllText(args[4], "", Encoding.UTF8); return 0;
        } catch (Exception error) {
            string message = "S2S cleanup could not finish. Close any remaining S2S processes and run setup again.\r\n" + error.Message;
            Console.Error.WriteLine(message);
            try { File.WriteAllText(args[4], message, Encoding.UTF8); } catch {}
            return 1;
        }
    }
}
