#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""照片排版打印系统 - 本地打印服务（仅用 Python 标准库）

启动后浏览器打开 http://127.0.0.1:8791
功能：
  GET  /              排版界面
  GET  /api/printers  列出系统打印机（lpstat）
  GET  /api/options   列出某打印机支持的纸张规格/介质类型等选项（lpoptions）
  POST /api/print     把前端排版好的 PDF 用 lp 直接发给打印机
  POST /api/open      把 PDF 存到临时文件并用「预览」打开（手动打印备用）
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import time as _time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8791   # 8787 已被本机「大模型用量」服务占用，改用 8791
ROOT = os.path.dirname(os.path.abspath(__file__))


def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return -1, "", str(e)


def list_printers():
    # lpstat -e 输出与语言无关：每行一个打印机名
    _, out, _ = run(["lpstat", "-e"])
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # 设备 URI：过滤掉 pdfwriter 之类的虚拟打印机（如 BeiKe-Print）
    _, vout, _ = run(["lpstat", "-v"])
    devices = {}
    for line in vout.splitlines():
        m = re.match(r"device for (\S+):\s*(\S+)", line)
        if not m:
            m = re.match(r"用于(\S+)的设备[：:]\s*(\S+)", line)
        if m:
            devices[m.group(1)] = m.group(2)
    real = [n for n in names
            if not devices.get(n, "").lower().startswith("pdfwriter")]
    # 状态描述按中英文两种格式解析
    _, out, _ = run(["lpstat", "-p"])
    states = {}
    for line in out.splitlines():
        m = re.match(r"printer\s+(\S+)\s+(.*)", line)
        if not m:
            m = re.match(r"打印机\s*(\S+?)\s*(闲置|停用|正在.*|已暂停.*|拒绝.*)$", line)
        if m:
            states[m.group(1)] = m.group(2).strip()
    printers = [{"name": n, "state": states.get(n, "")} for n in real]
    _, out, _ = run(["lpstat", "-d"])
    default = None
    m = re.search(r"system default destination:\s*(\S+)", out)
    if not m:
        m = re.search(r"系统默认目的位置[：:]\s*(\S+)", out)
    if m:
        default = m.group(1)
    # 系统默认若是被过滤的虚拟打印机，回退到惠普（或第一台真打印机）
    if default not in real:
        hp = next((n for n in real if re.search(r"hp|惠普", n, re.I)), None)
        default = hp or (real[0] if real else None)
    return {"printers": printers, "default": default}


def printer_options(printer):
    """解析 lpoptions -l，返回每个可选组：名称/显示名/取值列表/当前默认值"""
    rc, out, _ = run(["lpoptions", "-p", printer, "-l"])
    groups = []
    if rc == 0:
        for line in out.splitlines():
            if ":" not in line:
                continue
            head, body = line.split(":", 1)
            m = re.match(r"([\w.-]+)/(.*)", head.strip())
            if not m:
                continue
            choices, current = [], None
            for tok in body.split():
                if tok.startswith("*"):
                    current = tok[1:]
                choices.append(tok.lstrip("*"))
            groups.append({"name": m.group(1), "text": m.group(2),
                           "choices": choices, "current": current})
    return groups


def options_payload(printer):
    groups = printer_options(printer)

    def find_group(pattern):
        rx = re.compile(pattern, re.I)
        return next((g for g in groups if rx.search(g["name"]) or rx.search(g["text"])), None)

    ps = find_group(r"pagesize|media.?size|页面|纸张尺寸|media") or {}
    mt = find_group(r"mediatype|media.?type|paper.?type|介质类型|纸张类型|媒体类型") or {}
    bl = find_group(r"borderless|无边框|无边界") or {}
    return {"groups": groups,
            "pagesize": {"name": ps.get("name"), "choices": ps.get("choices", []),
                         "current": ps.get("current")},
            "mediatype": {"name": mt.get("name"), "choices": mt.get("choices", []),
                          "current": mt.get("current")},
            "borderless": {"name": bl.get("name"), "choices": bl.get("choices", []),
                           "current": bl.get("current")}}


def handle_print(data):
    printer = (data.get("printer") or "").strip()
    if not printer:
        printer = list_printers().get("default") or ""
    if not printer:
        return {"ok": False, "error": "未找到可用打印机，请先在 系统设置 → 打印机与扫描仪 中添加"}

    try:
        copies = max(1, int(data.get("copies") or 1))
    except ValueError:
        copies = 1
    media = (data.get("media") or "").strip()          # 纸张规格，如 4x6 / om4x6 / Custom.102x152mm
    mediatype = (data.get("mediatype") or "").strip()  # 纸张类型，如 Photo（照片纸）
    borderless = (data.get("borderless") or "").strip()
    mt_name = (data.get("mediatype_name") or "MediaType").strip()
    bl_name = (data.get("borderless_name") or "Borderless").strip()
    quality = bool(data.get("quality"))
    fit = data.get("fit", True)
    try:
        pdf = base64.b64decode(data.get("pdf") or "")
    except Exception:
        pdf = b""
    if not pdf:
        return {"ok": False, "error": "未收到打印内容"}

    fd, path = tempfile.mkstemp(prefix="photo_print_", suffix=".pdf")
    with os.fdopen(fd, "wb") as f:
        f.write(pdf)

    title = (data.get("title") or "照片排版打印").strip().replace("\n", " ")[:80] or "照片排版打印"
    cmd = ["lp", "-d", printer, "-n", str(copies), "-t", title]
    if data.get("hold"):
        cmd += ["-o", "job-hold-until=indefinite"]   # 仅测试用：任务保持挂起不打印
    if media:
        cmd += ["-o", "media=" + media]
    if mediatype:
        cmd += ["-o", "%s=%s" % (mt_name, mediatype)]
    if borderless:
        cmd += ["-o", "%s=%s" % (bl_name, borderless)]
    if quality:
        cmd += ["-o", "print-quality=5"]
    if fit:
        cmd += ["-o", "fit-to-page"]
    if copies > 1:
        cmd += ["-o", "Collate=True"]
    cmd.append(path)

    rc, out, err = run(cmd, timeout=60)
    msg = (out or err).strip()
    if rc == 0:
        jid = record_job_label(printer, msg, title)
        return {"ok": True, "message": "已发送到打印机「%s」（任务 #%s）：%s" % (printer, jid or "?", msg),
                "job": jid, "cmd": " ".join(cmd), "file": path}
    return {"ok": False, "error": "打印命令失败：%s（命令：%s）" % (msg, " ".join(cmd))}


LABELS_PATH = os.path.join(ROOT, "job_labels.json")

def load_labels():
    try:
        with open(LABELS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_labels(d):
    try:
        with open(LABELS_PATH, "w") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass

def record_job_label(printer, lp_output, title):
    """从 lp 输出解析任务号，记录 任务号→文件名 映射"""
    m = re.search(re.escape(printer) + r"-(\d+)", lp_output)
    if not m or not title:
        return None
    labels = load_labels()
    labels[m.group(1)] = title[:80]
    save_labels(labels)
    return m.group(1)


GEO_CACHE = {}

def geocode_place(lat, lon):
    """GPS 坐标 → 地名（OpenStreetMap 逆地理编码，无密钥）。
    有地标名返回「区·地标」，否则返回区/县级地名；失败返回空串。"""
    key = (round(lat, 3), round(lon, 3))   # ~110m 精度缓存
    if key in GEO_CACHE:
        return GEO_CACHE[key]
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon,
                                "zoom": 17, "accept-language": "zh"})
    url = "https://nominatim.openstreetmap.org/reverse?" + q
    req = urllib.request.Request(url, headers={
        "User-Agent": "photo-layout-printer/1.0 (personal local tool)"})
    place = ""
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        addr = data.get("address", {})
        display = [p.strip() for p in data.get("display_name", "").split(",")]

        def short(s, suffixes):
            s = (s or "").strip()
            for suf in suffixes:
                if s.endswith(suf) and len(s) > len(suf):
                    return s[:-len(suf)]
            return s

        # 省级：优先 address.state；直辖市（北京/上海等）从 display_name 里找「xx市」
        prov = short(addr.get("state"), ("省", "自治区", "市"))
        if not prov:
            for p in display:
                if p.endswith("市") and len(p) >= 3:
                    prov = short(p, ("市",))
                    break
        dist = short(addr.get("city") or addr.get("town") or addr.get("county"),
                     ("市", "区", "县", "旗"))
        town = short(addr.get("town") or addr.get("suburb") or addr.get("city_district")
                     or addr.get("quarter"), ("镇", "乡", "街道", "社区"))
        poi = data.get("name") or addr.get("park") or ""
        if poi and not re.search(r"[\u4e00-\u9fff]", poi):
            poi = ""     # OSM 里的拉丁名（如 Dufuchi Hutong）对中文打印没用
        parts = []
        for p in (prov, dist, town):
            if p and (not parts or parts[-1] != p):
                parts.append(p)
        if poi and poi not in "/".join(parts) and len(parts) < 3:
            parts.append(poi)   # 没有镇级信息时用地标名补足
        place = "·".join(parts).strip("·")[:60]
    except Exception as e:
        place = ""
        GEO_CACHE["__err__" + str(key)] = str(e)[:80]
    GEO_CACHE[key] = place
    return place


def queue_jobs(printer):
    """列出某打印机当前未完成的任务，附文件名与状态"""
    cmd = ["lpstat", "-o"] + ([printer] if printer else [])
    _, out, _ = run(cmd)
    _, lout, _ = run(["lpstat", "-l", "-o"] + ([printer] if printer else []))
    # lpstat -l 的每个任务块（含 警报：job-printing = 正在打印）
    blocks, cur = {}, None
    for line in lout.splitlines():
        m2 = re.match(r"(\S+)-(\d+)\s+\S+\s+\d+\s+", line)
        if m2:
            cur = m2.group(2)
            blocks[cur] = line + "\n"
        elif cur is not None and line.startswith("\t"):
            blocks[cur] += line + "\n"
    labels = load_labels()
    jobs, pending_ids = [], []
    for line in out.splitlines():
        m = re.match(r"(\S+)-(\d+)\s+(\S+)\s+(\d+)\s+(.*)", line)
        if not m:
            continue
        jid = m.group(2)
        pending_ids.append(jid)
        elapsed = None
        try:
            ts = datetime.strptime(m.group(5).strip(), "%a %b %d %H:%M:%S %Y")
            elapsed = max(0, int(_time.time() - ts.timestamp()))
        except Exception:
            pass
        jobs.append({"printer": m.group(1), "job": int(jid),
                     "name": labels.get(jid, ""),
                     "state": "打印中" if "job-printing" in blocks.get(jid, "") else "排队中",
                     "user": m.group(3), "size_kb": max(1, round(int(m.group(4)) / 1024)),
                     "time": m.group(5).strip(), "elapsed_s": elapsed})
    # 只保留仍在队列中的映射
    if labels:
        save_labels({k: v for k, v in labels.items() if k in pending_ids})
    # 按打印顺序（任务号升序）标注序号：第 1 个 = 下一个打印
    jobs.sort(key=lambda j: j["job"])
    for i, j in enumerate(jobs):
        j["order"] = i + 1
    return {"jobs": jobs}


def cancel_print_jobs(printer, job=None):
    """取消任务：job 为空时取消该打印机全部任务（cancel -a）"""
    if job:
        rc, out, err = run(["cancel", str(job)], timeout=15)
    else:
        rc, out, err = run(["cancel", "-a", printer], timeout=15)
    msg = (out or err).strip()
    if rc == 0:
        return {"ok": True, "message": msg or ("已取消任务 #%s" % job if job else "已取消全部任务")}
    return {"ok": False, "error": msg or "取消命令执行失败"}


def handle_open(data):
    try:
        pdf = base64.b64decode(data.get("pdf") or "")
    except Exception:
        pdf = b""
    if not pdf:
        return {"ok": False, "error": "未收到内容"}
    fd, path = tempfile.mkstemp(prefix="photo_layout_", suffix=".pdf")
    with os.fdopen(fd, "wb") as f:
        f.write(pdf)
    rc, out, err = run(["open", path])
    if rc == 0:
        return {"ok": True, "message": "已在「预览」中打开：" + path, "path": path}
    return {"ok": False, "error": (out or err).strip()}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as e:
                self._send(500, json.dumps({"error": str(e)}).encode())
        elif self.path == "/api/printers":
            self._send(200, json.dumps(list_printers()).encode())
        elif self.path.startswith("/api/geocode"):
            q = parse_qs(urlparse(self.path).query)
            try:
                lat = float(q.get("lat", ["0"])[0]); lon = float(q.get("lon", ["0"])[0])
                self._send(200, json.dumps({"place": geocode_place(lat, lon)}).encode())
            except Exception as e:
                self._send(200, json.dumps({"place": "", "error": str(e)[:80]}).encode())
        elif self.path.startswith("/api/queue"):
            q = parse_qs(urlparse(self.path).query)
            p = (q.get("printer") or [""])[0]
            self._send(200, json.dumps(queue_jobs(p)).encode())
        elif self.path.startswith("/api/options"):
            q = parse_qs(urlparse(self.path).query)
            p = (q.get("printer") or [""])[0]
            self._send(200, json.dumps(options_payload(p) if p else
                                       {"groups": [], "pagesize": {}, "mediatype": {}, "borderless": {}}).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n))
        except Exception:
            return self._send(400, b'{"ok":false,"error":"bad request"}')
        if self.path == "/api/print":
            self._send(200, json.dumps(handle_print(data)).encode())
        elif self.path == "/api/cancel":
            self._send(200, json.dumps(cancel_print_jobs(
                (data.get("printer") or "").strip(),
                data.get("job"))).encode())
        elif self.path == "/api/open":
            self._send(200, json.dumps(handle_open(data)).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *a):
        pass


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("端口 %d 已被占用，服务可能已在运行，直接打开浏览器即可：" % PORT)
        print("  http://127.0.0.1:%d" % PORT)
        return
    print("照片排版打印服务已启动：http://127.0.0.1:%d  （Ctrl+C 退出）" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
