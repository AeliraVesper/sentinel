#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""sentinel — Termux/Android 内存哨兵

零第三方依赖(纯 Python 标准库),用于 Android 后台进程容易被
系统内存回收(LMKD)杀掉的场景。三个模式:
  sentinel          查看一次体检报告
  sentinel watch    持续盯防,内存/温度危险时高亮告警
  sentinel guard    守护命令:挂了自动拉起,危险时退出自保

所有数值读取均带异常兜底,任何一项读不到都不影响整体运行。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

VERSION = "1.0.0"

# ---------------------------------------------------------------- 常量

RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
CYAN = "\033[36m"
BOLD = "\033[1m"
RESET = "\033[0m"

# 危险阈值(可被环境变量覆盖)
WARN_MEM_MB = int(os.environ.get("SENTINEL_WARN_MEM_MB", "4096"))    # 可用内存低于此 → 黄
CRIT_MEM_MB = int(os.environ.get("SENTINEL_CRIT_MEM_MB", "2048"))    # 可用内存低于此 → 红
WARN_SWAP_PCT = int(os.environ.get("SENTINEL_WARN_SWAP_PCT", "50"))  # swap 使用率高于此 → 黄
CRIT_SWAP_PCT = int(os.environ.get("SENTINEL_CRIT_SWAP_PCT", "80"))  # swap 使用率高于此 → 红
WARN_TEMP_C = int(os.environ.get("SENTINEL_WARN_TEMP_C", "55"))      # 温度高于此 → 黄
CRIT_TEMP_C = int(os.environ.get("SENTINEL_CRIT_TEMP_C", "70"))      # 温度高于此 → 红
GUARD_INTERVAL = float(os.environ.get("SENTINEL_GUARD_INTERVAL", "10"))
WATCH_INTERVAL = float(os.environ.get("SENTINEL_WATCH_INTERVAL", "5"))

# 阈值可被环境变量覆盖(见文件头部注释)
THERMAL_DIRS = [
    "/sys/class/thermal",
]


# ---------------------------------------------------------------- 数据读取

def read_meminfo() -> dict:
    """读 /proc/meminfo,返回 {字段: MB}。任何字段缺失都给 0。"""
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    out[parts[0][:-1]] = int(parts[1]) // 1024  # kB → MB
    except OSError:
        pass
    return out


def mem_status(mem: dict) -> dict:
    """计算内存水位指标与危险等级。"""
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    swap_pct = int(swap_used * 100 / swap_total) if swap_total else 0

    level = "ok"
    reasons = []
    if avail and avail < CRIT_MEM_MB:
        level = "crit"
        reasons.append(f"可用内存仅 {avail}MB(<{CRIT_MEM_MB}MB)")
    elif avail and avail < WARN_MEM_MB:
        level = "warn"
        reasons.append(f"可用内存 {avail}MB(<{WARN_MEM_MB}MB)")
    if swap_pct > CRIT_SWAP_PCT:
        level = "crit"
        reasons.append(f"swap 已用 {swap_pct}%")
    elif swap_pct > WARN_SWAP_PCT:
        if level == "ok":
            level = "warn"
        reasons.append(f"swap 已用 {swap_pct}%")

    return {
        "total_mb": total,
        "avail_mb": avail,
        "used_mb": max(0, total - avail) if total else 0,
        "swap_total_mb": swap_total,
        "swap_used_mb": swap_used,
        "swap_pct": swap_pct,
        "level": level,
        "reasons": reasons,
    }


def read_temperature() -> float | None:
    """读第一个可用的 thermal zone 温度,返回摄氏温度或 None。"""
    zones = ["/sys/class/thermal/thermal_zone0/temp"]
    try:
        zones = [os.path.join(THERMAL_DIRS[0], d, "temp")
                 for d in sorted(os.listdir(THERMAL_DIRS[0]))
                 if d.startswith("thermal_zone")]
    except OSError:
        pass
    for z in zones:
        try:
            with open(z) as f:
                raw = int(f.read().strip())
            return raw / 1000.0 if raw > 1000 else float(raw)
        except (OSError, ValueError):
            continue
    return None


def read_loadavg() -> tuple:
    """读 /proc/loadavg,返回 (1,5,15分钟负载) 或 None。"""
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return tuple(float(x) for x in parts[:3])
    except (OSError, ValueError):
        return None


def read_disk(path: str) -> dict | None:
    """读磁盘使用情况,返回 {total, used, free, pct} GB 或 None。"""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = (st.f_blocks - st.f_bavail) * st.f_frsize
    gb = 1024 ** 3
    return {
        "total": total / gb,
        "used": used / gb,
        "free": free / gb,
        "pct": int(used * 100 / total) if total else 0,
    }


def top_processes(n: int = 5) -> list:
    """按 RSS 列出内存占用最大的 n 个进程: [(name, pid, rss_mb)]。"""
    procs = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm") as f:
                    name = f.read().strip()[:24]
                with open(f"/proc/{pid}/status") as f:
                    rss_kb = 0
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_kb = int(line.split()[1])
                            break
                if rss_kb > 0:
                    procs.append((name, int(pid), rss_kb // 1024))
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    procs.sort(key=lambda x: x[2], reverse=True)
    return procs[:n]


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """检查 TCP 端口是否可连。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def read_saved_states() -> dict:
    """读上次持久化的服务状态(仅当文件存在)。"""
    path = os.environ.get("SENTINEL_STATE", "")
    if not path:
        return {}
    states = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                states[k.strip()] = v.strip()
    except OSError:
        pass
    return states


def write_saved_states(states: dict) -> None:
    path = os.environ.get("SENTINEL_STATE", "")
    if not path:
        return
    try:
        with open(path, "w") as f:
            for k, v in states.items():
                f.write(f"{k}={v}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 输出

def level_color(level: str) -> str:
    return {"crit": RED, "warn": YELLOW, "ok": GREEN}.get(level, RESET)


def bar(value_pct: int, level: str, width: int = 20) -> str:
    """画一个颜色条。"""
    filled = max(0, min(width, int(value_pct * width / 100)))
    c = level_color(level)
    return f"{c}{'█' * filled}{'░' * (width - filled)}{RESET}"


def fmt_mb(mb: int) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    return f"{mb}M"


def health_line(name: str, value: str, level: str) -> str:
    c = level_color(level)
    mark = {"crit": "●", "warn": "▲", "ok": "✓"}.get(level, "?")
    return f"  {c}{mark}{RESET} {name:<12} {value}"


# ---------------------------------------------------------------- 报告

def make_report() -> dict:
    """采集一次快照,返回结构化报告。"""
    mem = read_meminfo()
    ms = mem_status(mem)
    temp = read_temperature()
    load = read_loadavg()
    disk = read_disk(os.path.expanduser("~"))
    top = top_processes(6)

    # 综合等级:取最严重的
    overall = ms["level"]
    reasons = list(ms["reasons"])
    if temp is not None:
        if temp >= CRIT_TEMP_C:
            overall = "crit"
            reasons.append(f"温度 {temp:.0f}°C")
        elif temp >= WARN_TEMP_C:
            if overall == "ok":
                overall = "warn"
            reasons.append(f"温度 {temp:.0f}°C")

    return {
        "time": time.strftime("%H:%M:%S"),
        "mem": ms,
        "temp": temp,
        "load": load,
        "disk": disk,
        "top": top,
        "overall": overall,
        "reasons": reasons,
    }


def render_report(r: dict) -> str:
    L = []
    lvl = r["overall"]
    c = level_color(lvl)
    label = {"crit": "危险", "warn": "警惕", "ok": "安全"}[lvl]
    L.append(f"\n{BOLD}🔍 哨兵体检 · {r['time']}{RESET}  总体: {c}{BOLD}{label}{RESET}")
    if r["reasons"]:
        L.append(f"  {c}⚠ {'; '.join(r['reasons'])}{RESET}")

    ms = r["mem"]
    used_pct = int(ms["used_mb"] * 100 / ms["total_mb"]) if ms["total_mb"] else 0
    avail_pct = 100 - used_pct
    mem_level = "crit" if ms["avail_mb"] and ms["avail_mb"] < CRIT_MEM_MB else (
        "warn" if ms["avail_mb"] and ms["avail_mb"] < WARN_MEM_MB else "ok")
    L.append("")
    L.append(f"  {BOLD}内存{RESET}  已用 {fmt_mb(ms['used_mb'])} / {fmt_mb(ms['total_mb'])} ({used_pct}%)  可用 {fmt_mb(ms['avail_mb'])}")
    L.append(f"         {bar(avail_pct, mem_level, 24)}  ← 可用水位")
    swap_level = "crit" if ms["swap_pct"] > CRIT_SWAP_PCT else (
        "warn" if ms["swap_pct"] > WARN_SWAP_PCT else "ok")
    L.append(f"  {BOLD}swap{RESET}   已用 {fmt_mb(ms['swap_used_mb'])} / {fmt_mb(ms['swap_total_mb'])} ({ms['swap_pct']}%)  {bar(ms['swap_pct'], swap_level, 24)}")

    temp = r["temp"]
    if temp is not None:
        t_level = "crit" if temp >= CRIT_TEMP_C else ("warn" if temp >= WARN_TEMP_C else "ok")
        L.append(f"  {BOLD}温度{RESET}   {temp:.0f}°C  {bar(int(temp), t_level, 24)}")

    load = r["load"]
    if load:
        L.append(f"  {BOLD}负载{RESET}   {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f} (1/5/15 分钟)")

    disk = r["disk"]
    if disk:
        d_level = "crit" if disk["pct"] > 90 else ("warn" if disk["pct"] > 75 else "ok")
        L.append(f"  {BOLD}磁盘{RESET}   {disk['used']:.0f}G / {disk['total']:.0f}G ({disk['pct']}%)  {bar(disk['pct'], d_level, 24)}")

    L.append("")
    L.append(f"  {BOLD}内存大户 TOP{RESET}")
    for name, pid, rss in r["top"]:
        L.append(f"    {rss:>6}MB  {name:<24} pid {pid}")

    return "\n".join(L)


# ---------------------------------------------------------------- 模式

def mode_report() -> int:
    print(render_report(make_report()))
    return 0


def mode_watch() -> int:
    """持续盯防:每 WATCH_INTERVAL 秒刷新,等级变化/危险时高亮。"""
    print(f"{CYAN}👀 哨兵持续盯防中 (每 {WATCH_INTERVAL:.0f}s 刷新, Ctrl+C 退出){RESET}")
    prev = None
    try:
        while True:
            r = make_report()
            line = (f"{level_color(r['overall'])}[{time.strftime('%H:%M:%S')}] "
                    f"可用 {fmt_mb(r['mem']['avail_mb'])} "
                    f"swap {r['mem']['swap_pct']}%"
                    + (f" 温度 {r['temp']:.0f}°C" if r['temp'] is not None else "")
                    + f"  等级:{r['overall'].upper()}{RESET}")
            if r["overall"] != prev:
                line = BOLD + line + RESET
                if r["overall"] == "crit":
                    line += "  ⚠⚠ 危险!手机随时可能杀后台进程,先停掉大内存的事!"
                prev = r["overall"]
            print(line, flush=True)
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        print("\n已停止盯防。")
    return 0


def _spawn_detached(cmd: list) -> bool:
    """用 setsid 分离启动一个命令,不等待。返回是否成功启动。"""
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except OSError:
        return False


def _is_alive(spec: str) -> bool:
    """判断服务是否存活。spec 形如: name:port / name:cmd:port / :port """
    parts = spec.split(":")
    port = None
    for p in parts:
        if p.isdigit():
            port = int(p)
    if port is None:
        print(f"{RED}!! 守护目标 '{spec}' 没有端口,无法探测。格式: 名称:端口{RESET}")
        return True  # 探测不了就当作活着,避免误杀
    return is_port_open(port)


def mode_guard(service: str, start_cmd: list) -> int:
    """守护一个服务:每 GUARD_INTERVAL 秒探测端口,挂了自动拉起。"""
    name = service.split(":")[0]
    print(f"{CYAN}🛡 哨兵守护模式:盯防 '{service}'(每 {GUARD_INTERVAL:.0f}s 探测,"
          f"挂了自动执行: {' '.join(start_cmd)}){RESET}")
    alive = True
    try:
        while True:
            ok = _is_alive(service)
            ts = time.strftime("%H:%M:%S")
            if ok:
                if not alive:
                    print(f"{GREEN}[{ts}] ✓ {name} 已恢复在线{RESET}")
                    alive = True
            else:
                print(f"{YELLOW}[{ts}] ✗ {name} 掉线了,尝试拉起...{RESET}", flush=True)
                ok_after = False
                if _spawn_detached(start_cmd):
                    # 拉起后轮询等待就绪:慢启动服务(llama-server 加载模型)
                    # 需要几十秒,不能只等一个固定间隔就判定失败。
                    # 最长等 GUARD_INTERVAL 秒,期间每 0.5s 探测一次。
                    deadline = time.time() + max(3.0, GUARD_INTERVAL)
                    while time.time() < deadline:
                        time.sleep(0.5)
                        if _is_alive(service):
                            ok_after = True
                            break
                if ok_after:
                    print(f"{GREEN}[{time.strftime('%H:%M:%S')}] ✓ {name} 拉起成功{RESET}")
                    alive = True
                else:
                    print(f"{RED}[{time.strftime('%H:%M:%S')}] ✗ {name} 拉起失败,继续盯防{RESET}")
            time.sleep(GUARD_INTERVAL)
    except KeyboardInterrupt:
        print(f"\n已停止守护 '{name}'。")
    return 0


def print_usage() -> int:
    print(f"""sentinel {VERSION} — Termux/Android 内存哨兵(零依赖,纯标准库)

用法:
  sentinel                    查看一次体检报告
  sentinel watch              持续盯防,内存/温度危险时高亮告警
  sentinel guard <名称:端口> <重启命令...>   守护服务:挂了自动拉起

示例:
  sentinel
  sentinel watch
  sentinel guard llama:8080 ~/start-local-model.sh

环境变量(可选):
  SENTINEL_WARN_MEM_MB / SENTINEL_CRIT_MEM_MB   内存告警阈值(默认 4096/2048 MB)
  SENTINEL_WARN_SWAP_PCT / SENTINEL_CRIT_SWAP_PCT  swap 阈值(默认 50/80 %)
  SENTINEL_WARN_TEMP_C / SENTINEL_CRIT_TEMP_C    温度阈值(默认 55/70 °C)
  SENTINEL_WATCH_INTERVAL / SENTINEL_GUARD_INTERVAL  轮询间隔(默认 5/10 s)
""")
    return 0


# ---------------------------------------------------------------- 入口

def main(argv: list[str]) -> int:
    # 输出重定向到 PIPE/文件时,Python 默认块缓冲会吞掉日志;
    # guard/watch 是长驻进程,必须行缓冲,否则 kill 时丢失"拉起成功"等关键输出。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    # 无参数 = 直接看一次体检报告(最常用的用法)
    if not argv:
        return mode_report()
    if argv[0] in ("-h", "--help", "help"):
        return print_usage()

    cmd = argv[0]
    if cmd == "watch":
        return mode_watch()
    if cmd == "guard":
        if len(argv) < 3:
            print(f"{RED}!! guard 需要 <名称:端口> 和 <重启命令>{RESET}")
            return print_usage()
        return mode_guard(argv[1], argv[2:])
    if cmd in ("status", "report", "check", "info"):
        return mode_report()
    if cmd == "version":
        print(f"sentinel {VERSION}")
        return 0

    # 未知命令但带参数 → 报错;无参数 → 报告
    if cmd.startswith("-"):
        print(f"{RED}!! 未知选项: {cmd}{RESET}")
        return print_usage()
    print(f"{RED}!! 未知命令: {cmd}{RESET}")
    return print_usage()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # 管道下游关闭(如 sentinel | head),安静退出而不是刷一堆 Traceback
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
