#!/usr/bin/env python3
"""Local-only control panel for the resumable native Mac worker."""

import json
import os
import sqlite3
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).parent.resolve()
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/Volumes/sata11-155XXXX2337/摄像头文件备份/延时摄影"))
PROCESS = None
LOGS = []
LOCK = threading.Lock()


PAGE = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Mac 影像协处理</title><style>
:root{--night:#15221f;--tea:#2d6b57;--cream:#f4f0e7;--orange:#e86b3d;--fog:#b9c7bf}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#e3ede8,#f5f0e6);color:var(--night);font:16px "Avenir Next","PingFang SC",sans-serif}main{max-width:920px;margin:auto;padding:48px 24px}.tag{font:800 11px monospace;color:var(--tea);letter-spacing:.15em}h1{font:700 clamp(42px,7vw,74px)/.9 "Iowan Old Style","Songti SC",serif;margin:12px 0 32px;letter-spacing:-.06em}.grid{display:grid;grid-template-columns:1fr 1.2fr;gap:18px}.card{background:#fffdf8;border:1px solid #c7c4b9;box-shadow:7px 8px 0 #25453a18;padding:24px}.status{background:var(--night);color:#f9f4e9}.status strong{font:700 30px "Iowan Old Style",serif}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#7b8b82;margin-right:8px}.dot.run{background:#f1bd5f;box-shadow:0 0 0 5px #f1bd5f33}label{display:grid;gap:7px;margin:13px 0;font-size:13px;font-weight:700;color:#52625a}input{padding:12px;border:1px solid #c7c4b9;font:inherit;color:var(--night)}button{padding:13px 16px;border:0;background:var(--tea);color:white;font:800 15px inherit;cursor:pointer}button.stop{background:var(--orange)}.actions{display:flex;gap:8px;margin-top:20px}pre{height:280px;margin:0;overflow:auto;background:#10201c;color:#cce0d5;padding:16px;font:12px/1.55 ui-monospace,monospace;white-space:pre-wrap}.note{color:#647168;font-size:13px;line-height:1.65}@media(max-width:700px){.grid{grid-template-columns:1fr}main{padding:28px 16px}}</style>
<main><div class=tag>LOCALHOST ONLY · MPS / METAL WORKER</div><h1>Mac 影像协处理</h1><div class=grid><section class=card><div class=tag>任务设定</div><label>录像日期<select id=date><option>正在读取 NAS 日期索引…</option></select></label><div class=note id=dateHint></div><label>执行批数 <input id=batches type=number min=1 max=50 value=1></label><label><input id=ac type=checkbox checked> 仅在接电时领取下一批</label><div class=actions><button id=start>开始协处理</button><button class=stop id=stop>停止当前批次</button></div><p class=note>每批最多处理 5 个未处理文件。可随时停止；完成结果已写回 NAS，下一次会自动续接。</p></section><section class='card status'><div class=tag style='color:#a9caba'>运行状态</div><p><span id=dot class=dot></span><strong id=status>检查中</strong></p><p id=detail>仅监听 127.0.0.1，不对局域网开放。</p></section><section class=card style='grid-column:1/-1'><div class=tag style='margin-bottom:10px'>实时日志</div><pre id=logs>正在连接本地工作节点…</pre></section></div></main><script>
const $=s=>document.querySelector(s),api=(url,opt)=>fetch(url,opt).then(async r=>{const d=await r.json();if(!r.ok)throw Error(d.error);return d});async function dates(){const d=await api('/api/dates');$('#dateHint').textContent=d.message;$('#date').innerHTML=d.dates.map(x=>`<option value="${x}">${x.slice(0,4)}.${x.slice(4,6)}.${x.slice(6)}</option>`).join('')||'<option value="">没有可用日期</option>'}async function refresh(){try{const s=await api('/api/status');$('#status').textContent=s.running?'正在协处理':'空闲';$('#detail').textContent=s.detail;$('#dot').className='dot '+(s.running?'run':'');$('#logs').textContent=s.logs.join('\\n')||'尚无日志';$('#logs').scrollTop=$('#logs').scrollHeight}catch(e){$('#status').textContent='本地服务异常';$('#detail').textContent=e.message}}$('#start').onclick=async()=>{try{const date=$('#date').value;if(!date)throw Error('请选择录像日期');await api('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date,batches:+$('#batches').value,require_ac:$('#ac').checked})});refresh()}catch(e){alert(e.message)}};$('#stop').onclick=()=>api('/api/stop',{method:'POST'}).then(refresh);dates();refresh();setInterval(refresh,1500)</script></html>"""


def status():
    with LOCK:
        running = PROCESS is not None and PROCESS.poll() is None
        return {"running": running, "detail": "Mac 与 NAS 之间仅通过 SMB 读写。" if running else "可开始新的小批次任务。", "logs": LOGS[-200:]}


def available_dates():
    database = OUTPUT_ROOT / "inventory.sqlite3"
    if not database.exists():
        return {"dates": [], "message": "NAS 尚未建立录像索引，请先打开 NAS 管理页等待索引完成。"}
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2) as connection:
            dates = [row[0] for row in connection.execute("SELECT DISTINCT substr(start,1,8) FROM videos ORDER BY 1 DESC")]
        return {"dates": dates, "message": f"发现 {len(dates)} 个有录像的日期。"}
    except sqlite3.Error as error:
        return {"dates": [], "message": f"无法读取 NAS 索引：{error}"}


def run_worker(date, batches, require_ac):
    global PROCESS
    try:
        with LOCK:
            environment = dict(os.environ, REQUIRE_AC="1" if require_ac else "0")
            PROCESS = subprocess.Popen(["sh", str(ROOT / "mac-worker.sh"), date, str(batches)], cwd=ROOT, env=environment,
                                       text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
        for line in PROCESS.stdout:
            with LOCK: LOGS.append(line.rstrip())
    finally:
        with LOCK: PROCESS = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def reply(self, value, status=HTTPStatus.OK):
        data=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json;charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.path=='/api/status': return self.reply(status())
        if self.path=='/api/dates': return self.reply(available_dates())
        self.send_response(HTTPStatus.OK);self.send_header('Content-Type','text/html;charset=utf-8');self.end_headers();self.wfile.write(PAGE.encode())
    def do_POST(self):
        global PROCESS
        if self.path=='/api/stop':
            with LOCK:
                if PROCESS and PROCESS.poll() is None: PROCESS.terminate(); LOGS.append('已请求停止当前批次。')
            return self.reply(status())
        if self.path!='/api/start': return self.reply({'error':'not found'},HTTPStatus.NOT_FOUND)
        try:
            data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))));date=data['date'];batches=int(data.get('batches',1));require_ac=bool(data.get('require_ac',True))
            if not (date.isdigit() and len(date)==8 and 1<=batches<=50): raise ValueError('日期或批次数无效')
            with LOCK:
                if PROCESS and PROCESS.poll() is None: raise ValueError('已有任务正在运行')
                LOGS.clear();LOGS.append(f'准备处理 {date}，共 {batches} 批。')
            threading.Thread(target=run_worker,args=(date,batches,require_ac),daemon=True).start();return self.reply(status())
        except (ValueError,json.JSONDecodeError) as error: return self.reply({'error':str(error)},HTTPStatus.BAD_REQUEST)


if __name__=='__main__':
    print('Mac worker console: http://127.0.0.1:8791',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8791),Handler).serve_forever()
