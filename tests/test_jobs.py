"""Tarefas do painel: descoberta de progresso, lançar, cancelar e servir o separador (sem rede, subprocessos mínimos)."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from osuml.jobs import JobManager, Template
from osuml.jobs.core import pid_alive
from osuml.panel.dashboard import make_dashboard
from osuml.progress import Progress

# script mínimo: escreve progresso "running" e dorme (ou acaba já) conforme o 2.º argumento
CODE = (
    "import sys,time;from osuml.progress import Progress\n"
    "p=Progress(sys.argv[1],'Tarefa de teste',10,'passos',min_interval=0);p.update(4,force=True)\n"
    "if sys.argv[2]=='sleep': time.sleep(60)\n"
    "p.finish()\n")


def _mgr(tmp_path, mode="quick", **kw):
    tpl = {"t": Template("Tarefa", "descrição", lambda m, prog: [prog, mode], "1 s")}
    return JobManager(tmp_path, tmp_path / "processed", tmp_path / "logs", templates=tpl,
                      prefix=[sys.executable, "-c", CODE], **kw)


def _wait(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_state_discovers_progress_files_and_classifies_them(tmp_path):
    m = _mgr(tmp_path)
    proc = tmp_path / "processed"
    Progress(proc / "a" / "x.progress.json", "Em curso", 10, "passos", min_interval=0).update(3, force=True)   # pid = este processo (vivo)
    done = Progress(proc / "b" / "progress.json", "Feita", 5, "linhas", min_interval=0)
    done.finish()
    dead = proc / "c" / "progress_all.json"
    dead.parent.mkdir(parents=True)
    dead.write_text(json.dumps({"label": "Morta", "status": "running", "done": 1, "total": 9, "unit": "mapas", "started_at": time.time() - 900,
                                "updated_at": time.time() - 600, "computed_now": 1, "pid": 4_000_000}), encoding="utf-8")
    old = proc / "d" / "progress.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"label": "Antiga", "status": "done", "done": 1, "total": 1, "started_at": 1, "updated_at": 2}), encoding="utf-8")
    st = m.state()
    by = {t["label"]: t for t in st["tasks"]}
    assert by["Em curso"]["status"] == "running" and by["Em curso"]["pct"] == 30.0 and by["Em curso"]["alive"] is True
    assert by["Feita"]["status"] == "done" and by["Feita"]["pct"] == 100.0
    assert by["Morta"]["status"] == "interrupted"  # sem sinal há 10 min e o processo já não existe
    assert "Antiga" not in by  # terminada há muito tempo: fora da lista
    assert st["running"] == 1 and [t["label"] for t in st["tasks"]][0] == "Em curso"  # em curso primeiro


def test_start_runs_a_predefined_template_and_reports_completion(tmp_path):
    m = _mgr(tmp_path, "quick")
    assert m.start("t")["ok"] is True
    assert _wait(lambda: any(t["status"] == "done" for t in m.state()["tasks"]))
    t = m.state()["tasks"][0]
    assert t["owned"] and t["template"] == "t" and t["exit_code"] == 0 and t["pct"] == 100.0
    assert m.start("nao-existe")["error"] == "tarefa desconhecida"


def test_cannot_start_twice_and_cancel_kills_the_process(tmp_path):
    m = _mgr(tmp_path, "sleep")
    assert m.start("t")["ok"] is True
    assert _wait(lambda: m.state()["running"] == 1)
    assert m.start("t")["error"] == "esta tarefa já está em curso"
    task = m.state()["tasks"][0]
    assert m.state()["templates"][0]["running"] is True
    pid = task["pid"]
    assert m.cancel(task["key"])["ok"] is True
    assert _wait(lambda: not pid_alive(pid))
    after = m.state()["tasks"][0]
    assert after["status"] in ("error", "interrupted") and "cancelada" in after["label"]
    assert m.cancel(task["key"])["error"] == "a tarefa não está em curso"
    assert m.cancel("nao/existe.json")["error"] == "tarefa não encontrada"


def test_start_refuses_when_required_files_are_missing(tmp_path):
    tpl = {"t": Template("T", "d", lambda m, prog: [prog, "quick"], "", ["ficheiro/que/nao/existe"])}
    m = JobManager(tmp_path, tmp_path / "processed", tmp_path / "logs", templates=tpl, prefix=[sys.executable, "-c", CODE])
    assert m.start("t")["error"].startswith("falta ")
    assert m.state()["templates"][0]["missing"] == ["ficheiro/que/nao/existe"]


def test_dashboard_serves_the_tasks_tab_and_launches_via_token(tmp_path):
    m = _mgr(tmp_path, "quick")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server, token = make_dashboard(SimpleNamespace(state=lambda: {}), None, port, None, m)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(base + "/tasks").read().decode()
        assert token in page and "/tasks/api/start" in page and "__TOKEN__" not in page
        shell = urllib.request.urlopen(base + "/").read().decode()
        assert '"tasks"' in shell and "strip" in shell
        state = json.loads(urllib.request.urlopen(base + "/tasks/api/state").read())
        assert state["templates"][0]["name"] == "t" and state["running"] == 0
        req = urllib.request.Request(base + "/tasks/api/start", data=json.dumps({"name": "t"}).encode(),
                                     headers={"X-Panel-Token": token, "Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        assert _wait(lambda: any(t["status"] == "done" for t in m.state()["tasks"]))
    finally:
        server.shutdown()


def test_template_waits_for_the_tasks_it_depends_on(tmp_path):
    tpl = {"a": Template("A", "d", lambda m, prog: [prog, "sleep"]),
           "b": Template("B", "d", lambda m, prog: [prog, "quick"], "", [], ["a"])}
    m = JobManager(tmp_path, tmp_path / "processed", tmp_path / "logs", templates=tpl, prefix=[sys.executable, "-c", CODE])
    assert m.start("a")["ok"] is True
    assert _wait(lambda: m.state()["running"] == 1)
    st = {t["name"]: t for t in m.state()["templates"]}
    assert st["b"]["blocked_by"] == ["a"] and "espera" in m.start("b")["error"]
    key = m.state()["tasks"][0]["key"]
    m.cancel(key)
    assert _wait(lambda: not m.state()["templates"][1]["blocked_by"])
    assert m.start("b")["ok"] is True


def test_remote_tasks_from_a_pod_are_shown_and_cancellable(tmp_path):
    calls: list[list[str]] = []
    payload = {"label": "Modelo pass/fail", "status": "running", "done": 30, "total": 100, "unit": "passos", "started_at": time.time() - 60,
               "updated_at": time.time(), "computed_now": 30, "pid": 4242}

    def fake_ssh(argv):
        calls.append(argv)
        if "kill" in argv[-1]:
            return 0, "", ""
        return 0, json.dumps(payload) + "\n---LOG---\nlinha de log\n", ""

    remote = tmp_path / "remote_tasks.json"
    remote.write_text(json.dumps([{"name": "pod1", "label": "Pod RunPod", "host_label": "RunPod", "ssh": "root@1.2.3.4", "port": 39416,
                                   "key": "~/.ssh/pod", "progress": "/root/work/out/progress.json", "log": "/root/work/run.log"}]), encoding="utf-8")
    m = JobManager(tmp_path, tmp_path / "processed", tmp_path / "logs", templates={}, remote_file=remote, ssh_runner=fake_ssh)
    st = m.state()
    t = st["tasks"][0]
    assert t["remote"] == "RunPod" and t["status"] == "running" and t["pct"] == 30.0 and t["key"] == "remote:pod1" and "linha de log" in t["log"]
    assert st["running"] == 1 and "39416" in calls[0] and calls[0][-1].startswith("cat /root/work/out/progress.json")
    assert m.cancel("remote:pod1")["ok"] is True and "kill -TERM" in calls[-1][-1] and "4242" in calls[-1][-1]
    assert m.cancel("remote:nao-existe")["error"] == "tarefa remota não encontrada"


def test_remote_task_survives_an_unreachable_pod(tmp_path):
    state = {"up": True}

    def fake_ssh(argv):
        if state["up"]:
            return 0, json.dumps({"label": "x", "status": "running", "done": 1, "total": 2, "started_at": 1, "updated_at": time.time(), "pid": 1}) + "\n---LOG---\n", ""
        return 255, "", "Connection refused"

    remote = tmp_path / "r.json"
    remote.write_text(json.dumps([{"name": "p", "ssh": "root@h", "progress": "/p.json"}]), encoding="utf-8")
    clock = {"t": time.time()}
    m = JobManager(tmp_path, tmp_path / "processed", tmp_path / "logs", templates={}, remote_file=remote, ssh_runner=fake_ssh, now=lambda: clock["t"])
    assert m.state()["tasks"][0]["status"] == "running"
    state["up"] = False
    clock["t"] += 10  # passou a cache de 3 s
    t = m.state()["tasks"][0]
    assert t.get("unreachable") is True and t["pct"] == 50.0  # mantém o último estado conhecido, marcado como sem ligação


def test_a_pod_with_many_tasks_is_read_with_a_single_ssh_call(tmp_path):
    now = time.time()
    a = {"label": "Download 2026_08_01", "status": "running", "done": 50, "total": 100, "unit": "bytes", "started_at": now - 10, "updated_at": now,
         "computed_now": 50, "pid": 111}
    b = {"label": "Pipeline — fase 1/5", "status": "running", "done": 0, "total": 5, "unit": "fases", "started_at": now - 20, "updated_at": now,
         "computed_now": 0, "pid": 222}
    calls: list[list[str]] = []

    def fake_ssh(argv):
        calls.append(argv)
        if "kill" in argv[-1]:
            return 0, "", ""
        return 0, f"@@FILE /w/progress/00_pipeline.json\n{json.dumps(b)}\n@@FILE /w/progress/dl_2026_08_01.json\n{json.dumps(a)}\n@@LOG\nlog do pipeline\n", ""

    remote = tmp_path / "r.json"
    remote.write_text(json.dumps([{"name": "pod", "label": "Pod treino", "ssh": "root@h", "progress_dir": "/w/progress", "log": "/w/logs/pipeline.log"}]),
                      encoding="utf-8")
    m = JobManager(tmp_path, tmp_path / "processed", tmp_path / "logs", templates={}, remote_file=remote, ssh_runner=fake_ssh)
    st = m.state()
    assert len(calls) == 1 and st["running"] == 2
    keys = {t["key"]: t for t in st["tasks"]}
    assert set(keys) == {"remote:pod:00_pipeline", "remote:pod:dl_2026_08_01"}
    assert keys["remote:pod:dl_2026_08_01"]["pct"] == 50.0 and "Download" in keys["remote:pod:dl_2026_08_01"]["label"]
    assert "log do pipeline" in keys["remote:pod:00_pipeline"]["log"]
    assert m.cancel("remote:pod:dl_2026_08_01")["ok"] is True and "111" in calls[-1][-1]


def test_eta_uses_the_recent_speed_not_the_average_since_the_start(tmp_path):
    clock = {"t": 1000.0}
    prog = tmp_path / "processed" / "progress_x.json"
    prog.parent.mkdir(parents=True)

    def write(done):
        prog.write_text(json.dumps({"label": "x", "status": "running", "done": done, "total": 1000, "unit": "passos", "started_at": 0.0,
                                    "updated_at": clock["t"], "computed_now": done, "pid": None}), encoding="utf-8")

    m = JobManager(tmp_path, tmp_path / "processed", tmp_path / "logs", templates={}, now=lambda: clock["t"])
    write(100)
    m.state()  # 1.ª amostra: 100 passos ao fim de 1000 s (média 0,1/s)
    clock["t"] += 60
    write(280)  # +180 passos em 60 s = 3/s reais
    t = m.state()["tasks"][0]
    assert 2.5 < t["rate"] < 3.5 and t["eta_s"] < 400  # (1000-280)/3 ≈ 240 s; pela média seria ~2 h
