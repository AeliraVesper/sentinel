# sentinel · 内存哨兵

Termux / Android 专属的内存与温度哨兵工具。零第三方依赖,纯 Python 标准库,直接在手机上跑。

**它解决什么问题?** Android 在内存紧张时会悄悄杀掉后台进程(比如本地模型服务),没有任何提示。sentinel 盯着内存水位、swap、温度,**在系统动手之前给你预警**,还能守护指定服务,掉了自动拉起。

## 安装

```bash
cp sentinel.py $PREFIX/bin/sentinel   # 或 ~/../usr/bin/sentinel
chmod +x $PREFIX/bin/sentinel
```

## 用法

```bash
sentinel                     # 看一次体检报告
sentinel watch               # 持续盯防,危险时高亮告警
sentinel guard llama:8080 ~/start-local-model.sh   # 守护服务:挂了自动拉起
```

### 体检报告长这样

```
🔍 哨兵体检 · 08:02:57  总体: 警惕
  ⚠ 可用内存 2708MB(<4096MB); 温度 68°C

  内存  已用 8.1G / 10.8G (75%)  可用 2.6G
         ██████░░░░░░░░░░░░░░░░░░  ← 可用水位
  swap   已用 3.6G / 12.0G (29%)  ██████░░░░░░░░░░░░░░░░░░
  温度   68°C  ████████████████░░░░░░░░
  磁盘   337G / 477G (70%)  ████████████████░░░░░░░░

  内存大户 TOP
       159MB  linker64                 pid 25462
        20MB  linker64                 pid 2621
```

## 阈值(可用环境变量覆盖)

| 变量 | 默认 | 含义 |
|---|---|---|
| SENTINEL_WARN_MEM_MB / CRIT | 4096 / 2048 | 可用内存预警/危险 (MB) |
| SENTINEL_WARN_SWAP_PCT / CRIT | 50 / 80 | swap 使用率预警/危险 (%) |
| SENTINEL_WARN_TEMP_C / CRIT | 55 / 70 | 温度预警/危险 (°C) |
| SENTINEL_WATCH_INTERVAL | 5 | watch 刷新间隔 (s) |
| SENTINEL_GUARD_INTERVAL | 10 | guard 探测间隔 (s) |

## guard 模式说明

`sentinel guard <名称:端口> <启动命令...>` 通过探测端口判断服务是否存活,
掉了就自动执行启动命令拉起。慢启动服务(llama-server 要加载几十秒模型)会
轮询等待就绪,不会误报失败。

## 测试

```bash
python3 test_sentinel.py    # 40 项自检:数据读取、阈值判定、报告渲染、端口探测、guard 端到端
```
