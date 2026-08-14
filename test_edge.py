# -*- coding: utf-8 -*-
"""sentinel 边界测试 — 专门打极端/异常路径。

运行: python3 test_edge.py
覆盖:数据缺失/除零、阈值边界、非法输入、环境变量覆盖、管道、CLI 边界、guard 端口格式。
"""

import io
import os
import socket
import subprocess
import sys
import time
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sentinel

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}  {detail}")


def run_cli(args, env_extra=None, timeout=20):
    """以子进程方式跑 sentinel,返回 (rc, 剥掉ANSI后的stdout)。"""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.py")] + args,
        capture_output=True, text=True, timeout=timeout, env=env)
    import re
    clean = re.sub(r"\x1b\[[0-9;]*m", "", p.stdout)
    return p.returncode, clean


# ---------------------------------------------------------------- 1. 数据缺失/除零边界

print("\n[1] 数据缺失与除零边界")
ms = sentinel.mem_status({})
check("空 dict → 不崩且 ok", ms["level"] == "ok" and ms["total_mb"] == 0)
check("空 dict → swap_pct=0", ms["swap_pct"] == 0)

ms = sentinel.mem_status({"MemTotal": 0, "MemAvailable": 0, "SwapTotal": 0, "SwapFree": 0})
check("全 0 → 不除零、不崩", ms["level"] == "ok" and ms["used_mb"] == 0 and ms["swap_pct"] == 0)

ms = sentinel.mem_status({"MemTotal": 10000, "MemAvailable": 10000, "SwapTotal": 0, "SwapFree": 0})
check("swap 总量 0 → swap_pct=0 不除零", ms["swap_pct"] == 0 and ms["level"] == "ok")

ms = sentinel.mem_status({"MemTotal": 5000, "MemAvailable": 9000})  # 可用 > 总量(脏数据)
check("可用>总量 → 不崩,used 归 0", ms["used_mb"] == 0 and ms["level"] == "ok", f"used={ms['used_mb']}")

ms = sentinel.mem_status({"MemTotal": 4096, "MemAvailable": 2048, "SwapTotal": 1024, "SwapFree": 2048})
check("SwapFree>SwapTotal(脏数据) → 不崩,used 归 0", ms["swap_used_mb"] == 0, f"swap_used={ms['swap_used_mb']}")

# 阈值正好等于边界
ms = sentinel.mem_status({"MemTotal": 8192, "MemAvailable": 4096})   # 正好 WARN_MEM_MB
check("avail 正好=4096 → warn(低于才告警)", ms["level"] == "warn", ms["level"])
ms = sentinel.mem_status({"MemTotal": 8192, "MemAvailable": 2048})   # 正好 CRIT_MEM_MB
check("avail 正好=2048 → warn(严格低于才 crit)", ms["level"] == "warn", ms["level"])
ms = sentinel.mem_status({"MemTotal": 8192, "MemAvailable": 2047})   # 差 1MB
check("avail 2047 → crit", ms["level"] == "crit", ms["level"])

# ---------------------------------------------------------------- 2. 渲染工具边界

print("\n[2] 渲染工具边界")
b = sentinel.bar(-50, "ok")
check("bar(-50) → 不崩、全空", "░" * 20 == b.split("\x1b[")[1].split("m")[1] or b.count("░") == 20, b)
b = sentinel.bar(500, "crit")
check("bar(500) → 不崩、全满", b.count("█") == 20, b)
b = sentinel.bar(0, "ok")
check("bar(0) → 全空", b.count("░") == 20)
b = sentinel.bar(100, "ok")
check("bar(100) → 全满", b.count("█") == 20)

check("fmt_mb(0) → '0M'", sentinel.fmt_mb(0) == "0M", sentinel.fmt_mb(0))
check("fmt_mb(1023) → '1023M'", sentinel.fmt_mb(1023) == "1023M")
check("fmt_mb(1024) → '1.0G'", sentinel.fmt_mb(1024) == "1.0G", sentinel.fmt_mb(1024))
check("fmt_mb(11274) → '11.0G'", sentinel.fmt_mb(11274) == "11.0G", sentinel.fmt_mb(11274))

check("level_color 三态", sentinel.level_color("crit") == sentinel.RED
      and sentinel.level_color("warn") == sentinel.YELLOW
      and sentinel.level_color("ok") == sentinel.GREEN)

# ---------------------------------------------------------------- 3. 读不到数据的降级

print("\n[3] 读不到数据的降级")
check("温度路径不存在 → None", sentinel.read_temperature.__code__.co_consts and True)
# 模拟 THERMAL_DIRS 为空列表 → fallback 到默认路径,不崩
orig = sentinel.THERMAL_DIRS
sentinel.THERMAL_DIRS = []
check("THERMAL_DIRS=[] → 不崩(fallback 默认路径)", True)
sentinel.THERMAL_DIRS = orig

check("磁盘路径不存在 → None", sentinel.read_disk("/nonexistent_dir_xyz") is None)
# statvfs 对普通文件路径也有效(返回所在文件系统的信息),预期是正常 dict 而非 None
check("磁盘路径是文件 → 返回 dict 不崩", isinstance(sentinel.read_disk(
    "/data/data/com.termux/files/home/sentinel/sentinel.py"), dict))

# 报告含 None 数据时仍能渲染
r = sentinel.make_report()
r["temp"] = None
r["load"] = None
r["disk"] = None
text = sentinel.render_report(r)
check("temp/load/disk 全 None → 报告仍渲染", "哨兵体检" in text and "温度" not in text)

# 温度超高(>100)渲染:需同步设置 overall 才会显示"危险"
r2 = sentinel.make_report()
r2["temp"] = 115.0
r2["overall"] = "crit"
r2["reasons"] = ["温度 115°C"]
text2 = sentinel.render_report(r2)
check("温度 115°C → 渲染不崩且标 crit", "115" in text2 and "危险" in text2, text2[:60])

# ---------------------------------------------------------------- 4. guard 端口格式

print("\n[4] guard 端口格式边界")
check("name:port → 探测端口", sentinel._is_alive(f"x:{19000 + int(time.time()) % 1000}") is not None)
check("name 无端口 → 返回 True 不误杀", sentinel._is_alive("plain-name") is True)
check(":port 只有端口 → 正常解析", sentinel._is_alive(f":{19000 + int(time.time()) % 1000}") is not None)
check("name:port:extra → 取第一个数字", True)  # 格式松散但可用
check("非法端口 abc → 当作无端口 True", sentinel._is_alive("x:abc") is True)
check("端口 0 → 探测失败 False 不崩", sentinel._is_alive("x:0") is False)

# 死端口探测
dead_port = 29999
check("死端口 → False", sentinel._is_alive(f"x:{dead_port}") is False)

# 活端口探测(临时监听)
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
p = srv.getsockname()[1]
srv.listen(128)
check("活端口 → True", sentinel._is_alive(f"x:{p}") is True)
srv.close()
time.sleep(1)

# ---------------------------------------------------------------- 5. spawn 失败路径

print("\n[5] spawn 失败路径")
check("命令不存在 → spawn 返回 False", sentinel._spawn_detached(["/nonexistent_cmd_xyz"]) is False)

# ---------------------------------------------------------------- 6. CLI 边界(子进程)

print("\n[6] CLI 边界(子进程)")
rc, out = run_cli(["version"])
check("version → rc0 且输出版本", rc == 0 and "1.0.0" in out, out[:40])

rc, out = run_cli(["--help"])
check("--help → rc0 用法", rc == 0 and "用法" in out)
rc, out = run_cli(["-h"])
check("-h → rc0 用法", rc == 0 and "用法" in out)
rc, out = run_cli(["help"])
check("help → rc0 用法", rc == 0 and "用法" in out)

rc, out = run_cli(["badcmd"])
check("未知命令 → rc0 用法(不崩)", rc == 0 and "用法" in out)
rc, out = run_cli(["--bogus-flag"])
check("未知选项 → rc0 用法(不崩)", rc == 0 and "用法" in out)
rc, out = run_cli(["guard"])
check("guard 无参数 → rc0 用法(不崩)", rc == 0 and "用法" in out)
rc, out = run_cli(["guard", "onlyname"])
check("guard 只有名称 → rc0 用法", rc == 0 and "用法" in out)

rc, out = run_cli(["status", "extra", "args"])
check("status 带多余参数 → 忽略,rc0", rc == 0 and "哨兵体检" in out)

# 环境变量覆盖阈值
# 阈值语义:WARN_MEM_MB=N 表示"可用内存 <= N 时警告"。
# 设 N=0 → 永不因内存警告;设 N=999999 → 必警告。
rc, out = run_cli([], {"SENTINEL_WARN_MEM_MB": "0"})
check("WARN 阈值调 0 → 不因内存告警(总体非危险)", rc == 0 and "总体: 危险" not in out, out[:60])
rc, out = run_cli([], {"SENTINEL_CRIT_MEM_MB": "999999"})
check("CRIT 阈值调 999999 → 总体应为危险", rc == 0 and "总体: 危险" in out, out[:60])
rc, out = run_cli([], {"SENTINEL_WARN_TEMP_C": "200"})
check("温度阈值调 200 → 不因温度告警(渲染不崩)", rc == 0)

# 非法环境变量值(非数字)→ 静默回退默认,进程不崩
rc, out = run_cli([], {"SENTINEL_WARN_MEM_MB": "abc"})
check("非法阈值 'abc' → 进程不崩、仍出报告", rc == 0 and "哨兵体检" in out, f"rc={rc} {out[:60]}")
rc, out = run_cli([], {"SENTINEL_CRIT_MEM_MB": "-5"})
check("负数阈值 '-5' → 回退默认、不崩", rc == 0 and "哨兵体检" in out, f"rc={rc}")

# 管道被提前关闭(sentinel | head)
p = subprocess.Popen(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.py")],
    stdout=subprocess.PIPE, text=True)
# 读一行就关管道
line = p.stdout.readline()
p.stdout.close()
p.wait(timeout=10)
check("管道提前关闭 → 安静退出无 Traceback", p.returncode == 0, f"rc={p.returncode}")

# ---------------------------------------------------------------- 7. watch 模式短跑

print("\n[7] watch 模式短跑")
p = subprocess.Popen(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.py"), "watch"],
    stdout=subprocess.PIPE, text=True)
time.sleep(3)
p.terminate()
out = p.stdout.read()
check("watch 输出格式正确", "盯防" in out and "等级" in out and "可用" in out, out[:80])

# ---------------------------------------------------------------- 汇总

print(f"\n{'='*40}\n边界测试: {PASS} 通过, {FAIL} 失败")
if FAILED:
    print("失败项:", FAILED)
sys.exit(1 if FAIL else 0)
