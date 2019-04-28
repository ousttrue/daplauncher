import json
import io
import os
import sys
import pathlib
import subprocess
import asyncio
from typing import Optional

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


def to_bytes(obj) -> bytes:
    body = json.dumps(obj).encode('utf-8')
    with io.BytesIO() as f:
        f.write(f'Content-Length: {len(body)}\r\n'.encode('ascii'))
        f.write(b'\r\n')
        f.write(body)
        return f.getvalue()


class DAP:
    def __init__(self) -> None:
        self.next_seq = 1

    def get_initialize_request(self):
        seq = self.next_seq
        self.next_seq += 1

        obj = {
            'seq': seq,
            'type': 'request',
            'command': 'initialize',
            'arguments': {
                'clientName': 'daplauncher.py',
                'adapterID': 1,
                'pathFormat': 'path',
            }
        }

        return obj

    async def read(self, f):
        size = 0
        # header
        while True:
            l = await f.readline()
            if not l:
                print('EOF')
                return None
            if l == b'\r\n':
                break
            if l.startswith(b'Content-Length:'):
                size = int(l[15:].strip())

        body = await f.read(size)

        return json.loads(body)


async def writer(f, dap):
    request = dap.get_initialize_request()
    b = to_bytes(request)
    f.write(b)
    f.close()


async def reader(f, dap):
    while True:
        msg = await dap.read(f)
        if not msg:
            break
        print(msg)


async def run(cmd, *args):
    print(cmd, *args)
    p = await asyncio.create_subprocess_exec(cmd,
                                             *args,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE,
                                             stdin=subprocess.PIPE)
    print(p)

    dap = DAP()

    # schedule tasks
    asyncio.create_task(writer(p.stdin, dap))
    asyncio.create_task(reader(p.stdout, dap))

    # wait until process terminated
    await p.wait()


def main() -> None:
    adapter_path = get_dap()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run('node', str(adapter_path)))
    print('finished')


if __name__ == '__main__':
    main()
