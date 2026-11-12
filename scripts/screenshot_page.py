#!/usr/bin/env python3
"""用 Firefox marionette 协议做「等 JS 渲染完再截图」的冒烟工具。

不需要 selenium / geckodriver / playwright，只走 TCP + marionette 协议，
适合在无 chromium、无 WebDriver 的环境里做前端页面冒烟取证。

前置（起一个开着 marionette 的 Firefox）：
    mkdir -p /tmp/ffprof
    printf 'user_pref("marionette.port", 2828);\\n' > /tmp/ffprof/user.js
    firefox --headless --marionette -profile /tmp/ffprof -no-remote about:blank &

用法：
    screenshot_page.py <port> <url> <out.png> [wait_s] [width] [height] [full]

参数：
    port       marionette 监听端口（与 user.js 里的 marionette.port 一致）
    url        要截图的地址
    out.png    输出文件
    wait_s     导航后等待秒数，给前端请求+渲染留时间（默认 3.0）
    width      视口宽（默认 1440）
    height     视口高（默认 1200）
    full       传任意值即整页截图（默认只截视口）

注意：脚本结尾会 Quit Firefox，连续截多张需每次重启 Firefox。
"""
import base64
import json
import socket
import sys
import time


class Marionette:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=60)
        self.buf = b""
        self._read_packet()  # 握手: {"applicationType":"gecko","marionetteProtocol":3}
        self.next_id = 1

    def _read_packet(self):
        while b":" not in self.buf:
            self.buf += self.sock.recv(4096)
        length, _, rest = self.buf.partition(b":")
        need = int(length)
        self.buf = rest
        while len(self.buf) < need:
            self.buf += self.sock.recv(65536)
        payload, self.buf = self.buf[:need], self.buf[need:]
        return json.loads(payload.decode())

    def _send(self, name, params=None):
        msg_id = self.next_id
        self.next_id += 1
        body = json.dumps([0, msg_id, name, params or {}]).encode()
        # 协议帧格式：<长度>:<json>，长度是字节数
        self.sock.sendall(str(len(body)).encode() + b":" + body)
        return msg_id

    def call(self, name, params=None):
        msg_id = self._send(name, params)
        while True:
            pkt = self._read_packet()
            # 响应包形如 [1, msgId, error, result]：error 在下标 2，结果在下标 3
            if isinstance(pkt, list) and len(pkt) >= 4 and pkt[1] == msg_id:
                if pkt[2]:
                    raise RuntimeError(pkt[2])
                return pkt[3]
            # 其它事件包（如 marionette:aknownissue）直接丢弃


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    port = int(sys.argv[1])
    url = sys.argv[2]
    out = sys.argv[3]
    wait = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0
    width = int(sys.argv[5]) if len(sys.argv) > 5 else 1440
    height = int(sys.argv[6]) if len(sys.argv) > 6 else 1200
    full = len(sys.argv) > 7

    m = Marionette(port)
    m.call("WebDriver:NewSession", {"capabilities": {}})
    m.call("WebDriver:SetWindowRect", {"width": width, "height": height})
    m.call("WebDriver:Navigate", {"url": url})
    time.sleep(wait)
    result = m.call("WebDriver:TakeScreenshot", {"full": full, "hash": False})
    b64 = result["value"] if isinstance(result, dict) else result
    with open(out, "wb") as fh:
        fh.write(base64.b64decode(b64))
    print("saved", out, "bytes", len(b64))
    m.call("Marionette:Quit", {"flags": ["eForceQuit"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())