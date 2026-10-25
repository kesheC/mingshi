#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""明势 Míngshì · 启动器。

用法：
    python run.py                 # 启动服务并打开浏览器（默认 127.0.0.1:8765）
    python run.py --no-open       # 只启动服务
    python run.py --port 9000     # 换端口
    python run.py report -o r.md  # 直接导出当前研判为 Markdown
    python run.py doctor          # 体检：数据源可达性 + 依赖 + 缓存

零第三方依赖：只要能跑 Python 3.10+，解压就能用。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认是 GBK，直接打印中文与制表符会变成乱码。切到 UTF-8 代码页，
# 让 `python run.py` 的输出在任何终端里都可读（失败也不影响主流程）。
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

from app import store                       # noqa: E402
from app.api import get_payload, serve      # noqa: E402
from app.engine import VERSION, analyze     # noqa: E402
from app.report import render_markdown      # noqa: E402


def _banner(host: str, port: int) -> str:
    url = f"http://{host}:{port}"
    line = "═" * 46
    return (f"\n{line}\n"
            f"  明势 Míngshì  v{VERSION}\n"
            f"  A 股市场大势研判 · 状态描述，不预测涨跌\n"
            f"{line}\n"
            f"  服务地址   {url}\n"
            f"  数据快照   {len(store.list_snapshots())} 份\n"
            f"  按 Ctrl+C 停止\n"
            f"{line}\n")


def cmd_serve(args) -> int:
    httpd = serve(args.host, args.port)
    print(_banner(args.host, args.port), flush=True)

    if not args.no_open:
        def _open() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(f"http://{args.host}:{args.port}")
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    if args.warm:
        def _warm() -> None:
            t0 = time.time()
            try:
                p = get_payload()
                print(f"  [预热完成] {p['as_of']} {p['regime']['name']} "
                      f"{time.time() - t0:.1f}s", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  [预热失败] {exc}", flush=True)
        threading.Thread(target=_warm, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        httpd.server_close()
    return 0


def cmd_report(args) -> int:
    t0 = time.time()
    payload = analyze(save=True)
    md = render_markdown(payload)
    out = args.out or f"mingshi-{payload['as_of']}.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"已生成 {out}  （{len(md)} 字符, {time.time() - t0:.1f}s）")
    print(f"状态：{payload['regime']['name']}　置信度 {payload['regime'].get('confidence')}")
    return 0


def cmd_doctor(args) -> int:
    from app.fetch import collect

    print(f"Python      {sys.version.split()[0]}")
    print(f"工作目录    {os.getcwd()}")
    print(f"快照数      {len(store.list_snapshots())}")
    print("正在探测数据源…")
    t0 = time.time()
    snap = collect()
    print(f"取数耗时    {time.time() - t0:.1f}s")
    print(f"交易日      {snap.as_of}")
    print(f"个股覆盖    {len(snap.market)} 只")
    print(f"成功率      {snap.ok_ratio * 100:.0f}%")
    print("数据源：")
    prov = {k: v for k, v in (snap.provenance or {}).items()
            if k not in ("fallbacks",)}
    for k, v in prov.items():
        print(f"  - {k:20s} {v}")
    if snap.errors:
        print("异常：")
        for e in snap.errors[:10]:
            print(f"  ! {e}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mingshi", description="明势 · A 股市场大势研判")
    ap.add_argument("--version", action="version", version=f"mingshi {VERSION}")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="启动 Web 服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    s.add_argument("--warm", action="store_true", help="启动后台预热一次研判")
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("report", help="导出 Markdown 研判报告")
    r.add_argument("-o", "--out", default=None)
    r.set_defaults(func=cmd_report)

    d = sub.add_parser("doctor", help="数据源体检")
    d.set_defaults(func=cmd_doctor)

    raw = list(sys.argv[1:]) if argv is None else list(argv)
    if not raw:
        raw = ["serve"]
    # 顶层 -h/--version 与子命令无关，不能在前面硬插 "serve"，否则永远走不到。
    if raw[0] in ("-h", "--help", "-v", "--version"):
        return_args = ap.parse_args(raw)
        return 0 if getattr(return_args, "cmd", None) else 0
    if raw[0] not in ("serve", "report", "doctor"):
        raw = ["serve"] + raw          # 允许 `python run.py --port 9000`
    args = ap.parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
