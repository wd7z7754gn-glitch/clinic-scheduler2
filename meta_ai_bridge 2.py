"""
meta_ai_bridge.py - Meta AI Tool Bridge Server

讓 Meta AI (或任何支援 Function Calling 的 AI) 透過 HTTP API
呼叫診所排班查詢與預約工具。

使用方式：
    python meta_ai_bridge.py [--port 8765]

Meta AI 透過 POST /tool_call 呼叫工具：
    {
      "tool": "search_doctor_availability" | "book_appointment" | "list_doctors",
      "params": { ... }
    }
"""

import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import argparse

try:
    from clinic_scheduler import search_availability, book_appointment, list_doctors
except ImportError:
    # 若在不同目錄執行，動態加入路徑
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from clinic_scheduler import search_availability, book_appointment, list_doctors


class ToolHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", ""):
            self._send_json(200, {
                "service": "clinic_scheduler",
                "status": "ok",
                "endpoints": {
                    "GET /health": "健康檢查",
                    "GET /doctors": "醫師清單",
                    "GET /schema": "Meta AI Tool Schema",
                    "POST /tool_call": "呼叫工具 (search_doctor_availability / book_appointment / list_doctors)"
                }
            })
        elif parsed.path == "/schema":
            import os
            schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meta_ai_tool_schema.json")
            with open(schema_path, encoding="utf-8") as f:
                schema = json.load(f)
            self._send_json(200, schema)
        elif parsed.path == "/health":
            self._send_json(200, {"status": "ok", "service": "clinic_scheduler"})
        elif parsed.path == "/doctors":
            self._send_json(200, list_doctors())
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/tool_call":
            self._send_json(404, {"error": "Not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_json(400, {"error": "Empty body"})
            return

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        tool = body.get("tool", "")
        params = body.get("params", {})

        if tool == "search_doctor_availability":
            result = search_availability(
                doctor_name=params.get("doctor_name", ""),
                days=int(params.get("days", 3)),
            )
        elif tool == "book_appointment":
            result = book_appointment(
                doctor_name=params.get("doctor_name", ""),
                date=params.get("date", ""),
                time=params.get("time", ""),
                patient_name=params.get("patient_name", ""),
                patient_phone=params.get("patient_phone", ""),
                duration_mins=int(params.get("duration_mins", 15)),
                site=params.get("site"),
                note=params.get("note", ""),
            )
        elif tool == "list_doctors":
            result = list_doctors()
        else:
            self._send_json(400, {"error": f"Unknown tool: '{tool}'"})
            return

        self._send_json(200, result)


def main():
    import os
    parser = argparse.ArgumentParser(description="Clinic Scheduler - Meta AI Bridge Server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)), help="監聽埠號")
    parser.add_argument("--host", default="0.0.0.0", help="監聽位址")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), ToolHandler)
    print(f"[ClinicScheduler] Bridge server running at http://{args.host}:{args.port}")
    print(f"  GET  /health      - 健康檢查")
    print(f"  GET  /schema      - Meta AI Tool Schema")
    print(f"  GET  /doctors     - 醫師清單")
    print(f"  POST /tool_call   - 呼叫工具")
    print(f"\n按 Ctrl+C 停止")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止伺服器")
        server.server_close()


if __name__ == "__main__":
    main()
