import json
import io
import os
import sys
import pathlib
import subprocess
import asyncio
from typing import Optional, NamedTuple, Any, Awaitable, Dict

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def get_dap(pred=lambda ex: ex.name.startswith('ms-python')
        ) -> Optional[pathlib.Path]:
    home = pathlib.Path(os.environ['USERPROFILE'])
    extensions = home / '.vscode/extensions'
    ms_python = next(iter((f for f in extensions.iterdir() if pred(f))))
    main_js = ms_python / 'out\client\debugger\debugAdapter/main.js'
    if main_js.exists():
        return main_js


class Request(NamedTuple):
    seq: int
    type: str
    command: str
    arguments: Any = None

    def to_bytes(self) -> bytes:
        body = json.dumps(self._asdict()).encode('utf-8')
        with io.BytesIO() as f:
            f.write(f'Content-Length: {len(body)}\r\n'.encode('ascii'))
            f.write(b'\r\n')
            f.write(body)
            return f.getvalue()

    def __str__(self) -> str:
        return f'<=={self.seq}:{self.command}'


class Response(NamedTuple):
    seq: int
    type: str
    request_seq: int
    success: bool
    command: str
    message: Optional[str] = None
    body: Any = None

    def __str__(self) -> str:
        return f'==>{self.request_seq}:{self.command}, {self.success}'


async def read(dap, r: asyncio.StreamReader) -> Awaitable[Response]:
    size = 0
    # header
    while True:
        l = await r.readline()
        if not l:
            print('==>EOF')
            return None
        if l == b'\r\n':
            break
        if l.startswith(b'Content-Length:'):
            size = int(l[15:].strip())

    body = await r.read(size)

    obj = json.loads(body)

    return Response(**obj)



class DAP:
    def __init__(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.next_seq = 1
        self.request_map: Dict[int, Request] = { }
        self.w = w

        # schedule infinite StreamReader
        asyncio.create_task(self._reader(r))

    async def _reader(self, r: asyncio.StreamReader):
        while True:
            res = await read(self, r)
            if not res:
                break
            print(res)

            # dispatch response
            req_fut = self.request_map.get(res.request_seq)
            if req_fut:
                req_fut.set_result(res)
            else:
                raise RuntimeError(f'request: {res.request_seq} not found')

    def _create_request(self, command, args) -> Request:
        seq = self.next_seq
        self.next_seq += 1
        return Request(seq, 'request', command, args)

    def _create_initialize_request(self) -> Request:
        req = self._create_request('initialize', {
            'clientName': 'daplauncher.py',
            'adapterID': 1,
            'pathFormat': 'path',
            })
        return req

    def _create_disconnect_request(self) -> Request:
        req =  self._create_request('disconnect', {
            'clientName': 'daplauncher.py',
            'adapterID': 1,
            'pathFormat': 'path',
            })
        return req

    async def _send_request(self, req):
        print(req)

        # Create a new Future object.
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self.request_map[req.seq] = fut

        self.w.write(req.to_bytes())
        res = await fut

        return res

    def close(self):
        print('<==close')
        self.w.close()

    async def initialize(self):
        return await self._send_request( self._create_initialize_request())

    async def disconnect(self):
        return await self._send_request( self._create_disconnect_request())

    async def terminate(self):
        req, fut = self.create_termnate_request()
        print(req)
        self.w.write(req.to_bytes())
        res = await fut


async def run(cmd, *args):
    # create process
    print(cmd, *args)
    p = await asyncio.create_subprocess_exec(cmd,
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE)
    print(p)

    dap = DAP(p.stdout, p.stdin)

    # protocol
    await dap.initialize()
    await dap.disconnect()

    dap.close()

    # wait until process terminated
    ret = await p.wait()
    print(f'terminated: {ret}')


def main() -> None:
    adapter_path = get_dap()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run('node', str(adapter_path)))


if __name__ == '__main__':
    main()
