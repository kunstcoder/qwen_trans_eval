import subprocess
import json
import os
from datetime import datetime

import pytz
import numpy as np
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
    # Remote paths
    "work_dir": "",
    "init_sh": "",        # local path to init.sh; defaults to {work_dir}/init.sh if empty
    "out_log_dir": "",    # base dir for timestamped log files
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


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        return {**DEFAULT_CONFIG, **saved}
    return DEFAULT_CONFIG.copy()


def save_config(data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _make_log_path(out_log_dir: str, data_name: str) -> str:
    """Return a timestamped log file path (Seoul timezone)."""
    seoul = pytz.timezone("Asia/Seoul")
    ts = datetime.now(seoul).strftime("%Y%m%d_%H%M%S")
    if out_log_dir:
        os.makedirs(out_log_dir, exist_ok=True)
        return os.path.join(out_log_dir, f"{data_name}.{ts}.log")
    return f"/tmp/{data_name}.{ts}.log"


def build_ssh_cmd(config: dict, dataset: dict) -> tuple[str, str]:
    """
    Replicates the original invoke-based SSH command:

      ssh -i {pem} -p {port} -t {job_host}@{jump_host}
        ssh -o StrictHostKeyChecking=no root@{api_ip}
          'bash -s -- "\"{script}\"" {work_dir} {out_log}'
        < {init_sh}

    Returns (cmd_string, out_log_path).
    """
    pem       = config["pem_path"]
    job_host  = config["job_host"]
    jump_host = config["jump_host"]
    jump_port = config.get("jump_port", "3307")
    api_ip    = config["api_ip"]
    work_dir  = config["work_dir"]
    init_sh   = config.get("init_sh") or f"{work_dir}/init.sh"
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

    # Replicates original quoting exactly:
    # 'bash -s -- "\"{script}\"" {work_dir} {out_log}'
    # → $1 = "python Qwen3_request.py ...", $2 = work_dir, $3 = out_log
    inner_cmd = f'bash -s -- "\\"{script}\\"" {work_dir} {out_log}'

    cmd = (
        f"ssh -i {pem} -p {jump_port} -t"
        f" {job_host}@{jump_host}"
        f" ssh -o StrictHostKeyChecking=no root@{api_ip}"
        f" '{inner_cmd}'"
        f" < {init_sh}"
    )
    return cmd, out_log


def read_scores(out_dir: str) -> dict:
    """Read score JSON files from out_dir and return stats."""
    if not out_dir or not os.path.isdir(out_dir):
        return {"ok": False, "error": f"디렉토리 없음: {out_dir}"}
    files = sorted(
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if f.endswith(".json")
    )
    if not files:
        return {"ok": False, "error": "score JSON 파일 없음"}
    try:
        scores = []
        for path in files:
            with open(path, encoding="utf-8") as f:
                content = json.load(f)
            scores.append(round(float(np.mean(content["score"])), 2))
        avg = round(float(np.mean(scores)), 2)
        return {"ok": True, "scores": scores, "avg": avg, "count": len(scores)}
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

    # Merge UI values into config (except datasets, handled separately)
    for key in DEFAULT_CONFIG:
        if key != "datasets" and key in data:
            config[key] = data[key]

    datasets = [d for d in data.get("datasets", []) if d.get("enabled")]

    def generate():
        for dataset in datasets:
            name = dataset.get("data_name", "?")
            yield f"data: \n\n"
            yield f"data: ══ {name}  [{dataset.get('mode', '')}] ══\n\n"

            try:
                cmd, out_log = build_ssh_cmd(config, dataset)
            except Exception as e:
                yield f"data: [ERROR] 커맨드 빌드 실패: {e}\n\n"
                continue

            yield f"data: $ {cmd}\n\n"
            yield f"data: log → {out_log}\n\n"
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

                if proc.returncode == 0:
                    yield f"data: ✔ {name} 완료 (exit 0)\n\n"
                else:
                    yield f"data: ✘ {name} 실패 (exit {proc.returncode})\n\n"

            except Exception as e:
                yield f"data: [ERROR] {e}\n\n"

            # Try to read scores from local out_dir
            out_dir = dataset.get("out_dir", "")
            score_result = read_scores(out_dir)
            yield f"event: score\ndata: {json.dumps({'dataset': name, 'mode': dataset.get('mode',''), **score_result}, ensure_ascii=False)}\n\n"

        yield "event: done\ndata: 0\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/scores", methods=["POST"])
def scores_route():
    data = request.get_json(force=True)
    return read_scores(data.get("out_dir", ""))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
