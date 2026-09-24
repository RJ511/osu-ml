"""Janela de progresso (barra + ETA) para tarefas longas, local ou num pod por SSH. Só lê; não altera nada.

Lê um `progress.json` com: {"label", "status": running|done|error, "done", "total", "unit", "started_at",
"updated_at", "computed_now"} (segundos Unix). Exemplos:

  python scripts/progress_window.py --file data/processed/catalog/v1/progress_all.json
  python scripts/progress_window.py --ssh root@<ip-do-pod> --ssh-port <porta> --ssh-key ~/.ssh/pod \
      --remote-progress /root/work/out/v1/progress_all.json --remote-log /root/work/run.log

Abre http://127.0.0.1:<porta>/ (só localhost).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html><html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Progresso</title><style>
:root{--bg:#f5f2ea;--surface:#fff;--surface2:#ece6d6;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;--good:#2f8f57;--bad:#c14545;--warn:#b9791f;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--surface2:#26242f;--border:#383642;--text:#eeecf5;--dim:#a29dbb;--accent:#e483ac;--good:#4fbf80;--bad:#e2726f;--warn:#dba13f;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,sans-serif;padding:24px;display:flex;justify-content:center}
.card{width:min(760px,100%);background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--dim);font-size:13px}.pct{font:700 44px ui-monospace,monospace;margin:14px 0 6px}
.bar{height:22px;border-radius:99px;background:var(--surface2);overflow:hidden;border:1px solid var(--border)}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .6s ease}.bar.done i{background:var(--good)}.bar.error i{background:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}.grid span{font-size:12px;color:var(--dim);display:block}.grid b{font:600 18px ui-monospace,monospace}
.pill{display:inline-block;font:600 12px ui-monospace,monospace;padding:3px 10px;border-radius:99px;background:var(--surface2);margin-left:8px}
.pill.running{color:var(--accent)}.pill.done{color:var(--good)}.pill.error,.pill.stale{color:var(--bad)}
pre{background:var(--surface2);border-radius:8px;padding:10px;font-size:12px;overflow:auto;max-height:140px;margin:10px 0 0;white-space:pre-wrap}
</style></head><body><div class="card">
<h1><span id="label">Progresso</span><span class="pill" id="pill">a ligar…</span></h1><div class="sub" id="sub"></div>
<div class="pct" id="pct">—</div><div class="bar" id="bar"><i id="fill"></i></div>
<div class="grid"><div><span>Feito</span><b id="done">—</b></div><div><span>Velocidade</span><b id="rate">—</b></div>
<div><span>Tempo decorrido</span><b id="elapsed">—</b></div><div><span>Tempo restante (est.)</span><b id="eta">—</b></div></div>
<div class="sub" id="upd"></div><pre id="log" hidden></pre></div>
<script>
const $=id=>document.getElementById(id);
const dur=s=>{if(!isFinite(s)||s<0)return"—";s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h} h ${m} min`:m?`${m} min ${x} s`:`${x} s`};
const n=v=>Number(v).toLocaleString("pt-PT");
async function tick(){
 let s;try{s=await(await fetch("/state")).json()}catch(e){$("pill").textContent="sem ligação";$("pill").className="pill error";return}
 const p=s.progress;
 if(!p){$("pill").textContent=s.error?"erro":"a preparar…";$("pill").className="pill "+(s.error?"error":"");$("sub").textContent=s.error||"à espera do primeiro progresso (instalação/arranque)";
  if(s.log){$("log").hidden=false;$("log").textContent=s.log}return}
 $("label").textContent=p.label||"Progresso";
 const total=p.total||0,done=p.done||0,frac=total?Math.min(1,done/total):0,now=Date.now()/1000;
 const age=now-(p.updated_at||now),stale=p.status==="running"&&age>90;
 const status=stale?"stale":(p.status||"running");
 $("pill").textContent=stale?"sem atualizações":status==="done"?"concluído":status==="error"?"erro":"em curso";$("pill").className="pill "+status;
 $("bar").className="bar "+(p.status==="done"?"done":p.status==="error"?"error":"");$("fill").style.width=(frac*100)+"%";
 $("pct").textContent=(frac*100).toFixed(1)+" %";
 $("done").textContent=`${n(done)} / ${n(total)} ${p.unit||""}`;
 const end=p.status==="done"?p.updated_at:now,el=(end||now)-(p.started_at||now),cn=p.computed_now||0;
 const rate=el>0&&cn>0?cn/el:0;$("rate").textContent=rate?`${rate.toFixed(1)} ${p.unit||""}/s`:"—";
 $("elapsed").textContent=dur(el);$("eta").textContent=p.status==="done"?"0 s":rate?dur((total-done)/rate):"—";
 $("upd").textContent=`Último progresso há ${dur(age)}${s.source?" · "+s.source:""}`;
 if(s.log){$("log").hidden=false;$("log").textContent=s.log}
}
tick();setInterval(tick,2000);
</script></body></html>"""


def _native_path(path: str) -> str:
    """`~/x` ou `/c/Users/x` (caminho do Git Bash) -> caminho que o `ssh.exe` do Windows entende."""
    path = os.path.expanduser(path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == "/" and path[1].isalpha():
        path = f"{path[1].upper()}:{path[2:]}"
    return path


class Source:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args, self._cache, self._at = args, {}, 0.0

    def _local(self) -> dict:
        try:
            with open(self.args.file, encoding="utf-8") as fh:
                return {"progress": json.load(fh), "source": os.path.basename(self.args.file)}
        except FileNotFoundError:
            return {"progress": None, "error": None}
        except ValueError:
            return {"progress": None, "error": "progress.json ainda incompleto"}

    def _remote(self) -> dict:
        a = self.args
        cmd = ["ssh", "-F", "/dev/null", "-p", str(a.ssh_port), "-i", _native_path(a.ssh_key), "-o", "IdentitiesOnly=yes",
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
               "-o", "UserKnownHostsFile=/dev/null", a.ssh,
               f"cat {a.remote_progress} 2>/dev/null; echo; echo '---LOG---'; tail -n 4 {a.remote_log} 2>/dev/null"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        except subprocess.TimeoutExpired:
            return {"progress": None, "error": "SSH sem resposta"}
        except FileNotFoundError:
            return {"progress": None, "error": "comando ssh não encontrado"}
        out = res.stdout
        if res.returncode != 0 and "---LOG---" not in out:
            err = [l for l in res.stderr.splitlines() if l.strip() and "post-quantum" not in l and "store now" not in l
                   and "server may need" not in l and "Permanently added" not in l]
            return {"progress": None, "error": "SSH: " + (err[-1] if err else f"código {res.returncode}")}
        head, _, log = out.partition("---LOG---")
        prog = None
        try:
            prog = json.loads(head.strip()) if head.strip() else None
        except ValueError:
            pass
        return {"progress": prog, "log": log.strip() or None, "source": f"{a.ssh}:{a.ssh_port}", "error": None}

    def get(self) -> dict:
        if time.time() - self._at > 2.5:
            self._cache, self._at = (self._remote() if self.args.ssh else self._local()), time.time()
        return self._cache


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="progress.json local")
    ap.add_argument("--ssh", help="utilizador@host do pod")
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--ssh-key", default="~/.ssh/id_ed25519")
    ap.add_argument("--remote-progress")
    ap.add_argument("--remote-log", default="/root/work/run.log")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    if not args.file and not (args.ssh and args.remote_progress):
        ap.error("indica --file, ou --ssh com --remote-progress")
    src = Source(args)
    hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("Host", "") not in hosts:
                self.send_response(403); self.end_headers(); return
            if self.path == "/state":
                body, ctype = json.dumps(src.get()).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    print(f"Janela de progresso em http://127.0.0.1:{args.port}/  (Ctrl+C para sair)", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
