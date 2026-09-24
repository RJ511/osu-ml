"""Tarefas longas do projeto: descoberta das barras de progresso, lançamento e cancelamento a partir do painel.

- **Descoberta**: qualquer `progress.json` / `*.progress.json` / `progress_*.json` sob `data/processed` aparece no painel,
  mesmo que a tarefa tenha sido lançada de um terminal (formato em `progress.py`: label, status, done, total, unit,
  started_at, updated_at, pid).
- **Lançamento**: só modelos pré-definidos (`TEMPLATES`) — nunca comandos arbitrários. Cada um corre
  `python -m osuml ...` num subprocesso, com o log em `data/logs/jobs/` e o progresso em `data/processed/tasks/`.
- **Cancelamento**: termina o processo (e filhos) de uma tarefa em curso, lançada pelo painel ou por fora
  (o `pid` vem do próprio `progress.json`; só se o processo ainda existir e o progresso for recente).
"""

from __future__ import annotations

import ctypes
import json
from collections import deque
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

STALE_AFTER_S = 120


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    if os.name == "nt":  # nunca os.kill no Windows: termina o processo à força
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=15)
    else:
        os.kill(pid, signal.SIGTERM)


@dataclass
class Template:
    label: str
    description: str
    argv: Callable[["JobManager", str], list[str]]  # (manager, caminho do progresso) -> argumentos depois de `python -m osuml`
    eta: str = ""
    requires: list[str] = field(default_factory=list)  # ficheiros que têm de existir (relativos ao projeto)
    after: list[str] = field(default_factory=list)  # tarefas que têm de NÃO estar em curso (ex.: importações de que depende)


def _perf_tar(kind: str) -> str:
    return f"data/external/performance/2026_09_01_performance_osu_{kind}.tar.bz2"


def default_templates() -> dict[str, Template]:
    def table(kind: str):
        return lambda m, prog: ["dump-table", "--tar", _perf_tar(kind), "--table", "osu_user_beatmap_playcount", "--progress", prog]

    return {
        "dump-playcount-random": Template("Importar tentativas (playcount) — random_10000",
                                          "Tabela osu_user_beatmap_playcount do dump → Parquet (0 pedidos à API).",
                                          table("random_10000"), "~8-10 min", [_perf_tar("random_10000")]),
        "dump-playcount-top": Template("Importar tentativas (playcount) — top_1000",
                                       "Tabela osu_user_beatmap_playcount do dump → Parquet (0 pedidos à API).",
                                       table("top_1000"), "~9-11 min", [_perf_tar("top_1000")]),
        "playcount-check": Template("Testar 'tentativas − passes' como estimativa de fails",
                                    "Cruza playcount com os passes do dump; diz se dá para estimar fails sem a API.",
                                    lambda m, prog: ["analyze", "playcount-check", "--version", m.stamp(), "--progress", prog],
                                    "< 1 min", ["data/processed/dump_scores/v1"],
                                    ["dump-playcount-random", "dump-playcount-top"]),
        "pp-check": Template("Validar o pp local contra o oficial (5 000 scores)",
                             "Compara o pp do rosu-pp com o pp oficial do dump.",
                             lambda m, prog: ["analyze", "pp-check", "--version", m.stamp(), "--progress", prog], "~20 s",
                             ["data/processed/catalog/v1/plan.json"]),
        "acc-baseline": Template("Baseline de accuracy esperada (LightGBM)",
                                 "Treina e avalia com split temporal, hold-out de jogadores e ablações.",
                                 lambda m, prog: ["analyze", "acc-baseline", "--version", m.stamp(), "--progress", prog], "~1 min",
                                 ["data/processed/catalog/v1/map_attributes.parquet"]),
    }


def _native_path(path: str) -> str:
    """`~/x` ou `/c/Users/x` (Git Bash) -> caminho que o `ssh.exe` do Windows entende."""
    path = os.path.expanduser(path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == "/" and path[1].isalpha():
        path = f"{path[1].upper()}:{path[2:]}"
    return path


def ssh_argv(entry: dict[str, Any], remote_cmd: str) -> list[str]:
    return ["ssh", "-F", "/dev/null", "-p", str(entry.get("port", 22)), "-i", _native_path(entry.get("key", "~/.ssh/id_ed25519")),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null", entry["ssh"], remote_cmd]


def _run_ssh(argv: list[str], timeout: float = 25) -> tuple[int, str, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 255, "", "SSH sem resposta"
    except FileNotFoundError:
        return 255, "", "comando ssh não encontrado"


class JobManager:
    def __init__(self, root: Path, processed_dir: Path, logs_dir: Path, *, templates: dict[str, Template] | None = None,
                 python: str | None = None, prefix: list[str] | None = None, now: Callable[[], float] = time.time,
                 remote_file: Path | None = None, ssh_runner: Callable[[list[str]], tuple[int, str, str]] | None = None) -> None:
        self.root, self.processed, self.logs = Path(root), Path(processed_dir), Path(logs_dir) / "jobs"
        self.templates = templates if templates is not None else default_templates()
        self.python, self._now = python or sys.executable, now
        self.prefix = prefix  # por omissão `python -m osuml`; os testes trocam-no por um script mínimo
        self.owned: dict[str, dict[str, Any]] = {}  # template -> {popen, log, started, progress, key}
        # tarefas noutra máquina (pod RunPod): `data/control/remote_tasks.json` = lista de {name, label, ssh, port, key, progress, log}
        self.remote_file = Path(remote_file) if remote_file else Path(root) / "data" / "control" / "remote_tasks.json"
        self._ssh = ssh_runner or (lambda argv: _run_ssh(argv))
        self._remote_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._hist: dict[str, deque] = {}  # key -> amostras (tempo, feito) recentes, para a velocidade instantânea

    # ---------------------------------------------------------------- utilidades
    def stamp(self) -> str:
        return "p" + datetime.now().strftime("%m%d-%H%M")

    def _progress_path(self, template: str) -> Path:
        return self.processed / "tasks" / f"{template}.progress.json"

    def _rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.processed.resolve()).as_posix()
        except ValueError:
            return p.as_posix()

    def discover(self) -> list[Path]:
        found: set[Path] = set()
        for pat in ("**/progress.json", "**/*.progress.json", "**/progress_*.json"):
            found.update(self.processed.glob(pat))
        return sorted(found)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _tail(path: Path | None, n: int = 6) -> str:
        if not path or not path.exists():
            return ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])[-900:]

    # ------------------------------------------------------------------ tarefas remotas
    def remote_entries(self) -> list[dict[str, Any]]:
        data = self._read(self.remote_file) if self.remote_file.exists() else None
        return [e for e in (data or []) if isinstance(e, dict) and e.get("ssh") and (e.get("progress") or e.get("progress_dir")) and e.get("name")]

    def _recent_rate(self, key: str, done: float, now: float, fallback: float, window: float = 180.0) -> float:
        """Velocidade pelos últimos `window` s (a média desde o início engana quando há uma fase inicial sem passos, p.ex. a ler dados)."""
        h = self._hist.setdefault(key, deque())
        if h and done < h[-1][1]:  # recomeçou
            h.clear()
        if not h or now - h[-1][0] >= 1.0:
            h.append((now, done))
        while len(h) > 2 and now - h[1][0] >= window:
            h.popleft()
        t0, d0 = h[0]
        if now - t0 >= 15 and done > d0:
            return (done - d0) / (now - t0)
        return fallback

    def _remote_dir_tasks(self, e: dict[str, Any]) -> list[dict[str, Any]]:
        """Pod com várias tarefas: UMA ligação SSH lê todos os `*.json` de `progress_dir` (em vez de uma por barra)."""
        now = self._now()
        cached = self._remote_cache.get(e["name"])
        if cached and now - cached[0] < 3:
            return list(cached[1].get("tasks", []))
        d = e["progress_dir"].rstrip("/")
        cmd = f"for f in {d}/*.json; do [ -f \"$f\" ] && echo \"@@FILE $f\" && cat \"$f\" && echo; done; echo '@@LOG'; tail -n 6 {e.get('log', '/dev/null')} 2>/dev/null"
        rc, out, err = self._ssh(ssh_argv(e, cmd))
        if rc != 0 and not out:
            last = list(cached[1].get("tasks", [])) if cached else []
            tasks = [{**t, "unreachable": True} for t in last]
            if not tasks:
                tasks = [{"key": f"remote:{e['name']}:pod", "label": e.get("label", e["name"]), "status": "interrupted", "done": 0, "total": 0, "pct": 0.0,
                          "unit": "", "rate": 0.0, "eta_s": None, "elapsed_s": 0, "age_s": 0, "pid": None, "alive": False, "owned": False,
                          "template": None, "exit_code": None, "started_at": now, "updated_at": now, "remote": e.get("host_label", e["ssh"]),
                          "log": (err.strip().splitlines() or ["sem ligação ao pod"])[-1][:200], "unreachable": True}]
            self._remote_cache[e["name"]] = (now, {"tasks": tasks})
            return tasks
        body, _, log = out.partition("@@LOG")
        tasks = []
        for block in body.split("@@FILE ")[1:]:
            path, _, raw = block.partition(chr(10))
            try:
                data = json.loads(raw.strip())
            except ValueError:
                continue
            stem = path.strip().rsplit("/", 1)[-1].removesuffix(".json")
            t = self._remote_build({**e, "name": f"{e['name']}:{stem}", "label": e.get("label", e["name"])}, data, log if stem.startswith("00_") else "", now)
            tasks.append(t)
        self._remote_cache[e["name"]] = (now, {"tasks": tasks})
        return tasks

    def _remote_task(self, e: dict[str, Any]) -> dict[str, Any] | None:
        now = self._now()
        cached = self._remote_cache.get(e["name"])
        if cached and now - cached[0] < 3:
            return cached[1] or None
        log_cmd = f"tail -n 5 {e['log']} 2>/dev/null" if e.get("log") else "true"
        rc, out, err = self._ssh(ssh_argv(e, f"cat {e['progress']} 2>/dev/null; echo; echo '---LOG---'; {log_cmd}"))
        head, _, log = out.partition("---LOG---")
        data = None
        try:
            data = json.loads(head.strip()) if head.strip() else None
        except ValueError:
            pass
        if data is None:
            last = cached[1] if cached else None
            if last:
                task = {**last, "unreachable": True}
            elif rc != 0:
                task = {"key": f"remote:{e['name']}", "label": e.get("label", e["name"]), "status": "interrupted", "done": 0, "total": 0, "pct": 0.0,
                        "unit": "", "rate": 0.0, "eta_s": None, "elapsed_s": 0, "age_s": 0, "pid": None, "alive": False, "owned": False,
                        "template": None, "exit_code": None, "started_at": now, "updated_at": now, "remote": e.get("host_label", e["ssh"]),
                        "log": (err.strip().splitlines() or ["sem ligação ao pod"])[-1][:200], "unreachable": True}
            else:
                task = None
            self._remote_cache[e["name"]] = (now, task or {})
            return task
        task = self._remote_build(e, data, log, now)
        self._remote_cache[e["name"]] = (now, task)
        return task

    def _remote_build(self, e: dict[str, Any], data: dict[str, Any], log: str, now: float) -> dict[str, Any]:
        status = data.get("status", "running")
        age = now - float(data.get("updated_at") or now)
        if status == "running" and age > STALE_AFTER_S:
            status = "interrupted"
        total, done = float(data.get("total") or 0), float(data.get("done") or 0)
        started = float(data.get("started_at") or now)
        end = float(data.get("updated_at") or now) if status != "running" else now
        rate = (float(data.get("computed_now") or 0) / (end - started)) if end > started and data.get("computed_now") else 0.0
        if status == "running":
            rate = self._recent_rate(f"remote:{e['name']}", done, now, rate)
        task = {"key": f"remote:{e['name']}", "label": f"{e.get('label', e['name'])} — {data.get('label', '')}".strip(" —"), "status": status,
                "done": done, "total": total, "pct": round(min(1.0, done / total) * 100, 1) if total else 0.0, "unit": data.get("unit", ""),
                "rate": round(rate, 2), "eta_s": round((total - done) / rate) if status == "running" and rate > 0 and total else None,
                "elapsed_s": round(end - started), "age_s": round(age), "pid": data.get("pid"), "alive": status == "running", "owned": False,
                "template": None, "exit_code": None, "started_at": started, "updated_at": data.get("updated_at"),
                "remote": e.get("host_label", e["ssh"]), "log": log.strip()[-900:]}
        return task

    def cancel_remote(self, name: str) -> dict[str, Any]:
        base = name.split(":", 1)[0]
        e = next((x for x in self.remote_entries() if x["name"] == base), None)
        if ":" in name:
            cached = next((t for t in self._remote_cache.get(base, (0, {}))[1].get("tasks", []) if t["key"] == f"remote:{name}"), None) or {}
        else:
            cached = self._remote_cache.get(name, (0, {}))[1]
        pid = cached.get("pid") if cached else None
        if e is None or not pid:
            return {"error": "tarefa remota não encontrada"}
        rc, out, err = self._ssh(ssh_argv(e, f"kill -TERM -- -$(ps -o pgid= -p {int(pid)} | tr -d ' ') 2>/dev/null || kill -TERM {int(pid)}"))
        self._remote_cache.pop(base, None)
        return {"ok": True} if rc == 0 else {"error": (err.strip() or "falhou o cancelamento")[:200]}

    # -------------------------------------------------------------------- estado
    def _task(self, path: Path, data: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        status = data.get("status", "running")
        age = now - float(data.get("updated_at") or now)
        pid = data.get("pid")
        alive = pid_alive(pid)
        owner = next((t for t, o in self.owned.items() if o["progress"].resolve() == path.resolve()), None)
        exit_code = None
        if owner:
            exit_code = self.owned[owner]["popen"].poll()
        if status == "running" and exit_code is not None:
            status = "error" if exit_code != 0 else "done"
        elif status == "running" and age > STALE_AFTER_S and not alive:
            status = "interrupted"
        total, done = float(data.get("total") or 0), float(data.get("done") or 0)
        started = float(data.get("started_at") or now)
        end = float(data.get("updated_at") or now) if status in ("done", "error", "interrupted") else now
        rate = (float(data.get("computed_now") or 0) / (end - started)) if end > started and data.get("computed_now") else 0.0
        if status == "running":
            rate = self._recent_rate(self._rel(path), done, now, rate)
        return {"key": self._rel(path), "label": data.get("label", path.stem), "status": status, "done": done, "total": total,
                "pct": round(min(1.0, done / total) * 100, 1) if total else 0.0, "unit": data.get("unit", ""), "rate": round(rate, 2),
                "eta_s": round((total - done) / rate) if status == "running" and rate > 0 and total else None,
                "elapsed_s": round(end - started), "age_s": round(age), "pid": pid, "alive": alive, "owned": owner is not None,
                "template": owner, "exit_code": exit_code, "started_at": started, "updated_at": data.get("updated_at"),
                "log": self._tail(self.owned[owner]["log"]) if owner else ""}

    def state(self, recent_hours: float = 36) -> dict[str, Any]:
        now, tasks = self._now(), []
        for path in self.discover():
            data = self._read(path)
            if data is None or "total" not in data:
                continue
            t = self._task(path, data)
            if t["status"] == "running" or now - float(t["updated_at"] or 0) < recent_hours * 3600:
                tasks.append(t)
        for e in self.remote_entries():
            got = self._remote_dir_tasks(e) if e.get("progress_dir") else [self._remote_task(e)]
            for t in got:
                if t and (t["status"] == "running" or now - float(t["updated_at"] or 0) < recent_hours * 3600 or t.get("unreachable")):
                    tasks.append(t)
        order = {"running": 0, "error": 1, "interrupted": 2, "done": 3}
        tasks.sort(key=lambda t: (order.get(t["status"], 9), -(t["updated_at"] or 0)))
        running_templates = {t["template"] for t in tasks if t["status"] == "running" and t["template"]}
        running_keys = {t["key"] for t in tasks if t["status"] == "running"}
        templates = []
        for name, tp in self.templates.items():
            missing = [r for r in tp.requires if not (self.root / r).exists()]
            key = self._rel(self._progress_path(name))
            waiting = [a for a in tp.after if self._rel(self._progress_path(a)) in running_keys or a in running_templates]
            templates.append({"name": name, "label": tp.label, "description": tp.description, "eta": tp.eta,
                              "running": name in running_templates or key in running_keys, "missing": missing,
                              "blocked_by": waiting})
        return {"now": now, "tasks": tasks, "templates": templates, "running": sum(1 for t in tasks if t["status"] == "running")}

    # ------------------------------------------------------------------- ações
    def start(self, name: str) -> dict[str, Any]:
        tp = self.templates.get(name)
        if tp is None:
            return {"error": "tarefa desconhecida"}
        missing = [r for r in tp.requires if not (self.root / r).exists()]
        if missing:
            return {"error": f"falta {missing[0]}"}
        for a in tp.after:
            other = self._read(self._progress_path(a))
            if other and other.get("status") == "running" and pid_alive(other.get("pid")) and self._now() - float(other.get("updated_at") or 0) < STALE_AFTER_S:
                return {"error": f"espera que '{self.templates[a].label}' termine"}
        prog = self._progress_path(name)
        old = self._read(prog)
        if old and old.get("status") == "running" and pid_alive(old.get("pid")) and self._now() - float(old.get("updated_at") or 0) < STALE_AFTER_S:
            return {"error": "esta tarefa já está em curso"}
        prog.parent.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        log = self.logs / f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        rel_prog = os.path.relpath(prog, self.root) if str(prog).startswith(str(self.root)) else str(prog)
        argv = [*(self.prefix or [self.python, "-m", "osuml"]), *tp.argv(self, rel_prog)]
        kw: dict[str, Any] = {"cwd": str(self.root), "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        fh = log.open("wb")
        popen = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, **kw)
        self.owned[name] = {"popen": popen, "log": log, "started": self._now(), "progress": prog}
        return {"ok": True, "pid": popen.pid}

    def cancel(self, key: str) -> dict[str, Any]:
        if key.startswith("remote:"):
            return self.cancel_remote(key.split(":", 1)[1])
        path = next((p for p in self.discover() if self._rel(p) == key), None)
        data = self._read(path) if path else None
        if data is None:
            return {"error": "tarefa não encontrada"}
        pid = data.get("pid")
        if data.get("status") != "running" or not pid_alive(pid) or pid == os.getpid():
            return {"error": "a tarefa não está em curso"}
        kill_tree(int(pid))
        try:  # deixa o progresso marcado como cancelado (o processo já não o vai atualizar)
            data["status"] = "error"
            data["label"] = f"{data.get('label', '')} — cancelada"
            data["updated_at"] = self._now()
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
        return {"ok": True}
