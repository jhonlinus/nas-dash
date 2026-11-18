#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAS Dash — 单文件 NAS 状态面板
================================
零第三方依赖，只读 /proc、df、smartctl、docker ps。

两种运行模式:
  local : 直接在 NAS 上跑（读本机 /proc）
  ssh   : 在任意机器上跑，用 ssh（或任何远程执行器）到 NAS 取数据

用法:
  python3 nasdash.py --mode local --port 8066
  python3 nasdash.py --mode ssh --remote 'ssh root@nas.local {cmd}' --port 8066
"""

import argparse
import base64
import copy
import hmac
import json
import os
import re
import shlex
import socketserver
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("NASDASH_DATA") or (HERE / "data"))
DATA.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 采集脚本

SAMPLE_SCRIPT = r"""
export LC_ALL=C
echo "@@HOST"
hostname; uname -r; cat /proc/uptime; cat /proc/loadavg; nproc
echo "@@MEM"
grep -E "^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Shmem|SReclaimable):" /proc/meminfo
echo "@@DF"
df -kP -x tmpfs -x devtmpfs -x overlay -x efivarfs -x squashfs -x fuse.gvfsd-fuse 2>/dev/null
echo "@@RAID"
head -20 /proc/mdstat 2>/dev/null
echo "@@DISK"
lsblk -d -o NAME,MODEL,SIZE,ROTA,TYPE 2>/dev/null
echo "@@TEMP"
sensors 2>/dev/null
echo "@@DOCKER"
timeout 6 docker ps --no-trunc --format "{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}" 2>/dev/null | head -80
echo "@@CPU"
grep -E "^cpu[0-9 ]" /proc/stat
echo "@@NET"
cat /proc/net/dev
echo "@@END"
"""

# 仅 ssh 模式需要：远端没有共享 /sys，只能让 shell 帮忙读 cgroup
CG_SHELL_SCRIPT = r"""
export LC_ALL=C
echo "@@CGROUP"
for f in /sys/fs/cgroup/system.slice/docker-*.scope; do
  [ -r "$f/cpu.stat" ] || continue
  b=${f##*/}; id=${b#docker-}; id=${id%.scope}
  cu=$(awk '/^usage_usec/{print $2; exit}' "$f/cpu.stat" 2>/dev/null)
  mc=$(cat "$f/memory.current" 2>/dev/null)
  in=$(awk '/^inactive_file/{print $2; exit}' "$f/memory.stat" 2>/dev/null)
  echo "CG $id ${cu:-0} ${mc:-0} ${in:-0}"
done
echo "@@END"
"""

IO_SCRIPT = r"""
export LC_ALL=C
echo "@@CPU"
grep -E "^cpu[0-9 ]" /proc/stat
echo "@@NET"
cat /proc/net/dev
echo "@@END"
"""

SMART_SCRIPT = r"""
export LC_ALL=C
echo "@@SMART"
for d in /dev/sda /dev/sdb /dev/sdc /dev/sdd /dev/nvme0n1 /dev/nvme1n1 ; do
  [ -b "$d" ] || continue
  out=$(smartctl -H -i -A "$d" 2>&1)
  case "$out" in
    *"Unable to detect"*|*"Unknown USB"*|*"unsupported"*)
      out=$(smartctl -H -i -A -d sat "$d" 2>&1) ;;
  esac
  echo "--DEV $d"
  echo "$out"
done
echo "@@END"
"""

PHYS_IF = re.compile(r"^(enp|eno|eth|wlan|bond|enx|usb)")


# ---------------------------------------------------------------- 执行器

class Runner:
    """local 在本机执行采集脚本；ssh 把脚本交给 --remote 指定的远程执行器。

    采集脚本整段 base64 后再走远程，避免多行文本被引号/转义规则拆坏。
    """

    def __init__(self, mode, remote=None):
        self.mode = mode
        self.remote = (remote or "").strip()

    def remote_argv(self, cmd):
        """把 --remote 模板展开成 argv。模板用 {cmd} 占位，没占位就追加到末尾。

        只要能非交互执行命令的东西都能接：
          ssh -o BatchMode=yes root@nas {cmd}
          sshpass -p xxx ssh root@nas {cmd}
          docker exec nas-dash {cmd}
          python3 jssh.py exec mynas {cmd}      # JumpServer 之类的跳板机脚本
        """
        parts = shlex.split(self.remote)
        if not parts:
            raise SystemExit(
                "ssh 模式需要 --remote，例如：--remote 'ssh root@nas.local {cmd}'"
            )
        if any("{cmd}" in p for p in parts):
            return [p.replace("{cmd}", cmd) for p in parts]
        return parts + [cmd]

    def run(self, script):
        if self.mode == "local":
            r = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                errors="replace", timeout=120,
            )
            return r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        cmd = "echo %s | base64 -d | bash" % b64
        try:
            argv = self.remote_argv(cmd)
        except SystemExit as e:
            return "ERROR: %s" % e
        r = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=180,
        )
        out = r.stdout or ""
        if not out.strip() and r.stderr:
            out = r.stderr
        return out


def split_sections(text):
    """把 @@XXX 分段输出切成 dict"""
    sec, cur = {}, None
    for line in text.splitlines():
        if line.startswith("@@") and line[2:].strip().isalnum():
            name = line[2:].strip().upper()
            if name == "END":
                cur = None
                continue
            cur = sec.setdefault(name, [])
            continue
        if cur is not None:
            cur.append(line)
    return sec


# ---------------------------------------------------------------- 解析

def parse_cpu(sec):
    """返回 (总使用率%, [每核%], 各字段累计值供下次对比)"""
    def row(parts):
        v = [int(x) for x in parts[1:9]]
        idle = v[3] + v[4]
        return v, sum(v), idle

    c1, c2 = sec.get("CPU1", []), sec.get("CPU2", [])
    if not c1 or not c2:
        return None, [], {}
    total_pct, per_core = None, []
    m1, m2 = {}, {}
    for line in c1:
        f = line.split()
        if f:
            m1[f[0]] = f[1:]
    for line in c2:
        f = line.split()
        if f:
            m2[f[0]] = f[1:]
    for key in m2:
        if key not in m1:
            continue
        a = [int(x) for x in m1[key][:8]]
        b = [int(x) for x in m2[key][:8]]
        ta, tb = sum(a), sum(b)
        dt = tb - ta
        di = (b[3] + b[4]) - (a[3] + a[4])
        pct = 0.0 if dt <= 0 else max(0.0, min(100.0, (1 - di / dt) * 100))
        if key == "cpu":
            total_pct = round(pct, 1)
        elif key.startswith("cpu"):
            per_core.append(round(pct, 1))
    snap = {}
    for key in m2:
        snap[key] = [int(x) for x in m2[key][:8]]
    return total_pct, per_core, snap


def parse_net(sec, elapsed=1.0):
    def grab(lines):
        d = {}
        for line in lines:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            if not PHYS_IF.match(iface):
                continue
            f = rest.split()
            if len(f) < 9:
                continue
            d[iface] = {"rx": int(f[0]), "tx": int(f[8])}
        return d

    n1, n2 = grab(sec.get("NET1", [])), grab(sec.get("NET2", []))
    span = elapsed if elapsed and elapsed > 0 else 1.0
    per_if, total = [], {"rx": 0.0, "tx": 0.0}
    for iface, v2 in n2.items():
        v1 = n1.get(iface)
        if not v1:
            continue
        rx = max(0.0, (v2["rx"] - v1["rx"]) / span)
        tx = max(0.0, (v2["tx"] - v1["tx"]) / span)
        per_if.append({"if": iface, "rx": rx, "tx": tx,
                       "rx_total": v2["rx"], "tx_total": v2["tx"]})
        total["rx"] += rx
        total["tx"] += tx
    per_if.sort(key=lambda x: x["rx"] + x["tx"], reverse=True)
    return {"ifaces": per_if, "rx": total["rx"], "tx": total["tx"]}


def parse_mem(sec):
    kv = {}
    for line in sec.get("MEM", []):
        m = re.match(r"^(\w+):\s+(\d+)", line)
        if m:
            kv[m.group(1)] = int(m.group(2))
    total = kv.get("MemTotal", 0)
    avail = kv.get("MemAvailable", kv.get("MemFree", 0))
    used = max(0, total - avail)
    st, sf = kv.get("SwapTotal", 0), kv.get("SwapFree", 0)
    return {
        "total_kb": total, "used_kb": used, "avail_kb": avail,
        "cached_kb": kv.get("Cached", 0) + kv.get("SReclaimable", 0),
        "used_pct": round(used / total * 100, 1) if total else 0,
        "swap_total_kb": st, "swap_used_kb": max(0, st - sf),
        "swap_pct": round((st - sf) / st * 100, 1) if st else 0,
    }


SKIP_MOUNT = ("/boot", "/run", "/dev", "/sys", "/snap", "/var/lib/docker",
              "/vol1/docker", "/var/lib/kubelet", "/var/lib/containerd")


def parse_df(sec):
    vols = []
    seen = set()
    for line in sec.get("DF", []):
        f = line.split()
        if len(f) < 6 or not f[1].isdigit():
            continue
        dev, size_kb, used_kb, avail_kb, pct, mnt = f[0], int(f[1]), int(f[2]), int(f[3]), f[4], f[5]
        if mnt in seen or size_kb <= 0:
            continue
        if any(mnt == s or mnt.startswith(s + "/") for s in SKIP_MOUNT):
            continue
        if size_kb > 50 * 1024 * 1024 * 1024:  # 跳过 16E 之类的虚拟挂载
            continue
        seen.add(mnt)
        label = mnt
        kind = "internal"
        if mnt.startswith("/vol00/") or mnt.startswith("/mnt/") or mnt.startswith("/media/"):
            kind = "external"
            label = mnt.rstrip("/").split("/")[-1]
        elif mnt == "/":
            label = "系统盘"
        elif re.match(r"^/vol\d+$", mnt):
            label = mnt.replace("/vol", "存储卷 ")
        elif mnt == "/fs":
            label = "主存储池"
        vols.append({
            "mount": mnt, "dev": dev, "label": label, "kind": kind,
            "size": size_kb * 1024, "used": used_kb * 1024, "avail": avail_kb * 1024,
            "pct": float(pct.rstrip("%").replace("-", "0") or 0),
        })
    vols.sort(key=lambda v: (v["kind"] != "internal", v["mount"]))
    return vols


def parse_raid(sec):
    out, cur = [], None
    for line in sec.get("RAID", []):
        m = re.match(r"^(md\d+)\s*:\s*(\w+)\s+(\w+)\s+(.*)$", line)
        if m:
            cur = {"dev": m.group(1), "active": m.group(2) == "active",
                   "level": m.group(3), "detail": m.group(4).strip(), "members": []}
            out.append(cur)
            continue
        if cur and line.startswith(" "):
            cur["members"].append(line.strip())
    for r in out:
        blob = " ".join(r["members"])
        mm = re.search(r"\[(\d+)/(\d+)\]", blob)
        r["total"] = int(mm.group(1)) if mm else 0
        r["ok"] = int(mm.group(2)) if mm else 0
        r["degraded"] = "[U_]" in blob or "[_U]" in blob or (r["total"] and r["ok"] < r["total"])
    return out


def parse_temp(sec):
    """解析 sensors 输出，返回 {标签: 温度}"""
    res, adapter, sub = {}, None, None
    for line in sec.get("TEMP", []):
        if not line.strip():
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":") is False and ":" not in line:
            adapter = line.strip()
            continue
        if "Adapter:" in line:
            continue
        m = re.match(r"^(\S[^:]*):\s*\+?([\d.]+)\s*(?:°C|C)", line)
        if not m:
            continue
        name, val = m.group(1).strip(), float(m.group(2))
        low = (adapter or "").lower()
        if name.startswith("Package"):
            res["cpu"] = val
        elif "nvme" in low and name in ("Composite", "Sensor 1"):
            res.setdefault("nvme", val)
        elif "drivetemp" in low:
            res.setdefault("hdd", val)
        elif "pch" in low:
            res.setdefault("pch", val)
        elif "i915" in low:
            res.setdefault("gpu", val)
        elif name == "temp1":
            res.setdefault("hdd", val)
        elif name.startswith("Core"):
            res.setdefault("cores", [])
            if isinstance(res.get("cores"), list):
                res["cores"].append(val)
    return res


def parse_disklist(sec):
    disks = []
    for line in sec.get("DISK", [])[1:]:
        f = line.split()
        if len(f) < 4:
            continue
        name = f[0]
        typ = f[-1]
        rota = f[-2]
        size = f[-3]
        model = " ".join(f[1:-3]).strip() or name
        disks.append({"dev": "/dev/" + name, "model": model,
                      "size": size, "ssd": rota == "0", "type": typ})
    return disks


def parse_docker(sec):
    rows = []
    for line in sec.get("DOCKER", []):
        if not line.strip():
            continue
        if "\t" in line:
            f = [x.strip() for x in line.split("\t")]
        elif "\\t" in line:
            f = [x.strip() for x in line.split("\\t")]
        else:
            f = re.split(r"\s{2,}", line.strip())
        if len(f) < 3 or not f[1]:
            continue
        cid, name, status = f[0], f[1], f[2]
        image = f[3] if len(f) > 3 else ""
        rows.append({
            "id": cid, "name": name, "status": status, "image": image,
            "up": status.lower().startswith("up"),
            "healthy": "healthy" in status.lower(),
        })
    rows.sort(key=lambda x: (not x["up"], x["name"].lower()))
    return rows


# ---------------------------------------------------------------- 容器资源（直读 cgroup）

CG_DIRS = ("/sys/fs/cgroup/system.slice", "/sys/fs/cgroup/docker")


def _cg_vals(cpu_txt, mem_txt, stat_txt):
    cpu = 0
    m = re.search(r"^usage_usec\s+(\d+)", cpu_txt or "", re.M)
    if m:
        cpu = int(m.group(1))
    try:
        mem = int((mem_txt or "0").strip() or 0)
    except ValueError:
        mem = 0
    inact = 0
    m = re.search(r"^inactive_file\s+(\d+)", stat_txt or "", re.M)
    if m:
        inact = int(m.group(1))
    return {"cpu_usec": cpu, "mem": max(0, mem - inact)}


def read_cgroups(sec):
    """容器 cgroup 用量：优先用采集脚本的结果，local 模式直接读 /sys（零子进程）"""
    out = {}
    for line in sec.get("CGROUP", []) or []:
        f = line.strip().split()
        if len(f) < 5 or f[0] != "CG":
            continue
        try:
            mem = max(0, int(f[3]) - int(f[4]))
            out[f[1]] = {"cpu_usec": int(f[2]), "mem": mem}
        except ValueError:
            continue
    if out:
        return out
    if os.geteuid() != 0:
        return out
    for d in CG_DIRS:
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            cid = name[7:] if name.startswith("docker-") else name
            if cid.endswith(".scope"):
                cid = cid[:-6]
            if len(cid) < 12:
                continue
            base = os.path.join(d, name)
            try:
                with open(os.path.join(base, "cpu.stat")) as fh:
                    cpu_txt = fh.read()
            except OSError:
                continue
            try:
                with open(os.path.join(base, "memory.current")) as fh:
                    mem_txt = fh.read()
            except OSError:
                mem_txt = "0"
            try:
                with open(os.path.join(base, "memory.stat")) as fh:
                    stat_txt = fh.read()
            except OSError:
                stat_txt = ""
            out[cid] = _cg_vals(cpu_txt, mem_txt, stat_txt)
    return out


def attach_container_stats(rows, cg, prev_cg, elapsed):
    """把 cgroup 增量换算成每容器 CPU% 与内存字节，挂到容器列表上"""
    span = elapsed if elapsed and elapsed > 0 else 0
    for c in rows:
        cid = c.get("id") or ""
        key = None
        if cid in cg:
            key = cid
        else:
            for k in cg:
                if cid and (cid.startswith(k) or k.startswith(cid)):
                    key = k
                    break
        if not key:
            continue
        cur = cg[key]
        c["mem"] = cur["mem"]
        prev = (prev_cg or {}).get(key)
        if prev and span > 0:
            d_us = cur["cpu_usec"] - prev["cpu_usec"]
            if d_us >= 0:
                c["cpu"] = round(min(999.0, d_us / (span * 1e6) * 100), 1)
        c["_cg"] = key
    return rows


def strip_internal(rows):
    for c in rows:
        c.pop("_cg", None)
        c.pop("id", None)
    return rows


# ---------------------------------------------------------------- SMART

def _att(attrs, aid):
    r = attrs.get(aid)
    if not r:
        return None
    raw = r["raw"]
    m = re.match(r"^(\d+)", raw)
    return int(m.group(1)) if m else None


def parse_smart(text):
    blocks = re.split(r"^--DEV\s+", text, flags=re.M)
    out = []
    for b in blocks:
        if not b.strip():
            continue
        lines = b.splitlines()
        dev = lines[0].strip()
        if not dev.startswith("/dev/"):
            continue
        body = "\n".join(lines[1:])
        if "START OF INFORMATION SECTION" not in body and "START OF SMART DATA SECTION" not in body:
            out.append({"dev": dev, "ok": False, "error": "无 SMART 响应"})
            continue
        d = {"dev": dev, "ok": True}
        m = re.search(r"Device Model:\s+(.+)", body) or re.search(r"Model Number:\s+(.+)", body)
        d["model"] = m.group(1).strip() if m else dev
        m = re.search(r"Model Family:\s+(.+)", body)
        d["family"] = m.group(1).strip() if m else ""
        m = re.search(r"Serial Number:\s+(.+)", body)
        d["serial"] = m.group(1).strip() if m else ""
        m = re.search(r"User Capacity:\s+([\d,]+) bytes(?:\s+\[([^\]]+)\])?", body)
        if m:
            d["bytes"] = int(m.group(1).replace(",", ""))
            d["capacity"] = m.group(2) or ""
        else:
            m = re.search(r"Total NVM Capacity:\s+([\d,]+) bytes(?:\s+\[([^\]]+)\])?", body)
            if m:
                d["bytes"] = int(m.group(1).replace(",", ""))
                d["capacity"] = m.group(2) or ""
        m = re.search(r"Rotation Rate:\s+(\d+\s*rpm)", body)
        d["rpm"] = m.group(1) if m else ""
        m = re.search(r"SMART overall-health self-assessment test result:\s+(\w+)", body)
        d["health"] = m.group(1) if m else "UNKNOWN"
        nvme = "SMART/Health Information" in body
        d["nvme"] = nvme
        attrs = {}
        for line in body.splitlines():
            m = re.match(r"^\s*(\d+)\s+(\S+)\s+0x[0-9a-f]+\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S|-)\s+(.+)$", line)
            if m:
                attrs[int(m.group(1))] = {"name": m.group(2), "value": int(m.group(3)),
                                          "worst": int(m.group(4)), "thresh": int(m.group(5)),
                                          "raw": m.group(9).strip()}
        if nvme:
            m = re.search(r"Temperature:\s+(\d+)\s+Celsius", body)
            d["temp"] = int(m.group(1)) if m else None
            m = re.search(r"Available Spare:\s+(\d+)%", body)
            d["spare"] = int(m.group(1)) if m else None
            m = re.search(r"Percentage Used:\s+(\d+)%", body)
            d["used_pct"] = int(m.group(1)) if m else None
            m = re.search(r"Power On Hours:\s+([\d,]+)", body)
            d["hours"] = int(m.group(1).replace(",", "")) if m else None
            m = re.search(r"Data Units Written:\s+([\d,]+)", body)
            d["written_tb"] = round(int(m.group(1).replace(",", "")) * 512000 / 1e12, 1) if m else None
            m = re.search(r"Critical Warning:\s+0x([0-9a-f]+)", body)
            d["crit_warn"] = int(m.group(1), 16) if m else 0
            d["realloc"] = None
            d["pending"] = None
            d["uncorr"] = None
            d["crc"] = None
        else:
            d["realloc"] = _att(attrs, 5)
            d["pending"] = _att(attrs, 197)
            d["uncorr"] = _att(attrs, 198)
            d["crc"] = _att(attrs, 199)
            d["hours"] = _att(attrs, 9)
            d["temp"] = _att(attrs, 194) or _att(attrs, 190)
            d["load_cycles"] = _att(attrs, 193)
        # 风险评级
        risk, notes = 0, []
        if d.get("health") not in ("PASSED", "OK", None):
            risk = 3
            notes.append("SMART 自检未通过")
        if d.get("crit_warn"):
            risk = 3
            notes.append("NVMe 临界告警 0x%02x" % d["crit_warn"])
        if d.get("pending"):
            risk = 3
            notes.append("%d 个待映射扇区" % d["pending"])
        if d.get("uncorr"):
            risk = 3
            notes.append("%d 个不可修复扇区" % d["uncorr"])
        if d.get("realloc"):
            if d["realloc"] >= 50:
                risk = max(risk, 3)
                notes.append("已重分配 %d 个扇区" % d["realloc"])
            else:
                risk = max(risk, 2)
                notes.append("%d 个扇区已重分配" % d["realloc"])
        if d.get("crc"):
            risk = max(risk, 2)
            notes.append("CRC 错误 %d 次（查线材/接口）" % d["crc"])
        if d.get("temp") and d["temp"] >= 55:
            risk = max(risk, 2 if d["temp"] < 60 else 3)
            notes.append("温度 %d°C 偏高" % d["temp"])
        if d.get("used_pct") and d["used_pct"] >= 80:
            risk = max(risk, 2)
            notes.append("寿命已消耗 %d%%" % d["used_pct"])
        d["risk"] = risk
        d["notes"] = notes
        out.append(d)
    return out


# ---------------------------------------------------------------- 快照组装

def build_snapshot(runner, smart_cache):
    raw = runner.run(SAMPLE_SCRIPT)
    sec = split_sections(raw)
    if getattr(runner, "mode", "local") == "ssh":
        # 远端读不到 /sys，额外让 shell 采一次容器 cgroup
        sec.update(split_sections(runner.run(CG_SHELL_SCRIPT)))
    if not sec.get("HOST"):
        raise RuntimeError("采不到数据：\n" + raw[-500:])

    host = sec["HOST"]
    name = host[0].strip() if len(host) > 0 and host[0].strip() else "NAS"
    kernel = host[1].strip() if len(host) > 1 else ""
    up_s = float(host[2].split()[0]) if len(host) > 2 and host[2].split() else 0
    load = [float(x) for x in host[3].split()[:3]] if len(host) > 3 else [0, 0, 0]
    cores_n = int(host[4]) if len(host) > 4 and host[4].strip().isdigit() else (os.cpu_count() or 1)

    # ---- CPU / 网络：用上一轮采样做增量，省掉脚本里的 sleep 1 ----
    prev = STATE.get("prev_io") or {}
    now = time.time()
    span = (now - prev["ts"]) if prev.get("ts") else 0
    cpu_lines = sec.get("CPU", [])
    net_lines = sec.get("NET", [])
    if not prev.get("cpu"):
        # 首次运行没有基准，短睡 0.6 秒补一次
        time.sleep(0.6)
        io = split_sections(runner.run(IO_SCRIPT))
        prev = {"cpu": io.get("CPU", []), "net": io.get("NET", []), "ts": now}
        span = 0.6
    sec_io = dict(sec)
    sec_io["CPU1"] = prev.get("cpu", [])
    sec_io["CPU2"] = cpu_lines
    sec_io["NET1"] = prev.get("net", [])
    sec_io["NET2"] = net_lines

    cpu_pct, per_core, _ = parse_cpu(sec_io)
    net = parse_net(sec_io, span)
    mem = parse_mem(sec)
    vols = parse_df(sec)
    raid = parse_raid(sec)
    temps = parse_temp(sec)
    disks = parse_disklist(sec)
    docker = parse_docker(sec)

    # ---- 容器 CPU/内存：cgroup 直读（毫秒级），与 docker ps 列表解耦 ----
    cg = read_cgroups(sec)
    attach_container_stats(docker, cg, STATE.get("prev_cg"), span)
    STATE["prev_cg"] = cg
    STATE["prev_io"] = {"cpu": cpu_lines, "net": net_lines, "ts": now}
    docker_meta = {"cgroup_ok": bool(cg), "cg_total": len(cg)}

    # ssd 映射到 SMART 结果
    ssd_map = {d["dev"]: d["ssd"] for d in disks}
    smart = []
    if smart_cache.get("data"):
        smart = copy.deepcopy(smart_cache["data"])
        for s in smart:
            if "model" in s and not s.get("rpm") and s["dev"] in ssd_map and ssd_map[s["dev"]]:
                s["rpm"] = "SSD"

    total_sz = sum(v["size"] for v in vols)
    used_sz = sum(v["used"] for v in vols)

    warnings = []
    if mem["swap_pct"] >= 90 and mem["swap_total_kb"]:
        warnings.append({"level": "danger", "title": "交换分区几乎用尽",
                         "text": "swap 已用 %s / %s（%.0f%%），物理内存长期吃紧，建议查内存占用 top 或加内存。"
                                 % (gb(mem["swap_used_kb"] * 1024), gb(mem["swap_total_kb"] * 1024), mem["swap_pct"])})
    elif mem["swap_pct"] >= 60 and mem["swap_total_kb"]:
        warnings.append({"level": "warn", "title": "交换分区使用偏高",
                         "text": "swap 已用 %.0f%%。" % mem["swap_pct"]})
    if mem["used_pct"] >= 90:
        warnings.append({"level": "danger", "title": "内存占用过高", "text": "物理内存已用 %.0f%%。" % mem["used_pct"]})
    for v in vols:
        if v["pct"] >= 92:
            warnings.append({"level": "danger", "title": "存储空间告急 · %s" % v["label"],
                             "text": "%s 已用 %.0f%%，剩余 %s。" % (v["mount"], v["pct"], gb(v["avail"]))})
        elif v["pct"] >= 85:
            warnings.append({"level": "warn", "title": "存储空间偏高 · %s" % v["label"],
                             "text": "%s 已用 %.0f%%。" % (v["mount"], v["pct"])})
    for s in smart:
        if s.get("risk", 0) >= 3:
            warnings.append({"level": "danger", "title": "硬盘风险 · %s" % s.get("model", s["dev"]),
                             "text": "；".join(s.get("notes") or ["SMART 异常"]) + "。建议尽快备份并准备换盘。"})
        elif s.get("risk", 0) == 2:
            warnings.append({"level": "warn", "title": "硬盘注意 · %s" % s.get("model", s["dev"]),
                             "text": "；".join(s.get("notes") or [])})
    for r in raid:
        if r.get("degraded") or not r.get("active"):
            warnings.append({"level": "danger", "title": "阵列降级 · %s" % r["dev"],
                             "text": "%s 状态异常：%s" % (r["dev"], r["detail"])})
    if load[0] > cores_n * 1.5:
        warnings.append({"level": "warn", "title": "负载偏高",
                         "text": "1 分钟负载 %.2f，共 %d 核。" % (load[0], cores_n)})
    t = temps.get("cpu")
    if t and t >= 85:
        warnings.append({"level": "danger", "title": "CPU 温度过高", "text": "CPU 封装温度 %.0f°C。" % t})
    elif t and t >= 78:
        warnings.append({"level": "warn", "title": "CPU 温度偏高", "text": "CPU 封装温度 %.0f°C。" % t})
    if CFG.get("redact"):
        for d in smart:
            d["serial"] = "" if d.get("serial") else d.get("serial")
        for c in docker:
            c["image"] = ""
    strip_internal(docker)

    order = {"danger": 0, "warn": 1, "info": 2}
    warnings.sort(key=lambda w: order.get(w["level"], 3))

    return {
        "ts": int(time.time()),
        "host": {"name": name, "kernel": kernel, "uptime": up_s,
                 "load": load, "cores": cores_n},
        "cpu": {"pct": cpu_pct, "per_core": per_core, "temp": temps.get("cpu"),
                "pch": temps.get("pch")},
        "mem": mem,
        "net": net,
        "vols": vols,
        "storage_total": {"size": total_sz, "used": used_sz,
                          "pct": round(used_sz / total_sz * 100, 1) if total_sz else 0},
        "raid": raid,
        "temps": temps,
        "disks": disks,
        "smart": smart,
        "docker": {"list": docker,
                   "up": sum(1 for d in docker if d["up"]),
                   "total": len(docker),
                   "cgroup_ok": docker_meta["cgroup_ok"]},
        "warnings": warnings,
    }


def gb(b):
    s = float(b)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if s < 1024 or u == "PB":
            return ("%.1f %s" % (s, u)) if u not in ("B", "KB") else ("%d %s" % (s, u))
        s /= 1024


# ---------------------------------------------------------------- 状态机

CFG = {"auth": None, "redact": False, "trust_gateway": True, "debug": False,
       "logfile": str(DATA / "requests.log")}

STATE = {
    "snapshot": None,
    "history": deque(maxlen=8640),
    "smart_cache": {"data": None, "ts": 0},
    "last_error": None,
    "last_ok": 0,
    "polling": False,
    "poll_count": 0,
    "interval": 30,
    "mode": "local",
}


def sampler(runner):
    while True:
        if STATE["polling"]:
            time.sleep(1)
            continue
        STATE["polling"] = True
        t0 = time.time()
        try:
            # SMART 每 5 分钟读一次（与采样间隔解耦）
            if not STATE["smart_cache"]["data"] or time.time() - STATE["smart_cache"]["ts"] > 300:
                out = runner.run(SMART_SCRIPT)
                parsed = parse_smart(out)
                if parsed and any(p.get("ok") for p in parsed):
                    STATE["smart_cache"] = {"data": parsed, "ts": int(time.time())}
            snap = build_snapshot(runner, STATE["smart_cache"])
            if STATE["smart_cache"]["data"]:
                snap["smart_ts"] = STATE["smart_cache"]["ts"]
            snap["poll_ms"] = int((time.time() - t0) * 1000)
            STATE["snapshot"] = snap
            STATE["last_ok"] = snap["ts"]
            STATE["last_error"] = None
            STATE["poll_count"] += 1
            STATE["history"].append({
                "t": snap["ts"],
                "cpu": snap["cpu"]["pct"] or 0,
                "mem": snap["mem"]["used_pct"],
                "swap": snap["mem"]["swap_pct"],
                "rx": snap["net"]["rx"],
                "tx": snap["net"]["tx"],
                "load": snap["host"]["load"][0],
            })
            persist()
        except Exception as e:  # noqa
            STATE["last_error"] = str(e)[:800]
        finally:
            STATE["polling"] = False
        # 采样间隔 = interval - 本轮耗时（保证节奏稳定）
        spent = time.time() - t0
        time.sleep(max(3, STATE["interval"] - spent))


def persist(force=False):
    now = time.time()
    # 落盘节流：最多每 60 秒写一次，避免采样变快后频繁刷盘
    if not force and now - STATE.get("last_persist", 0) < 60:
        return
    STATE["last_persist"] = now
    try:
        (DATA / "history.json").write_text(json.dumps({
            "interval": STATE["interval"],
            "points": list(STATE["history"])[-2000:],
        }, ensure_ascii=False), encoding="utf-8")
        (DATA / "snapshot.json").write_text(
            json.dumps(STATE["snapshot"], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def restore():
    try:
        h = json.loads((DATA / "history.json").read_text(encoding="utf-8"))
        for p in h.get("points", []):
            STATE["history"].append(p)
    except Exception:
        pass
    try:
        STATE["snapshot"] = json.loads((DATA / "snapshot.json").read_text(encoding="utf-8"))
    except Exception:
        pass


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def log_message(self, fmt, *args):
        # 默认静默；--debug 时写文件（用于排查飞牛统一网关的前缀/请求头形态）
        if not CFG.get("debug"):
            return
        try:
            line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args)
            lf = CFG.get("logfile")
            if lf:
                if os.path.exists(lf) and os.path.getsize(lf) > 512 * 1024:
                    os.replace(lf, lf + ".1")
                with open(lf, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass

    # 飞牛统一网关形如 /app/<appname>/...，应用需自己兼容这个前缀
    GATEWAY_RE = re.compile(r"^/app/[A-Za-z0-9._\-]+(/.*)?$")

    def _strip_gateway_prefix(self, path):
        """返回 (实际路径, 需要重定向到的地址或 None)。"""
        m = self.GATEWAY_RE.match(path)
        if not m:
            return path, None
        rest = m.group(1)
        if not rest:
            # /app/nasdash -> /app/nasdash/，否则页面里的相对路径会算错
            return path, path + "/"
        return rest, None

    def _authorized(self):
        if not CFG["auth"]:
            return True
        # 飞牛统一网关已校验飞牛登录态并注入 X-Trim-Userid，此时不再要 Basic 密码。
        # 仅信任回环来源（网关在本机），公网反代侧会清空 X-Trim-* 头，防止伪造绕过。
        if CFG.get("trust_gateway") and self.headers.get("X-Trim-Userid"):
            ip = (self.client_address or ("",))[0]
            if ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return True
        import base64 as _b64
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            pair = _b64.b64decode(hdr[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        return hmac.compare_digest(pair, CFG["auth"])

    def _json(self, obj, code=200, etag=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def _index(self):
        try:
            body = (HERE / "index.html").read_bytes()
        except OSError:
            return self.send_error(500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 允许被飞牛桌面（同源 /app/<appname>/）以 iframe 嵌入；其它站点仍禁止
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def do_GET(self):
        raw_path, _, qs = self.path.partition("?")
        path, redirect = self._strip_gateway_prefix(raw_path)
        if redirect:
            self.send_response(302)
            self.send_header("Location", redirect + (("?" + qs) if qs else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # 只放行首页与只读 API，其余一律 404（防止 data/ 目录被下载）
        if path not in ("/", "/index.html") and not path.startswith("/api/"):
            return self.send_error(404)
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="NAS Monitor"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/snapshot":
            snap = STATE["snapshot"] or {}
            ts = snap.get("ts")
            etag = 'W/"%s"' % ts if ts else None
            # 数据没变就回 304：省掉 JSON 序列化与几十 KB 传输
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("Cache-Control", "no-store")
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json({
                "snapshot": snap,
                "meta": {
                    "mode": STATE["mode"], "interval": STATE["interval"],
                    "last_ok": STATE["last_ok"], "error": STATE["last_error"],
                    "polling": STATE["polling"], "now": int(time.time()),
                },
            }, etag=etag)
        if path == "/api/history":
            return self._json({"points": list(STATE["history"])[-720:]})
        if path == "/api/refresh":
            # 冷却 10 秒，避免被反复触发放大开销
            if time.time() - STATE.get("last_manual", 0) > 10 and not STATE["polling"]:
                STATE["last_manual"] = time.time()
                threading.Thread(target=one_shot, daemon=True).start()
            return self._json({"ok": True})
        return self._index()

    def do_POST(self):
        return self.send_error(405)

    def do_HEAD(self):
        self._head = True
        try:
            return self.do_GET()
        finally:
            self._head = False


def one_shot():
    """手动刷新：跳过等待立刻采一次"""
    if STATE["polling"]:
        return
    STATE["polling"] = True
    try:
        if not STATE["smart_cache"]["data"]:
            out = RUNNER.run(SMART_SCRIPT)
            parsed = parse_smart(out)
            if parsed and any(p.get("ok") for p in parsed):
                STATE["smart_cache"] = {"data": parsed, "ts": int(time.time())}
        snap = build_snapshot(RUNNER, STATE["smart_cache"])
        if STATE["smart_cache"]["data"]:
            snap["smart_ts"] = STATE["smart_cache"]["ts"]
        STATE["snapshot"] = snap
        STATE["last_ok"] = snap["ts"]
        STATE["last_error"] = None
    except Exception as e:
        STATE["last_error"] = str(e)[:800]
    finally:
        STATE["polling"] = False


RUNNER = None


_UnixBase = getattr(socketserver, "ThreadingUnixStreamServer", None)


if _UnixBase is None:  # Windows 上的 Python 没有 AF_UNIX socket 服务类
    UnixHTTPServer = None
else:

    class UnixHTTPServer(_UnixBase):
        """Unix Domain Socket 上的 HTTP 服务。

        飞牛 fnOS 的统一网关（trim_http_cgi）按 app/ui/config 里的
        gatewaySocket 去连 <安装根>/app.sock，把 /app/<appname>/ 的请求转发进来，
        这样应用不占端口、也能从外网经飞牛域名访问。
        """

        daemon_threads = True

        def __init__(self, sock_path, handler):
            self.sock_path = sock_path
            parent = os.path.dirname(os.path.abspath(sock_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            # 上次异常退出可能留下失效的 socket 文件，不清理会 bind 失败
            try:
                if os.path.exists(sock_path):
                    os.unlink(sock_path)
            except OSError:
                pass
            super().__init__(sock_path, handler)
            try:
                os.chmod(sock_path, 0o660)
            except OSError:
                pass

        def get_request(self):
            # AF_UNIX 没有对端地址，伪造一个回环地址，让 Handler 的
            # client_address / address_string() 逻辑保持正常
            req, _ = super().get_request()
            return req, ("127.0.0.1", 0)


def main():
    global RUNNER
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="local", choices=["local", "ssh"])
    ap.add_argument("--remote", default=os.environ.get("NASDASH_REMOTE", ""),
                    help="ssh 模式的远程执行命令模板，用 {cmd} 占位，例如 "
                         "'ssh -o BatchMode=yes root@nas.local {cmd}'")
    ap.add_argument("--port", type=int, default=8066, help="TCP 端口；给 0 表示不监听 TCP")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="监听地址，默认只给本机反代用；容器内或要局域网直连改 0.0.0.0")
    ap.add_argument("--sock", default=None,
                    help="额外监听 Unix Domain Socket（飞牛统一网关用，如 $TRIM_APPDEST/app.sock）")
    ap.add_argument("--auth", default=None, help="HTTP Basic 认证，格式 user:pass；不给则不认证")
    ap.add_argument("--redact", action="store_true", help="隐藏序列号等敏感字段")
    ap.add_argument("--interval", type=int, default=30, help="采样间隔秒")
    ap.add_argument("--no-sample", action="store_true", help="只读缓存，不主动采样")
    ap.add_argument("--no-gateway-trust", action="store_true",
                    help="不信任飞牛统一网关的 X-Trim-Userid 登录态（仍要 Basic 密码）")
    ap.add_argument("--debug", action="store_true", help="把 HTTP 请求写进 data/requests.log")
    args = ap.parse_args()

    STATE["interval"] = args.interval
    STATE["mode"] = args.mode
    CFG["auth"] = args.auth
    CFG["redact"] = args.redact
    CFG["trust_gateway"] = not args.no_gateway_trust
    CFG["debug"] = args.debug
    CFG["logfile"] = str(DATA / "requests.log")
    if args.mode == "ssh" and not args.remote:
        ap.error("--mode ssh 需要 --remote，例如 --remote 'ssh root@nas.local {cmd}'")
    RUNNER = Runner(args.mode, args.remote)
    restore()

    if not args.no_sample:
        threading.Thread(target=sampler, args=(RUNNER,), daemon=True).start()

    listeners = []

    # 飞牛统一网关（/app/<appname>/）通过 Unix Domain Socket 转发，不占端口
    if args.sock:
        if UnixHTTPServer is None:
            ap.error("当前平台不支持 Unix Domain Socket，请去掉 --sock 改用 --port")
        usrv = UnixHTTPServer(args.sock, Handler)
        threading.Thread(target=usrv.serve_forever, daemon=True).start()
        listeners.append("unix://%s" % args.sock)

    if args.port:
        srv = ThreadingHTTPServer((args.bind, args.port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        listeners.append("http://%s:%d" % (args.bind, args.port))

    if not listeners:
        ap.error("--port 与 --sock 至少要有一个（--port 0 表示只走 socket）")

    print("NAS Dash  %s   mode=%s interval=%ds auth=%s"
          % ("  ".join(listeners), args.mode, args.interval, "on" if args.auth else "off"))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
