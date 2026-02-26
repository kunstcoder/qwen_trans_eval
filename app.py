import subprocess
import shlex
import json
import os
from flask import Flask, render_template, request, Response, stream_with_context

app = Flask(__name__)

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "ssh_user": "user",
    "ssh_host": "server",
    "ssh_key": "",
    "remote_script": "python run.py",
    "remote_dir": "/home",
    "script_path": "something.sh",
    "args": ["", ""],
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # merge with defaults for any missing keys
        return {**DEFAULT_CONFIG, **cfg}
    return DEFAULT_CONFIG.copy()


def save_config(data: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def build_ssh_command(config: dict, args: list[str]) -> list[str]:
    """Build the ssh command list from config and argument list."""
    ssh_user = config["ssh_user"]
    ssh_host = config["ssh_host"]
    ssh_key = config.get("ssh_key", "").strip()
    script_path = config.get("script_path", "something.sh")

    remote_cmd_parts = [config["remote_script"], f"--dir={config['remote_dir']}"]
    for arg in args:
        if arg.strip():
            remote_cmd_parts.append(arg.strip())

    # Build: bash -s -- "cmd" "arg1" "arg2"
    remote_shell = "bash -s -- " + " ".join(shlex.quote(p) for p in remote_cmd_parts)

    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd += [f"{ssh_user}@{ssh_host}", remote_shell]

    return cmd, script_path


@app.route("/")
def index():
    config = load_config()
    return render_template("index.html", config=config)


@app.route("/save_config", methods=["POST"])
def save_config_route():
    data = request.get_json()
    if not data:
        return {"ok": False, "error": "No data"}, 400
    config = load_config()
    config.update(data)
    save_config(config)
    return {"ok": True}


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json()
    config = load_config()

    # Override with values sent from UI
    for key in ("ssh_user", "ssh_host", "ssh_key", "remote_script", "remote_dir", "script_path"):
        if key in data:
            config[key] = data[key]

    args = data.get("args", [])

    cmd, script_path = build_ssh_command(config, args)

    def generate():
        yield f"data: $ {' '.join(shlex.quote(c) for c in cmd)}\n\n"
        yield f"data: (stdin: {script_path})\n\n"
        yield "data: ---\n\n"

        if not os.path.exists(script_path):
            yield f"data: [ERROR] script file not found: {script_path}\n\n"
            yield "event: done\ndata: 1\n\n"
            return

        try:
            with open(script_path, "rb") as script_file:
                proc = subprocess.Popen(
                    cmd,
                    stdin=script_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                )

            for line in iter(proc.stdout.readline, b""):
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                yield f"data: {text}\n\n"

            proc.stdout.close()
            proc.wait()
            yield f"event: done\ndata: {proc.returncode}\n\n"

        except Exception as e:
            yield f"data: [ERROR] {e}\n\n"
            yield "event: done\ndata: 1\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
