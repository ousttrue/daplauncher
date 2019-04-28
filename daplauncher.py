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
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def get_extensions_path():
    home = pathlib.Path(os.environ['USERPROFILE'])
    return home / '.vscode/extensions'


def get_python_adapter() -> Optional[pathlib.Path]:
    extensions = get_extensions_path()
    extension = next(
        iter((f for f in extensions.iterdir()
              if f.name.startswith('ms-python.python-'))))
    main = extension / 'out/client/debugger/debugAdapter/main.js'
    if main.exists():
        return 'node', [str(main)]


def get_lldb_adapter() -> Optional[pathlib.Path]:
    extensions = get_extensions_path()
    extension = next(
        iter((f for f in extensions.iterdir()
              if f.name.startswith('webfreak.debug-'))))
    main = extension / 'out/src/lldb.js'

    if main.exists():
        return 'node', [str(main)]


def get_gdb_adapter() -> Optional[pathlib.Path]:
    extensions = get_extensions_path()
    extension = next(
        iter((f for f in extensions.iterdir()
              if f.name.startswith('webfreak.debug-'))))
    main = extension / 'out/src/gdb.js'

    if main.exists():
        return 'node', [str(main)]


def get_go_adapter() -> Optional[pathlib.Path]:
    extensions = get_extensions_path()
    extension = next(
        iter((f for f in extensions.iterdir()
              if f.name.startswith('ms-vscode.go-'))))
    main = extension / 'out\src\debugAdapter\goDebug.js'
    if main.exists():
        return 'node', [str(main)]


def get_adapter(kind):
    return globals()[f'get_{kind}_adapter']()


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
        return f'<=={self.seq}: {self.command}'


class Response(NamedTuple):
    seq: int
    type: str
    request_seq: int
    success: bool
    command: str
    message: Optional[str] = None
    body: Any = None

    def __str__(self) -> str:
        j = ''
        if self.body:
            j = json.dumps(self.body, indent=2)
        return f'==>{self.request_seq}: {self.command}, {self.success}{j}'


class Event(NamedTuple):
    seq: int
    type: str
    event: str
    body: Any = None

    def __str__(self) -> str:
        return f'-->E: {self.event}'


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

    t = obj['type']
    if t == 'response':
        return Response(**obj)
    elif t == 'event':
        return Event(**obj)
    else:
        raise RuntimeError(f'unknown type: {t}')


class DAP:
    def __init__(self, r: asyncio.StreamReader, w: asyncio.StreamWriter,
                 config):
        self.next_seq = 1
        self.request_map: Dict[int, Request] = {}
        self.event_map: Dict[str, Event] = {}
        self.w = w
        self.config = config
        # schedule infinite StreamReader
        asyncio.create_task(self._reader(r))

    async def _reader(self, r: asyncio.StreamReader):
        while True:
            res = await read(self, r)
            if not res:
                break

            # dispatch response
            if isinstance(res, Response):
                print(res)
                req_fut = self.request_map.get(res.request_seq)
                if req_fut:
                    req_fut.set_result(res)
                else:
                    raise RuntimeError(f'request: {res.request_seq} not found')
            elif isinstance(res, Event):
                print(res)
                event_fut = self.event_map.get(res.event)
                if event_fut:
                    event_fut.set_result(res)
                else:
                    # do nothing
                    pass
            else:
                raise RuntimeError(f'unknown: {res}')

    def _create_request(self, command, args) -> Request:
        seq = self.next_seq
        self.next_seq += 1
        return Request(seq, 'request', command, args)

    def _create_initialize_request(self) -> Request:
        req = self._create_request('initialize', {
            'pathFormat': 'path',
        })
        return req

    def _create_configuration_done_request(self) -> Request:
        req = self._create_request('configurationDone', {})
        return req

    def _create_launch_request(self) -> Request:
        req = self._create_request('launch', self.config)
        return req

    def _create_terminate_request(self) -> Request:
        req = self._create_request('terminate', {})
        return req

    def _create_disconnect_request(self) -> Request:
        req = self._create_request('disconnect', {})
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

    async def initialize(self):
        # initialized event
        #loop = asyncio.get_event_loop()
        #fut = loop.create_future()
        #self.event_map['initialized'] = fut

        res = await self._send_request(self._create_initialize_request())

        # wait initialized event
        #await fut

        return res

    async def configuration_done(self):
        return await self._send_request(
            self._create_configuration_done_request())

    async def launch(self):
        return await self._send_request(self._create_launch_request())

    async def terminate(self):
        return await self._send_request(self._create_terminate_request())

    async def disconnect(self):
        return await self._send_request(self._create_disconnect_request())


async def error_reader(r: asyncio.StreamReader):
    while True:
        b = await r.readline()
        if not b:
            print('stderr: break')
            break
        print(b'stderr:' + b)


class Launcher:
    def __init__(self, cmd, *args, **kw):
        self.cmd = cmd
        self.args = args
        self.kw = kw
        self.p = None

    async def __aenter__(self):
        # create process
        print(self.cmd, *self.args)
        self.p = await asyncio.create_subprocess_exec(self.cmd,
                                                      *self.args,
                                                      stdout=subprocess.PIPE,
                                                      stderr=subprocess.PIPE,
                                                      stdin=subprocess.PIPE)
        print(self.p)

        # scheduled infinite error read
        asyncio.create_task(error_reader(self.p.stderr))

        return DAP(self.p.stdout, self.p.stdin, self.kw)

    async def __aexit__(self, exc_type, exc, tb):
        print('<==close')
        self.p.stdin.close()
        # wait until process terminated
        ret = await self.p.wait()
        print(f'terminated: {ret}')


async def debug_session(kw) -> None:

    cmd, args = get_adapter(kw['type'])

    # launch
    async with Launcher(cmd, *args, **kw) as dap:
        # debug session
        await dap.initialize()
        await dap.configuration_done()

        await dap.launch()

        await dap.terminate()
        await dap.disconnect()


if __name__ == '__main__':
    here = pathlib.Path(__file__).resolve().parent

    python_launch = {
        'name': 'Python: Current File',
        'type': 'python',
        'request': 'launch',
        'program': str(here / 'sample.py'),
        'console': 'integratedTerminal'
    }
    asyncio.run(debug_session(python_launch))
