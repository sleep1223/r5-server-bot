import asyncio
import base64
import contextlib
import re
import struct
from typing import Any

from Cryptodome.Cipher import AES
from Cryptodome.Random import get_random_bytes
from Cryptodome.Util import Counter
from loguru import logger

try:
    from .protos import netcon_pb2
except ImportError:
    from protos import netcon_pb2


# 常量
RCON_FRAME_MAGIC = 0x6E6F4352  # ('R'+('C'<<8)+('o'<<16)+('n'<<24))


class R5NetConsole:
    def __init__(self, host: str, port: int, key: str) -> None:
        self.host = host
        self.port = port
        self.key_bytes = base64.b64decode(key)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False
        self.console_log_queue: asyncio.Queue[str] = asyncio.Queue()
        self._read_task: asyncio.Task[None] | None = None

    async def connect(self, timeout: float = 10.0) -> bool:
        logger.info(f"正在连接到 {self.host}:{self.port}...")
        try:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=timeout)
            self.connected = True
            logger.success("已连接。")
            return True
            # 不要在此时启动后台读取器，以避免与 authenticate() 发生竞争条件
        except TimeoutError:
            logger.error(f"连接到 {self.host}:{self.port} 超时，耗时 {timeout}秒。")
            raise
        return False

    async def close(self) -> None:
        self.connected = False
        if self._read_task:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task

        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        logger.info("连接已关闭。")

    def _encrypt(self, data: bytes) -> tuple[bytes, bytes]:
        """返回 (encrypted_data, iv)"""
        iv = get_random_bytes(16)
        # AES-CTR 模式
        # mbedtls 使用 IV 作为初始计数器值
        # PyCryptodome 的 Counter.new 接受整数值，或者我们可以使用字节
        ctr = Counter.new(128, initial_value=int.from_bytes(iv, byteorder="big"))
        cipher = AES.new(self.key_bytes, AES.MODE_CTR, counter=ctr)
        encrypted = cipher.encrypt(data)
        return encrypted, iv

    def _decrypt(self, data: bytes, iv: bytes) -> bytes:
        ctr = Counter.new(128, initial_value=int.from_bytes(iv, byteorder="big"))
        cipher = AES.new(self.key_bytes, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(data)

    async def send_request(self, req_type: int, msg: str = "", val: str = "") -> None:
        request = netcon_pb2.request()
        request.messageId = -1
        request.requestType = req_type  # pyright: ignore[reportAttributeAccessIssue]
        request.requestMsg = msg
        request.requestVal = val

        serialized_req = request.SerializeToString()

        envelope = netcon_pb2.envelope()
        envelope.encrypted = True

        encrypted_data, iv = self._encrypt(serialized_req)
        envelope.nonce = iv
        envelope.data = encrypted_data

        envelope_bytes = envelope.SerializeToString()
        envelope_size = len(envelope_bytes)

        # 头部：Magic (4 字节) + Length (4 字节)，均为大端序
        header = struct.pack(">II", RCON_FRAME_MAGIC, envelope_size)
        if self.writer:
            self.writer.write(header + envelope_bytes)
            await self.writer.drain()

    async def receive_response(self) -> netcon_pb2.response | None:
        if not self.reader:
            return None
        # 读取头部
        try:
            header_data = await self.reader.readexactly(8)
        except asyncio.IncompleteReadError:
            return None

        magic, length = struct.unpack(">II", header_data)

        if magic != RCON_FRAME_MAGIC:
            logger.error(f"无效的 magic: {hex(magic)}")
            return None

        # 读取信封
        try:
            envelope_data = await self.reader.readexactly(length)
        except asyncio.IncompleteReadError:
            return None

        envelope = netcon_pb2.envelope()
        envelope.ParseFromString(envelope_data)

        data = envelope.data
        if envelope.encrypted:
            data = self._decrypt(data, envelope.nonce)

        response = netcon_pb2.response()
        response.ParseFromString(data)
        return response

    async def authenticate(self, password: str) -> bool:
        # SERVERDATA_REQUEST_AUTH = 1
        await self.send_request(netcon_pb2.SERVERDATA_REQUEST_AUTH, password)
        resp = await self.receive_response()
        return not (not resp or resp.responseType != netcon_pb2.SERVERDATA_RESPONSE_AUTH)

    async def _background_reader(self) -> None:
        while self.connected:
            try:
                resp = await self.receive_response()
                if not resp:
                    # Connection closed or invalid magic
                    break

                if resp.responseType == netcon_pb2.SERVERDATA_RESPONSE_CONSOLE_LOG:
                    # print(f"[Server] {resp.responseMsg.strip()}")
                    await self.console_log_queue.put(resp.responseMsg)
                elif resp.responseType == netcon_pb2.SERVERDATA_RESPONSE_AUTH:
                    # If we receive auth response here, it means we are authenticated?
                    # Or it's a delayed response.
                    # For now, let's just log it.
                    logger.info(f"[Auth] {resp.responseMsg}")
                else:
                    # print(f"[Unknown] Type: {resp.responseType}, Msg: {resp.responseMsg}")
                    pass
            except Exception as e:  # noqa: BLE001
                logger.error(f"Error in background reader: {e}")
                if not self.connected:
                    break
                await asyncio.sleep(1)

    async def authenticate_and_start(self, password: str) -> bool:
        # 我们需要在启动后台读取器之前手动处理认证
        logger.info("正在认证...")
        await self.send_request(netcon_pb2.SERVERDATA_REQUEST_AUTH, password)

        resp = await self.receive_response()
        if resp and resp.responseType == netcon_pb2.SERVERDATA_RESPONSE_AUTH:
            try:
                val = int(resp.responseVal) if resp.responseVal else 0
            except ValueError:
                val = 0

            if val == 0:
                logger.info("正在启用控制台日志...")
                await self.send_request(netcon_pb2.SERVERDATA_REQUEST_SEND_CONSOLE_LOG, "", "1")

            logger.success(f"认证成功: {resp.responseMsg}")

            # 现在启动后台读取器
            self._read_task = asyncio.create_task(self._background_reader())
            return True
        return False

    def _normalize_exec_command(self, cmd: str, val: str) -> tuple[str, str]:
        cmd = cmd.strip()
        val = val.strip()

        if not cmd and not val:
            return "", ""

        if val:
            cmd_token = cmd.split(maxsplit=1)[0] if cmd else ""
            val_token = val.split(maxsplit=1)[0]
            full_command = val if cmd_token and val_token == cmd_token else f"{cmd} {val}".strip()
        else:
            full_command = cmd

        request_name = full_command.split(maxsplit=1)[0]
        return request_name, full_command

    async def exec_command(self, cmd: str, val: str = "", timeout: float = 2.0) -> str:  # noqa: ASYNC109
        """
        执行命令并在给定的超时时间内捕获控制台输出。
        """
        # 清空现有队列以避免旧日志
        while not self.console_log_queue.empty():
            self.console_log_queue.get_nowait()

        request_name, full_command = self._normalize_exec_command(cmd, val)
        await self.send_request(netcon_pb2.SERVERDATA_REQUEST_EXECCOMMAND, request_name, full_command)

        captured = []
        # We wait for a bit to collect logs.
        # Status command usually returns data quickly.

        end_time = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = end_time - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                line = await asyncio.wait_for(self.console_log_queue.get(), timeout=remaining)
                captured.append(line)
            except TimeoutError:
                break

        return "".join(captured)

    async def get_status(self) -> dict[str, Any]:
        raw_output = await self.exec_command("status", timeout=1.0)
        return self._parse_status(raw_output)

    async def kick(self, nucleus_id: str | int, reason: str = "") -> bool:
        cmd = f"kickid {nucleus_id}"
        if reason:
            # User requested format: kickid <uid> #<REASON>
            if not reason.startswith("#"):
                reason = f"#{reason}"
            cmd += f" {reason}"
        resp = await self.exec_command(cmd)
        return f"Kicked '{nucleus_id}' from server" in resp

    async def ban(self, nucleus_id: str | int, reason: str = "") -> bool:
        cmd = f"bannid {nucleus_id}"
        if reason:
            # User requested format: banid <uid> #<REASON>
            if not reason.startswith("#"):
                reason = f"#{reason}"
            cmd += f" {reason}"
        resp = await self.exec_command(cmd)
        return f"Added '{nucleus_id}' to banned list" in resp

    async def unban(self, nucleus_id: str | int) -> bool:
        resp = await self.exec_command(f"unban {nucleus_id}")
        return f"Removed '{nucleus_id}' from banned list" in resp

    def _clean_ip(self, ip_str: str) -> str:
        # Remove port first
        # Case 1: [IPv6]:port
        match = re.match(r"^\[(.*?)\](?::\d+)?$", ip_str)
        if match:
            ip_content = match.group(1)
            # Check for IPv4-mapped IPv6
            if ip_content.lower().startswith("::ffff:"):
                return ip_content[7:]
            return ip_content

        # Case 2: IPv4:port or just IPv4
        match = re.match(r"^([\d\.]+)(?::\d+)?$", ip_str)
        if match:
            return match.group(1)

        return ip_str

    def _parse_status(self, raw_output: str) -> dict[str, Any]:
        result = {"raw": raw_output, "details": {}, "players": [], "max_players": 0}

        lines = raw_output.splitlines()
        player_section = False
        last_player: dict[str, Any] | None = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 解析键值对，例如 "hostname: Server Name"
            if not player_section and ":" in line and not line.startswith("#"):
                parts = line.split(":", 1)
                key = parts[0].strip()
                val = parts[1].strip()
                result["details"][key] = val

                # Parse max players from "players : X humans, Y bots (Z max)"
                # if key == "players":
                #     match = re.search(r"\((\d+) max\)", val)
                #     if match:
                #         result["max_players"] = int(match.group(1))

            # 检查玩家部分头部
            if line.startswith("#") and "userid" in line and "name" in line:
                player_section = True
                continue

            if player_section:
                # 解析玩家行
                # 格式通常为：userid name uniqueid connected ping loss state rate
                # 例如：2 "Player" 12345 00:05 23 0 active 100000
                # 但名称可能包含空格。
                # 使用正则。
                # 标准状态行正则：
                # #\s*(\d+)\s+"(.*?)"\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\d+)
                # 但标题行以 # 开头，玩家行在某些版本中通常不以 # 开头，
                # 或以 # 后跟索引开头。
                # 让我们尝试灵活解析。

                # 示例：
                # # 2 "PlayerName" 123...

                # 如果行以 # 开头并且是一个数字，则它是一个玩家。
                # 或者只是一个数字。

                match = re.search(
                    r'^\s*#?\s*(\d+)\s+"(.*?)"\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\d+)(?:\s+([\d\.:]+))?',
                    line,
                )
                if match:
                    player = {
                        "userid": match.group(1),
                        "name": match.group(2),
                        "uniqueid": match.group(3),
                        "connected": match.group(4),
                        "ping": int(match.group(5)),
                        "loss": int(match.group(6)),
                        "state": match.group(7),
                        "rate": int(match.group(8)),
                        "ip": self._clean_ip(match.group(9)) if match.group(9) else "",
                    }
                    result["players"].append(player)
                    last_player = player
                elif last_player and not last_player.get("ip"):
                    # Check if line is an IP address
                    # Supports [IPv6]:port and IPv4:port
                    ip_match = re.search(r"^\[?[\da-fA-F:.]+\]?:\d+", line)
                    if ip_match:
                        last_player["ip"] = self._clean_ip(ip_match.group(0))

        return result


async def main() -> None:
    import os

    host = os.getenv("R5_RCON_HOST", "").strip()
    port = int(os.getenv("R5_RCON_PORT", "0"))
    rcon_key = os.getenv("R5_RCON_KEY", "").strip()
    rcon_pwd = os.getenv("R5_RCON_PASSWORD", "").strip()

    if not host or not port or not rcon_key or not rcon_pwd:
        raise RuntimeError("Missing RCON env vars: R5_RCON_HOST/R5_RCON_PORT/R5_RCON_KEY/R5_RCON_PASSWORD")

    client = R5NetConsole(host, port, rcon_key)
    try:
        await client.connect()
        if await client.authenticate_and_start(rcon_pwd):
            logger.success("认证成功。")

            # 执行状态命令
            logger.info("正在获取状态...")

            # import json
            # for _ in range(1):
            #     status_data = await client.get_status()
            #     logger.info("--- 状态数据 ---")
            #     logger.info(json.dumps(status_data, indent=2, ensure_ascii=False))
            #     await asyncio.sleep(1)

            logger.info("正在unban玩家...")
            is_success = await client.unban(1012222108277)
            if is_success:
                logger.success("玩家unban成功。")
            else:
                logger.error("玩家unban失败。")

        else:
            logger.error("认证失败。")

    except Exception as e:  # noqa: BLE001
        logger.exception(f"Error: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
