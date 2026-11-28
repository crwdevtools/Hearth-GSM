// Stand-in for docker.exe when testing start.bat in CI: records each call, says "linux".
// It must be a real .exe: a .cmd called from start.bat without `call` never returns to it.
using System;
using System.IO;

class FakeDocker
{
    static int Main(string[] args)
    {
        var log = Environment.GetEnvironmentVariable("DOCKER_CALLS");
        if (!string.IsNullOrEmpty(log)) File.AppendAllText(log, string.Join(" ", args) + "\r\n");
        if (args.Length > 1 && args[0] == "info" && args[1] == "--format") Console.WriteLine("linux");
        return 0;
    }
}
