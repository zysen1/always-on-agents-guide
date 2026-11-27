"""回归检查：两次进程接收同事件、并发去重、跨进程批准、导出和跳过。"""
import json
import http.client
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    program = [sys.executable, str(Path(__file__).with_name("radar.py")),
               "--db", str(Path(directory, "check.sqlite3"))]
    def run(*args):
        return json.loads(subprocess.check_output([*program, *args], text=True))
    first = run("sample")
    assert first == {"action": "生成待审草稿", "id": 1}
    with ThreadPoolExecutor(max_workers=4) as pool:
        repeats = list(pool.map(lambda _: run("sample"), range(4)))
    assert all(result == {"action": "重复已跳过", "id": 1} for result in repeats)
    assert run("status") == {"已收件": 1, "待审核": 1, "已收录": 0, "未命中": 0}
    assert run("approve", "1")["action"] == "人工确认收录"
    assert run("approve", "1")["action"] == "此前已收录"
    assert run("export", "--state", "pending") == []
    approved = run("export", "--state", "approved")
    assert len(approved) == 1 and approved[0]["approved_at"] and approved[0]["draft"]
    assert run("add", "--source", "检验样例", "--title", "天气预报", "--url",
               "https://example.org/weather#test")["action"] == "未命中关注词"
    assert run("status") == {"已收件": 2, "待审核": 0, "已收录": 1, "未命中": 1}
    feed = ("--source", "公开来源", "--title", "Release Candidate", "--url", "https://example.org/rc")
    assert run("add", *feed, "--keyword", "CLI", "--keyword", "candidate")["action"] == "生成待审草稿"
    assert run("add", *feed, "--all")["action"] == "重复已跳过"
    assert run("add", "--source", "公开来源", "--title", "1.0", "--url", "https://example.org/release",
               "--all")["action"] == "生成待审草稿"
    invalid = subprocess.run([*program, "live", "--repo", "https://example.org/project"], capture_output=True)
    assert invalid.returncode and "owner/repo" in invalid.stderr.decode()
    server = subprocess.Popen([*program, "serve", "--port", "0"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
    try:
        port = int(server.stdout.readline().split()[1].rsplit(":", 1)[1])
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        client.request("GET", "/state", headers={"Host": "example.org"})
        response = client.getresponse()
        assert response.status == 403
        response.read()
        client.request("GET", "/state")
        response = client.getresponse()
        assert response.status == 200
        assert len(json.load(response)["items"]) == 4
        client.close()
    finally:
        server.terminate()
        server.wait(timeout=5)
print("通过：跨进程恢复、并发去重、人工确认、筛选与导出、自选关键词、仓库名与本地访问限制。")
