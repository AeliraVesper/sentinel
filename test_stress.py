# -*- coding: utf-8 -*-
"""sentinel 压力/稳定性测试 — 长时间运行、反复拉起、内存压力模拟。

运行: python3 test_stress.py
注意: 用"服务自然死亡"模拟掉线,不用 pgrep/pkill 主动杀
(模式字符串会自匹配 Hermes wrapper,见 termux-setup 技能坑 #7/#12)。
"""

import os
import subprocess
import sys
import time
import random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sentinel

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


print("[1] 连续 30 次体检不崩(数据波动)")
for i in range(30):
    r = sentinel.make_report()
    t = sentinel.render_report(r)
    assert "哨兵体检" in t
check("30 次连续体检稳定", True)

print("[2] watch 长时间运行(10 秒)")
p = subprocess.Popen(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.py"), "watch"],
    stdout=subprocess.DEVNULL)
time.sleep(10)
alive = p.poll() is None
p.terminate()
check("watch 10 秒不崩", alive)

print("[3] guard 反复拉起(服务自然死亡 ×3,共 18 秒)")
gport = 24000 + random.randint(0, 20000)
code = (f"import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,"
        f"socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',{gport})); s.listen(128); time.sleep(8)")
env = dict(os.environ, SENTINEL_GUARD_INTERVAL="2")
g = subprocess.Popen(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.py"),
     "guard", f"svc:{gport}", sys.executable, "-c", code],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
time.sleep(18)
g.terminate()
out = g.stdout.read()
n_down = out.count("掉线了")
n_up = out.count("拉起成功")
check("至少 2 次掉线检测", n_down >= 2, f"掉线={n_down}")
check("至少 2 次拉起成功", n_up >= 2, f"拉起={n_up}")
check("掉线与拉起数量一致", n_down == n_up, f"掉线={n_down} 拉起={n_up}")

print("[4] 内存压力模拟(可用 500MB → 危险告警)")
r = sentinel.make_report()
r["mem"]["avail_mb"] = 500
r["overall"] = "crit"
t = sentinel.render_report(r)
check("可用500MB → 显示危险", "危险" in t)
check("可用500MB → 含告警说明", "可用" in t and "swap" in t)

print(f"\n压力测试: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
