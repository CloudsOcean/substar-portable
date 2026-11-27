using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class SubstarLauncher
{
    private delegate bool ConsoleHandler(uint control);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleCtrlHandler(ConsoleHandler handler, bool add);
    [DllImport("user32.dll")]
    private static extern bool AllowSetForegroundWindow(int processId);

    private static int Main()
    {
        Console.OutputEncoding = Encoding.UTF8;
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "runtime", "python", "python.exe");
        string script = Path.Combine(root, "launcher.py");
        string data = Path.Combine(root, "data");
        Process exportHelper = null;
        ConsoleHandler close = control =>
        {
            if (control != 2)  // CTRL_CLOSE_EVENT
                return false;
            StopExportHelper(exportHelper);
            return true;
        };
        SetConsoleCtrlHandler(close, true);
        try
        {
            if (!File.Exists(python) || !File.Exists(script))
                throw new FileNotFoundException("便携包不完整，请解压整个 Substar 文件夹。");

            string bundledHelper = Path.Combine(root, "runtime", "windows-export", "SubstarWindowsExport.exe");
            string localHelper = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "SubstarWorkbench", "WindowsExport", "SubstarWindowsExport.exe");
            string helper = File.Exists(localHelper) ? localHelper : bundledHelper;
            if (File.Exists(helper))
            {
                try
                {
                    exportHelper = Process.Start(StartInfo(helper, Path.Combine(data, ".substar-workbench", "runtime"), root));
                }
                catch (System.ComponentModel.Win32Exception error)
                {
                    Console.Error.WriteLine("Windows 导出组件未启动：" + error.Message);
                }
            }

            var start = StartInfo(python, script, root);
            start.EnvironmentVariables["SUBSTAR_DATA_ROOT"] = data;
            start.EnvironmentVariables["SUBSTAR_EDITION"] = "slim";
            start.EnvironmentVariables["SUBSTAR_CONSOLE_OWNER_PID"] = Process.GetCurrentProcess().Id.ToString();
            using (var launcher = Process.Start(start))
            {
                AllowSetForegroundWindow(launcher.Id);
                launcher.WaitForExit();
                if (launcher.ExitCode != 0)
                    ShowFailure();
                return launcher.ExitCode;
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Substar 启动失败：" + error.Message);
            ShowFailure();
            return 1;
        }
        finally
        {
            SetConsoleCtrlHandler(close, false);
            StopExportHelper(exportHelper);
            if (exportHelper != null)
                exportHelper.Dispose();
        }
    }

    private static void ShowFailure()
    {
        if (Console.IsInputRedirected)
            return;
        Console.Error.WriteLine("按任意键关闭此窗口。");
        Console.ReadKey(true);
    }

    private static ProcessStartInfo StartInfo(string executable, string argument, string root)
    {
        return new ProcessStartInfo(executable, "\"" + argument + "\"") {
            WorkingDirectory = root,
            UseShellExecute = false,
        };
    }

    private static void StopExportHelper(Process helper)
    {
        if (helper == null)
            return;
        try
        {
            if (!helper.HasExited)
                helper.Kill();
        }
        catch (InvalidOperationException) { }
        catch (System.ComponentModel.Win32Exception) { }
    }
}
