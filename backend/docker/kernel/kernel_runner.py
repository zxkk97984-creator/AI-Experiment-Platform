"""Kernel runner — runs inside Docker container, connects to persistent ipykernel.

Reads {"code": "..."} from stdin, connects to ipykernel via /tmp/conn.json,
executes the code, collects IOPub outputs, prints JSON result to stdout.
Student code never appears in argv.
"""
import json
import sys

from jupyter_client import BlockingKernelClient

# 单元格最长 30 分钟（教师可配 10–1800 秒），期间可能长时间没有输出。
# 30 秒的 iopub 等待会让「安静超过 30 秒」的单元格被误判为执行完毕（输出丢失），
# 真正的硬边界由外层 docker exec 的 timeout 决定，所以这里等到会话最长时限。
IOPUB_WAIT_SECONDS = 1800


def main():
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError) as e:
        print(json.dumps({"outputs": [], "error": str(e)}))
        sys.exit(1)

    code = data.get("code", "")
    if not code:
        print(json.dumps({"outputs": [], "error": "empty code"}))
        return

    # Load connection file
    with open("/tmp/conn.json") as f:
        conn_info = json.load(f)
    conn_info["ip"] = "127.0.0.1"
    conn_info["kernel_name"] = ""

    kc = BlockingKernelClient()
    kc.load_connection_info(conn_info)

    try:
        kc.start_channels()
        kc.wait_for_ready(timeout=10)

        msg_id = kc.execute(code)
        outputs = []
        while True:
            try:
                msg = kc.get_iopub_msg(timeout=IOPUB_WAIT_SECONDS)
            except Exception:
                break
            mt = msg.get("msg_type", "")
            if mt == "status":
                if msg["content"].get("execution_state") == "idle":
                    break
            elif mt in ("stream", "display_data", "execute_result", "error"):
                content = dict(msg["content"])
                if "data" in content:
                    content["data"] = {
                        k: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                        for k, v in content["data"].items()
                    }
                outputs.append({"msg_type": mt, "content": content})
    finally:
        try:
            kc.stop_channels()
        except Exception:
            pass

    print(json.dumps({"outputs": outputs}))


if __name__ == "__main__":
    main()
