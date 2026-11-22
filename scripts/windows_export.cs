using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class WindowsExport
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static readonly Regex Token = new Regex("^[a-f0-9]{32}$");
    private static readonly Regex ProjectId = new Regex("^[A-Za-z0-9_-]{1,128}$");
    private static readonly Regex DeviceName = new Regex(
        @"^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)", RegexOptions.IgnoreCase);
    private static readonly HashSet<string> Extensions = new HashSet<string>(
        new[] { ".mp4", ".mov", ".wav", ".mp3", ".m4a", ".flac", ".srt", ".txt", ".xml", ".zip" },
        StringComparer.OrdinalIgnoreCase);

    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Length != 1)
            return;
        string runtime = Path.GetFullPath(args[0]);
        string install = Path.GetFullPath(Path.Combine(runtime, "..", "..", ".."));
        string mutexName = "Local\\Substar.WindowsExport." + install.Replace('\\', '_');
        using (var mutex = new Mutex(false, mutexName))
        {
            bool acquired;
            try { acquired = mutex.WaitOne(0); }
            catch (AbandonedMutexException) { acquired = true; }
            if (!acquired)
                return;
            try { Watch(runtime, install); }
            finally { mutex.ReleaseMutex(); }
        }
    }

    private static void Watch(string runtime, string install)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(45);
        while (DateTime.UtcNow < deadline)
        {
            if (Directory.Exists(runtime))
            {
                foreach (string path in Directory.GetFiles(runtime, "*.json"))
                {
                    string origin;
                    Dictionary<string, object> ticket;
                    try
                    {
                        var config = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
                        if (!String.Equals(Text(config, "install_root"), install, StringComparison.OrdinalIgnoreCase))
                            continue;
                        string instance = Text(config, "instance_id");
                        int port = Convert.ToInt32(config["port"]);
                        if (port < 1024 || port > 65535 || !Token.IsMatch(instance ?? ""))
                            continue;
                        origin = "http://127.0.0.1:" + port;
                        var request = Request(origin + "/api/native-output/claim");
                        request.Headers["X-Substar-Instance-Id"] = instance;
                        request.Method = "POST";
                        request.ContentLength = 0;
                        ticket = Read(request);
                    }
                    catch (Exception)
                    {
                        continue;
                    }
                    string token = Text(ticket, "token");
                    if (Token.IsMatch(token ?? ""))
                        Export(origin, origin + "/api/native-output/" + token, ticket);
                    deadline = DateTime.UtcNow.AddSeconds(15);
                    break;
                }
            }
            Thread.Sleep(500);
        }
    }

    private static void Export(string origin, string ticket, Dictionary<string, object> request)
    {
        try
        {
            string title = Text(request, "title");
            string projectId = Text(request, "project_id");
            if (!ProjectId.IsMatch(projectId))
                throw new InvalidDataException("项目编号无效");
            string folder = ChooseParent(title);
            if (folder == null)
            {
                Update(ticket, "cancelled");
                return;
            }

            Update(ticket, "selected");
            var rendering = WaitFor(ticket, "selected", "rendering", TimeSpan.FromMinutes(2));
            string jobId = Text(rendering, "job_id");
            if (!Token.IsMatch(jobId))
                throw new InvalidDataException("导出任务编号无效");
            string job = origin + "/api/projects/" + projectId + "/output-jobs/" + jobId;
            var output = WaitFor(job, "queued,running", "ready", TimeSpan.FromHours(4.2));
            var files = (IList)output["files"];
            string saved = SaveFiles(folder, title, job, files);
            Update(ticket, "saved", "folder", saved);
        }
        catch (Exception error)
        {
            try
            {
                string message = error.Message;
                if (message.Length > 1000)
                    message = message.Substring(0, 1000);
                Update(ticket, "error", "message", message);
            }
            catch (Exception)
            {
                MessageBox.Show(error.Message, "Substar 导出", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }

    private static string ChooseParent(string title)
    {
        using (var owner = new Form { Text = "Substar 导出", TopMost = true,
                                      StartPosition = FormStartPosition.CenterScreen,
                                      ClientSize = new Size(340, 64),
                                      FormBorderStyle = FormBorderStyle.FixedDialog,
                                      MinimizeBox = false, MaximizeBox = false })
        using (var caption = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter,
                                        Text = "请选择导出位置" })
        using (var dialog = new FolderBrowserDialog())
        {
            dialog.Description = "选择导出位置。Substar 将创建子文件夹：" + title;
            dialog.ShowNewFolderButton = true;
            string downloads = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads");
            if (Directory.Exists(downloads))
                dialog.SelectedPath = downloads;
            owner.Controls.Add(caption);
            owner.Show();
            owner.Activate();
            return dialog.ShowDialog(owner) == DialogResult.OK ? dialog.SelectedPath : null;
        }
    }

    private static string SaveFiles(string parent, string title, string job, IList files)
    {
        if (files.Count < 2 || files.Count > 12)
            throw new InvalidDataException("导出的文件数量无效");
        string folderName = ValidName(title) ? title : "_" + title;
        if (!ValidName(folderName))
            throw new InvalidDataException("导出标题无法用作文件夹名称");

        string staging = Path.Combine(parent, ".substar-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(staging);
        bool committed = false;
        var stagedFiles = new List<string>();
        try
        {
            for (int index = 0; index < files.Count; index++)
            {
                var file = (Dictionary<string, object>)files[index];
                string name = Text(file, "name");
                if (!ValidName(name) || !Extensions.Contains(Path.GetExtension(name)))
                    throw new InvalidDataException("导出的文件名无效");
                long expected = Convert.ToInt64(file["bytes"]);
                using (var response = (HttpWebResponse)Request(job + "/files/" + index).GetResponse())
                using (var input = response.GetResponseStream())
                using (var output = new FileStream(Path.Combine(staging, name), FileMode.CreateNew))
                {
                    stagedFiles.Add(output.Name);
                    input.CopyTo(output);
                    if (output.Length != expected)
                        throw new IOException("导出的文件未完整写入：" + name);
                }
            }
            for (int number = 1; number <= 9999; number++)
            {
                string label = number == 1 ? folderName : folderName + " (" + number + ")";
                string target = Path.Combine(parent, label);
                if (Directory.Exists(target) || File.Exists(target))
                    continue;
                try
                {
                    Directory.Move(staging, target);
                    committed = true;
                    return target;
                }
                catch (IOException)
                {
                    if (!Directory.Exists(target) && !File.Exists(target))
                        throw;
                }
            }
            throw new IOException("没有可用的导出文件夹名称");
        }
        finally
        {
            if (!committed)
            {
                foreach (string file in stagedFiles)
                    File.Delete(file);
                Directory.Delete(staging);
            }
        }
    }

    private static bool ValidName(string name)
    {
        return name.Length > 0 && name.IndexOfAny(Path.GetInvalidFileNameChars()) < 0 &&
               !name.EndsWith(".") && !name.EndsWith(" ") && !DeviceName.IsMatch(name);
    }

    private static Dictionary<string, object> WaitFor(string url, string waiting, string wanted, TimeSpan timeout)
    {
        DateTime until = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < until)
        {
            var result = Read(Request(url));
            string state = Text(result, "state");
            if (state == wanted)
                return result;
            if (Array.IndexOf(waiting.Split(','), state) == -1)
            {
                string message = Text(result, "message");
                throw new IOException(String.IsNullOrEmpty(message) ? "导出任务已结束" : message);
            }
            Thread.Sleep(750);
        }
        throw new TimeoutException("等待导出超时");
    }

    private static string Text(Dictionary<string, object> data, string name)
    {
        object result;
        return data.TryGetValue(name, out result) ? result as string : null;
    }

    private static HttpWebRequest Request(string url)
    {
        var request = (HttpWebRequest)WebRequest.Create(url);
        request.AllowAutoRedirect = false;
        request.Proxy = null;
        request.Timeout = 15000;
        request.ReadWriteTimeout = 120000;
        return request;
    }

    private static Dictionary<string, object> Update(string url, string action, string key = null, string value = null)
    {
        var values = new Dictionary<string, string> { { "action", action } };
        if (key != null)
            values.Add(key, value);
        byte[] body = Encoding.UTF8.GetBytes(Json.Serialize(values));
        var request = Request(url);
        request.Method = "POST";
        request.ContentType = "application/json";
        request.ContentLength = body.Length;
        using (var stream = request.GetRequestStream())
            stream.Write(body, 0, body.Length);
        return Read(request);
    }

    private static Dictionary<string, object> Read(HttpWebRequest request)
    {
        using (var response = (HttpWebResponse)request.GetResponse())
        using (var stream = response.GetResponseStream())
        using (var reader = new StreamReader(stream, Encoding.UTF8))
            return Json.Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
    }
}
