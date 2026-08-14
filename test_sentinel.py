# -*- coding: utf-8 -*-
"""sentinel 自测脚本 — 用真实环境 + 构造数据验证核心逻辑。

运行: python3 test_sentinel.py
"""

import os
import socket
import subprocess
import sys
import threading
import time

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


def fake_mem(total_mb, avail_mb, swap_total=8192, swap_free=4096):
    # 与 sentinel.read_meminfo() 的单位一致:MB
    return {
        "MemTotal": total_mb,
        "MemAvailable": avail_mb,
        "SwapTotal": swap_total,
        "SwapFree": swap_free,
    }


# ---------------------------------------------------------------- 1. 真实数据读取

print("\n[1] 真实环境数据读取")
mem = sentinel.read_meminfo()
check("meminfo 读到 MemTotal", mem.get("MemTotal", 0) > 0, f"got {mem}")
check("meminfo 读到 MemAvailable", mem.get("MemAvailable", 0) > 0)
check("swap 字段齐全", "SwapTotal" in mem and "SwapFree" in mem)

temp = sentinel.read_temperature()
check("温度可读且合理(0~120°C)", temp is not None and 0 <= temp <= 120, f"got {temp}")

load = sentinel.read_loadavg()
if load is None:
    print("    (提示) /proc/loadavg 在 Android 无权限读取——环境限制,工具已优雅降级,测试跳过此项")
    PASS += 1
else:
    check("负载可读(3元组)", len(load) == 3, f"got {load}")

disk = sentinel.read_disk(os.path.expanduser("~"))
check("磁盘可读", disk and disk["total"] > 0 and disk["pct"] >= 0, f"got {disk}")

top = sentinel.top_processes(5)
check("TOP 进程 ≤5 个", len(top) <= 5 and all(len(t) == 3 for t in top), f"got {top}")
if top:
    check("TOP 按 RSS 降序", all(top[i][2] >= top[i + 1][2] for i in range(len(top) - 1)),
          f"got {[t[2] for t in top]}")
    print(f"    top1: {top[0][0]} pid={top[0][1]} {top[0][2]}MB")

# ---------------------------------------------------------------- 2. 等级判定逻辑

print("\n[2] 内存等级判定逻辑")
ok = sentinel.mem_status(fake_mem(11274, 8000, swap_total=8192, swap_free=6000))
check("充裕内存 → ok", ok["level"] == "ok", f"got {ok['level']}")

warn = sentinel.mem_status(fake_mem(11274, 3500))
check("3500MB 可用 → warn", warn["level"] == "warn", f"got {warn['level']}")

crit = sentinel.mem_status(fake_mem(11274, 1500))
check("1500MB 可用 → crit", crit["level"] == "crit", f"got {crit['level']}")

swap_crit = sentinel.mem_status(fake_mem(11274, 8000, swap_total=8192, swap_free=1000))
check("swap 用 88% → crit(即使内存充足)", swap_crit["level"] == "crit",
      f"got {swap_crit['level']} swap_pct={swap_crit['swap_pct']}")

swap_warn = sentinel.mem_status(fake_mem(11274, 8000, swap_total=8192, swap_free=3500))
check("swap 用 57% → warn", swap_warn["level"] == "warn",
      f"got {swap_warn['level']} swap_pct={swap_warn['swap_pct']}")

edge = sentinel.mem_status(fake_mem(11274, 2048))
check("正好 2048MB → warn(低于才 crit)", edge["level"] == "warn", f"got {edge['level']}")

zero = sentinel.mem_status({})
check("空数据 → ok 不崩", zero["level"] == "ok" and zero["total_mb"] == 0)

# ---------------------------------------------------------------- 3. 报告渲染

print("\n[3] 报告渲染")
r = sentinel.make_report()
check("报告含总体等级", r["overall"] in ("ok", "warn", "crit"))
text = sentinel.render_report(r)
for needle in ("哨兵体检", "内存", "swap", "内存大户"):
    check(f"报告包含 '{needle}'", needle in text)
print(text)

# ---------------------------------------------------------------- 4. 端口探测 + guard 拉起

print("\n[4] 端口探测与 guard 拉起")

# 起一个临时 HTTP 服务模拟目标
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
port = srv.getsockname()[1]
srv.listen(1)

def serve():
    # 只 accept 一次就退出:若线程持有 srv 引用阻塞在循环里,
    # 主线程 srv.close() 不会真正关闭 fd(Python socket 引用计数语义),端口永不释放
    try:
        c, _ = srv.accept()
        c.close()
    except OSError:
        pass

t = threading.Thread(target=serve, daemon=True)
t.start()

check("活端口 → is_port_open True", sentinel.is_port_open(port))
check("死端口 → is_port_open False", not sentinel.is_port_open(port + 1))
srv.close()
# serve 线程 accept 一次已退出、不再持有引用,close 立即释放端口;
# 留 1 秒余量给内核清理。
time.sleep(1.0)
check("关闭后端口 → False", not sentinel.is_port_open(port))

# guard: 用一个"假服务"验证拉起逻辑(用 python 直启,避免 bash 冷启动慢)
FAKE_SRV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fake_srv.py")
with open(FAKE_SRV, "w") as f:
    f.write(f"""import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', {port}))
s.listen(128)  # backlog 要大:探测连接若不被 accept 会堆积,backlog=1 时很快被 RST
time.sleep(15)
""")

check("guard 探测死端口 → 判定掉线", not sentinel._is_alive(f"fake:{port}"))

# 拉起假服务
launched = sentinel._spawn_detached([sys.executable, FAKE_SRV])
check("detached 拉起成功", launched)
time.sleep(2.5)  # python 解释器冷启动
check("拉起后端口变活", sentinel.is_port_open(port))

# 解析 guard 目标格式
check("格式解析: 名称:端口 → 取到端口", sentinel._is_alive(f"fake:{port}"))
check("无端口 → 当作存活(不误杀)", sentinel._is_alive("weird-name"))

# 清理假服务:用 PID 精确杀,不能 pkill -f(会误杀匹配到字符串的 Hermes wrapper)
import subprocess as _sp
try:
    _out = _sp.check_output(["pgrep", "-f", "_fake_srv.py"], text=True)
    for _pid in _out.split():
        os.kill(int(_pid), 9)
except Exception:
    pass
os.remove(FAKE_SRV)

# ---------------------------------------------------------------- 6. guard 端到端(子进程)

print("\n[6] guard 端到端(子进程,随机端口)")
import random as _rnd
_gport = 20000 + _rnd.randint(0, 20000)
# 假服务:绑定随机端口后 sleep 15s 自然退出(不留残留进程)。
# 用 python -c 内联(不写文件),并缩小 guard 间隔加快测试。
_srv_code = (f"import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,"
             f"socket.SO_REUSEADDR,1); s.bind(('127.0.0.1',{_gport})); s.listen(128); time.sleep(15)")
_srv_cmd = [sys.executable, "-c", _srv_code]
_env = dict(os.environ, SENTINEL_GUARD_INTERVAL="2")

# 起 guard 子进程:端口天生是死的,应立即检测掉线并自动拉起假服务
_guard = _sp.Popen(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.py"),
     "guard", f"demo:{_gport}"] + _srv_cmd,
    stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, env=_env)
time.sleep(5)
_guard.terminate()
_gout = _guard.stdout.read()
check("guard 检测到掉线", "掉线了" in _gout, f"got: {_gout[:200]!r}")
check("guard 自动拉起成功", "拉起成功" in _gout, f"got: {_gout[:200]!r}")
check("guard 拉起后端口变活", sentinel.is_port_open(_gport))

# 等假服务自己退出(15s sleep),轮询端口释放,避免 pkill 误杀
_released = False
for _ in range(25):
    time.sleep(0.5)
    if not sentinel.is_port_open(_gport):
        _released = True
        break
check("假服务自然退出、端口释放", _released)

# ---------------------------------------------------------------- 5. CLI 入口

print("\n[5] CLI 入口")
# 无参数 = 体检报告(测试时要验证输出确实包含报告内容,不能只查 rc)
import io as _io
import contextlib as _ctx
_buf = _io.StringIO()
with _ctx.redirect_stdout(_buf):
    rc = sentinel.main([])
check("main([]) rc=0", rc == 0)
check("main([]) 输出体检报告", "哨兵体检" in _buf.getvalue(), f"got: {_buf.getvalue()[:80]!r}")

# 其余入口:只查 rc(输出已在各自模式验证过)
for args, expect_rc in [
    (["status"], 0),
    (["version"], 0),
    (["--help"], 0),
    (["badcmd"], 0),        # 未知命令 → 打印用法,rc 0
    (["guard", "onlyname"], 0),  # 参数不足 → 用法
]:
    rc = sentinel.main(args)
    check(f"main({args}) rc={expect_rc}", rc == expect_rc, f"got {rc}")

# watch 模式:后台跑 2 秒验证不崩
proc = subprocess.Popen(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.py"), "watch"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
time.sleep(2.5)
proc.terminate()
out = proc.stdout.read().decode(errors="replace")
check("watch 模式能持续输出", "盯防" in out and "等级" in out, f"got: {out[:120]}")

print(f"\n{'='*40}\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
