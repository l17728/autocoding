"""Interrupt this probe's opaque HTTPS tunnels; never alter system networking."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from resume_probe import ROOT,Host,turn
from autocoding.core import DomainError


class TunnelProxy:
    def __init__(self):
        self.blocked=False;self.writers=set();self.tasks=set()
        self.connections=0;self.rejected=0;self.bytes=0

    async def handle(self,reader,writer):
        task=asyncio.current_task();self.tasks.add(task)
        upstream=None
        try:
            header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),10)
            method,target,_=header.split(b'\r\n',1)[0].decode('ascii').split(' ')
            host,port=target.rsplit(':',1)
            if method!='CONNECT' or port!='443' or self.blocked:
                self.rejected+=1;writer.write(b'HTTP/1.1 503 Unavailable\r\n\r\n');await writer.drain();return
            remote,upstream=await asyncio.wait_for(asyncio.open_connection(host,int(port)),15)
            if self.blocked:
                self.rejected+=1;return
            # No TLS decryption, headers, prompts or credentials are persisted.
            self.connections+=1;self.writers.update([writer,upstream])
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain()
            async def relay(source,destination):
                while data:=await source.read(65536):
                    self.bytes+=len(data);destination.write(data);await destination.drain()
            a=asyncio.create_task(relay(reader,upstream));b=asyncio.create_task(relay(remote,writer))
            try:await asyncio.wait([a,b],return_when=asyncio.FIRST_COMPLETED)
            finally:
                a.cancel();b.cancel();await asyncio.gather(a,b,return_exceptions=True)
        except (OSError,ValueError,asyncio.IncompleteReadError,asyncio.LimitOverrunError,TimeoutError):pass
        finally:
            for stream in [writer,upstream]:
                if stream is not None:
                    self.writers.discard(stream);stream.close()
            self.tasks.discard(task)


async def main():
    output=Path(tempfile.mkdtemp(prefix='codex-network-fault-',dir=ROOT/'.local'))
    project=output/'project';project.mkdir()
    proxy=TunnelProxy();server=await asyncio.start_server(proxy.handle,'127.0.0.1',0,limit=8192)
    url='http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
    names=['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','NO_PROXY','no_proxy']
    original={name:os.environ.get(name) for name in names}
    for name in names:os.environ[name]='' if name.lower()=='no_proxy' else url
    host=Host(project,output/'storage')
    async def disabled(*args,**kwargs):raise DomainError('ZHIPU_DEFERRED')
    host.pm_text=disabled
    report={'passed':False,'fault':'probe_scoped_HTTPS_tunnel_disconnect','outage_seconds':10,
        'natural_provider_outage':False,'system_network_changed':False,'zhipu_calls':0}
    started=time.monotonic()
    try:
        first=await host.start('codex');sid=first['session_id']
        marker='CONTEXT_'+uuid.uuid4().hex
        if await turn(host,sid,'Without tools or files remember '+marker+'. Reply OK.','first')!='completed':
            raise DomainError('FIRST_TURN_FAILED')
        if not proxy.connections or not proxy.bytes:raise DomainError('PROXY_NOT_OBSERVED')
        command=await host.send(sid,0,'Without tools or files list the integers 1 through 1000, one per line.',key='network-turn')
        async with asyncio.timeout(60):
            while host.core.command(command['command_id'])['state'] in {'queued','dispatching'}:await asyncio.sleep(.02)
        if host.core.command(command['command_id'])['state']!='acknowledged' or not proxy.writers:
            raise DomainError('FAULT_WINDOW_MISSED')
        proxy.blocked=True
        report['tunnels_interrupted']=len(proxy.writers)//2
        for writer in list(proxy.writers):writer.close()
        await asyncio.sleep(10)
        proxy.blocked=False
        async with asyncio.timeout(180):
            while host.core.command(command['command_id'])['state']=='acknowledged':await asyncio.sleep(.1)
        report['fault_turn_state']=host.core.command(command['command_id'])['state']
        report['no_new_platform_command']=len(host.core.snapshot()['commands'])==2
        if report['fault_turn_state']=='completed':
            before=host.output_seq
            report['followup_state']=await turn(host,sid,'Without tools or files return the CONTEXT_ marker I gave you.','followup')
            text=''.join(e.get('text','') for e in host.output if e['seq']>before and e['session_id']==sid)
            report['context_retained']=marker in text
            report['passed']=report['no_new_platform_command'] and report['followup_state']=='completed' and report['context_retained']
    except Exception as exc:report['error']=getattr(exc,'code',type(exc).__name__)
    finally:
        proxy.blocked=False
        await host.close()
        server.close();await server.wait_closed()
        for task in list(proxy.tasks):task.cancel()
        await asyncio.gather(*list(proxy.tasks),return_exceptions=True)
        for name,value in original.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value
        report.update(connections=proxy.connections,rejected_connections=proxy.rejected,
            tunnel_bytes=proxy.bytes,seconds=round(time.monotonic()-started,2))
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({**report,'report':str(output/'report.json')}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
