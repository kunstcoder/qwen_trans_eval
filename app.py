import base64
import subprocess
import json
import os
from datetime import datetime

import pytz
from flask import Flask, render_template, request, Response, stream_with_context

app = Flask(__name__)

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    # SSH connection
    "pem_path": "",
    "job_host": "",
    "jump_host": "jumping-host.company.com",
    "jump_port": "3307",
    "api_ip": "",
    # Remote paths (all paths below live on the remote server)
    "work_dir": "",
    "init_sh": "",       # local path to init.sh streamed via stdin; defaults to {work_dir}/init.sh
    "out_log_dir": "",   # REMOTE base dir for log files (created by init.sh / Qwen3_request.py)
    "num_repeat": "380",
    # Datasets
    "datasets": [
        {
            "data_name": "7domain_ko2en",
            "src_dir": "",
            "hyp_dir": "",
            "out_dir": "",
            "mode": "ko2en",
            "enabled": True,
        },
        {
            "data_name": "7domain_en2ko",
            "src_dir": "",
            "hyp_dir": "",
            "out_dir": "",
            "mode": "en2ko",
            "enabled": True,
        },
    ],
}


# ── Config I/O ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        return {**DEFAULT_CONFIG, **saved}
    return DEFAULT_CONFIG.copy()


def save_config(data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── SSH helpers ───────────────────────────────────────────────────────────────

def _make_log_path(out_log_dir: str, data_name: str) -> str:
    """
    Build a REMOTE log file path with Seoul-timezone timestamp.
    No local filesystem operations — the path is only used as a string
    argument passed to the remote Qwen3_request.py via SSH.
    """
    seoul = pytz.timezone("Asia/Seoul")
    ts = datetime.now(seoul).strftime("%Y%m%d_%H%M%S")
    base = out_log_dir.rstrip("/") if out_log_dir else "/tmp"
    return f"{base}/{data_name}.{ts}.log"


def build_ssh_cmd(config: dict, dataset: dict) -> tuple[str, str]:
    """
    Replicates runner.py's 2-hop SSH command exactly:

      ssh -i {pem} -p {port} -t {job_host}@{jump_host}
        ssh -o StrictHostKeyChecking=no root@{api_ip}
          'bash -s -- "\"{script}\"" {work_dir} {out_log}'
        < {init_sh}          ← local init.sh piped as stdin

    $1 in init.sh receives: "python Qwen3_request.py ..."
    $2 = work_dir,  $3 = out_log  (both remote paths)

    Returns (cmd_string, remote_out_log_path).
    """
    pem        = config["pem_path"]
    job_host   = config["job_host"]
    jump_host  = config["jump_host"]
    jump_port  = config.get("jump_port", "3307")
    api_ip     = config["api_ip"]
    work_dir   = config["work_dir"]
    init_sh    = config.get("init_sh") or f"{work_dir}/init.sh"
    num_repeat = config.get("num_repeat", "380")

    out_log = _make_log_path(config.get("out_log_dir", ""), dataset["data_name"])

    script = (
        f"python Qwen3_request.py"
        f" --src-dir={dataset['src_dir']}"
        f" --hyp-dir={dataset['hyp_dir']}"
        f" --out-dir={dataset['out_dir']}"
        f" --mode={dataset['mode']}"
        f" --log-path={out_log}"
        f" --num-repeat={num_repeat}"
    )

    # Replicates original quoting: 'bash -s -- "\"script\"" work_dir out_log'
    # → $1 = "python Qwen3_request.py ...",  $2 = work_dir,  $3 = out_log
    inner_cmd = f'bash -s -- "\\"{script}\\"" {work_dir} {out_log}'

    cmd = (
        f"ssh -i {pem} -p {jump_port} -t"
        f" {job_host}@{jump_host}"
        f" ssh -o StrictHostKeyChecking=no root@{api_ip}"
        f" '{inner_cmd}'"
        f" < {init_sh}"
    )
    return cmd, out_log


def fetch_remote_scores(config: dict, out_dir: str) -> dict:
    """
    SSH through the jump host to the API server and execute a small Python
    script that reads score JSON files from the REMOTE out_dir.

    The script is base64-encoded to avoid any shell quoting issues across
    the two SSH hops.

    SSH chain (no TTY, no stdin pipe needed):
      local → jump_host → api_ip
    """
    if not out_dir:
        return {"ok": False, "error": "out_dir 미설정"}

    pem       = config.get("pem_path", "")
    job_host  = config.get("job_host", "")
    jump_host = config.get("jump_host", "")
    jump_port = config.get("jump_port", "3307")
    api_ip    = config.get("api_ip", "")

    if not all([pem, job_host, jump_host, api_ip]):
        return {"ok": False, "error": "SSH 설정 불완전 (pem_path / job_host / jump_host / api_ip)"}

    # Escape out_dir for safe embedding in a Python string literal
    safe_dir = out_dir.replace("\\", "\\\\").replace("'", "\\'")

    # Python script that runs on the remote server
    # Mirrors legacy runner.py: os.listdir() (no extension filter) + utf-8 open
    py_src = "\n".join([
        "import os, json",
        f"try: files = sorted(['{safe_dir}/' + f for f in os.listdir('{safe_dir}')])",
        f"except FileNotFoundError: files = []",
        "if not files:",
        f"    print(json.dumps({{'ok': False, 'error': 'no files in {safe_dir}'}}))",
        "else:",
        "    scores = []",
        "    for f in files:",
        "        with open(f, encoding='utf-8') as fp:",
        "            data = json.load(fp)",
        "        s = data['score']",
        "        vals = s if isinstance(s, list) else [s]",
        "        scores.append(round(sum(vals) / len(vals), 2))",
        "    avg = round(sum(scores) / len(scores), 2)",
        "    print(json.dumps({'ok': True, 'scores': scores, 'avg': avg, 'count': len(scores)}))",
    ])

    # Encode to avoid all shell quoting problems (base64 is shell-safe)
    encoded = base64.b64encode(py_src.encode()).decode()
    remote_cmd = f"echo {encoded} | base64 -d | python3"

    # No -t (no TTY needed), -n prevents reading from local stdin
    cmd = (
        f"ssh -n -i {pem} -p {jump_port}"
        f" {job_host}@{jump_host}"
        f" \"ssh -n -o StrictHostKeyChecking=no root@{api_ip} '{remote_cmd}'\""
    )

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=60,
        )
        # Search from the last line backward for a JSON object
        for line in reversed(result.stdout.strip().split("\n")):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        err = result.stderr.strip() or f"JSON 없음 — stdout: {result.stdout[:300]!r}"
        return {"ok": False, "error": err}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "타임아웃 (60s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", config=load_config())


@app.route("/save_config", methods=["POST"])
def save_config_route():
    data = request.get_json()
    if not data:
        return {"ok": False, "error": "데이터 없음"}, 400
    save_config(data)
    return {"ok": True}


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(force=True)
    config = load_config()

    for key in DEFAULT_CONFIG:
        if key != "datasets" and key in data:
            config[key] = data[key]

    datasets = [d for d in data.get("datasets", []) if d.get("enabled")]

    def generate():
        for dataset in datasets:
            name = dataset.get("data_name", "?")
            yield "data: \n\n"
            yield f"data: ══ {name}  [{dataset.get('mode', '')}] ══\n\n"

            try:
                cmd, out_log = build_ssh_cmd(config, dataset)
            except Exception as e:
                yield f"data: [ERROR] 커맨드 빌드 실패: {e}\n\n"
                continue

            yield f"data: $ {cmd}\n\n"
            yield f"data: log → {out_log}  (원격 서버)\n\n"
            yield "data: ---\n\n"

            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                )
                for line in iter(proc.stdout.readline, b""):
                    text = line.decode("utf-8", errors="replace").rstrip("\n")
                    yield f"data: {text}\n\n"
                proc.stdout.close()
                proc.wait()

                rc = proc.returncode
                if rc == 0:
                    yield f"data: ✔ {name} 완료 (exit 0)\n\n"
                else:
                    yield f"data: ✘ {name} 실패 (exit {rc})\n\n"

            except Exception as e:
                yield f"data: [ERROR] {e}\n\n"

            # Fetch scores from the REMOTE out_dir via SSH
            out_dir = dataset.get("out_dir", "")
            yield f"data: [score] 원격 {out_dir} 에서 결과 읽는 중...\n\n"
            score_result = fetch_remote_scores(config, out_dir)

            # Print scores to terminal (mirrors legacy runner.py output)
            if score_result.get("ok"):
                scores = score_result.get("scores", [])
                avg = score_result.get("avg", 0)
                count = score_result.get("count", 0)
                mode = dataset.get("mode", "")
                for s in scores:
                    yield f"data: {s}\n\n"
                yield f"data: {avg} ({mode} {count} avg)\n\n"
            else:
                yield f"data: [score error] {score_result.get('error', '알 수 없는 오류')}\n\n"

            payload = {
                "dataset": name,
                "mode": dataset.get("mode", ""),
                **score_result,
            }
            yield f"event: score\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield "event: done\ndata: 0\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/scores", methods=["POST"])
def scores_route():
    """
    Manual score fetch.
    Body: { ...ssh config fields..., out_dir: '/remote/path' }
    """
    data = request.get_json(force=True)
    config = {**load_config()}
    for key in DEFAULT_CONFIG:
        if key != "datasets" and key in data:
            config[key] = data[key]
    return fetch_remote_scores(config, data.get("out_dir", ""))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
