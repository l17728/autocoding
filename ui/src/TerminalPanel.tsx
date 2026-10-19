import {useEffect,useRef,useState} from 'react';
import {Terminal} from '@xterm/xterm';
import {FitAddon} from '@xterm/addon-fit';
import {Unicode11Addon} from '@xterm/addon-unicode11';
import '@xterm/xterm/css/xterm.css';
import {readResponse} from './api';
import {pasteProgress,PasteDeadline,verifiedDraft} from './terminalPaste';
type Session={id:string;agent:string;owner:string;epoch:number;state:string;live:boolean;can_resume:boolean;can_stop?:boolean;startup_attempt?:number;startup_limit?:number;startup_state?:string;startup_error?:string;startup_diagnostic?:{stage:string;kind:string;winerror?:number;errno?:number;stopped_confirmed:boolean}};
const keys:Record<string,string>={'\r':'enter','\t':'tab','\x7f':'backspace','\x08':'backspace','\x1b':'escape',
  '\x1b[A':'up','\x1b[B':'down','\x1b[C':'right','\x1b[D':'left','\x1bOA':'up','\x1bOB':'down',
  '\x1bOC':'right','\x1bOD':'left','\x1b[H':'home','\x1b[F':'end','\x1bOH':'home','\x1bOF':'end',
  '\x1b[3~':'delete','\x1b[5~':'page_up','\x1b[6~':'page_down','\x03':'ctrl_c','\x10':'ctrl_p','\x1b[I':'focus_in','\x1b[O':'focus_out'};
const response=/^(?:\x1b\[(?:0n|[?>]?[0-9;]{1,32}c|[1-9][0-9]{0,2};[1-9][0-9]{0,2}R|\?[0-9;]{1,32}[$]y|\?[0-9;]{1,32}u|[468];[0-9]{1,5};[0-9]{1,5}t)|\x1b\](?:10|11|12|4;[0-9]{1,3});rgb:[0-9a-fA-F]{1,4}\/[0-9a-fA-F]{1,4}\/[0-9a-fA-F]{1,4}(?:\x07|\x1b\\)|\x1bP(?:>\|[A-Za-z0-9 ()._-]{1,64}|!\|[0-9a-fA-F]{8})\x1b\\)$/;
export function TerminalPanel({prefix,token,paused}:{prefix:string;token:string;paused:boolean}){
  const [sessions,setSessions]=useState<Session[]>([]),[selected,setSelected]=useState(''),[agent,setAgent]=useState('check');
  const [text,setText]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
  const [startupAttempts,setStartupAttempts]=useState(3);
  const [streamGeneration,setStreamGeneration]=useState(0);
  const container=useRef<HTMLDivElement>(null),session=sessions.find(s=>s.id===selected);
  const emulator=useRef<Terminal|null>(null);
  const inputState=useRef({session,paused,busy});inputState.current={session,paused,busy};
  async function api(path:string,body?:unknown){
    const response=await fetch(prefix+'/terminals'+path,{method:body===undefined?'GET':'POST',
      headers:{Authorization:`Bearer ${token}`,...(body===undefined?{}:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()})},
      body:body===undefined?undefined:JSON.stringify(body)});
    return readResponse(response);
  }
  async function act(work:()=>Promise<void>){setBusy(true);setNotice('');try{await work();setSessions(await api(''))}catch(e){setNotice(String(e))}finally{setBusy(false)}}
  async function start(body:unknown){const record=await api('',body);setSessions(await api(''));setSelected(record.id)}
  async function settlePaste(sid:string,epoch:number,value:string,receipt?:unknown){
    const deadline=new PasteDeadline(Date.now()),expected=value.replace(/\r\n?/g,'\n');
    const original=emulator.current;
    const readback=await verifiedDraft(value,receipt);
    if(receipt&&!readback)throw new Error('草稿读回校验不一致，未追加回车；请核对输入。');
    let completeSince=0,shown=-1;
    while(true){
      const state=inputState.current,s=state.session;
      if(s?.id!==sid||s.epoch!==epoch||!s.live||s.owner!=='human'||s.state!=='ready'||state.paused||emulator.current!==original)
        throw new Error('会话已改变，未追加回车；请核对输入。');
      const now=Date.now(),progress=readback?expected.length:original?pasteProgress(original,expected):0;
      if(progress>shown){shown=progress;setNotice(readback?'完整草稿已读回核对，正在等待提交，请勿重复发送。':`正在确认完整输入（${progress}/${expected.length}），请勿重复发送。`)}
      if(!deadline.observe(progress,now))break;
      if(progress===expected.length&&expected.length){
        if(!completeSince)completeSince=now;
        if(now-completeSince>=500)return;
      }else completeSince=0;
      await new Promise(resolve=>setTimeout(resolve,50));
    }
    throw new Error('尚未确认完整粘贴回显，未追加回车；请核对屏幕，不要重复发送。');
  }
  useEffect(()=>{
    let active=true,timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{try{const result=await api('');if(active)setSessions(result)}catch(e){if(active)setNotice(String(e))}finally{if(active)timer=setTimeout(poll,1000)}};
    void poll();return()=>{active=false;clearTimeout(timer)};
  },[prefix,token]);
  useEffect(()=>{
    if(!selected||!container.current)return;
    const terminal=new Terminal({allowProposedApi:true,convertEol:true,scrollback:1500,fontSize:13,theme:{background:'#111827',foreground:'#e5e7eb'},
      windowOptions:{getWinSizePixels:true,getCellSizePixels:true,getWinSizeChars:true}});
    terminal.loadAddon(new Unicode11Addon());terminal.unicode.activeVersion='11';
    const fit=new FitAddon();terminal.loadAddon(fit);terminal.open(container.current);
    emulator.current=terminal;container.current.scrollIntoView({block:'nearest'});
    const osc=terminal.parser.registerOscHandler(52,()=>true);
    // ConPTY advertises Win32 keyboard packets, but this API intentionally
    // accepts VT text and named keys. Keep xterm in that supported input mode.
    const win32=terminal.parser.registerCsiHandler({prefix:'?',final:'h'},params=>params.length===1&&params[0]===9001);
    let disposed=false,blocked=false,draining=false,replaying=false,rendering=false,renderSize=0;
    const renderQueue:{text:string;replay:boolean}[]=[];
    const renderNext=()=>{
      if(disposed||rendering||!renderQueue.length)return;
      const frame=renderQueue.shift()!;renderSize-=frame.text.length;
      rendering=true;replaying=frame.replay;
      terminal.write(frame.text,()=>{rendering=false;replaying=false;renderNext()});
    };
    const queue:{path:string;body:{epoch:number;name?:string;text?:string;bracketed?:boolean};replies?:number}[]=[];
    const drain=async()=>{if(draining)return;draining=true;try{
      while(queue.length&&!disposed&&!blocked){const item=queue.shift()!;await api(item.path,item.body)}
    }catch(e){blocked=true;queue.length=0;setNotice('输入结果待核对，键盘已冻结：'+String(e))}finally{draining=false}};
    const enqueue=(data:string)=>{
      const state=inputState.current,s=state.session;
      if(disposed||blocked||state.paused||!s?.live||s.id!==selected||s.owner!=='human'||s.state!=='ready')return;
      if(state.busy&&!response.test(data)){setNotice('终端操作尚未完成，本次键盘输入未发送；请等待操作完成。');return}
      if(data.startsWith('\x1b[200~')&&data.endsWith('\x1b[201~')){
        const value=data.slice(6,-6);
        if(!value)return;
        if(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value)){setNotice('粘贴内容含不支持的控制字符，未发送。');return}
        if(queue.length>=64){blocked=true;queue.length=0;setNotice('输入队列已满，键盘已冻结。');return}
        queue.push({path:`/${selected}/paste`,body:{epoch:s.epoch,text:value,bracketed:true}});
        queueMicrotask(()=>void drain());return;
      }
      const name=keys[data];
      const reply=response.test(data);
      if(!name&&!reply&&/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(data)){const family=data.startsWith('\x1bP')?'DCS':data.startsWith('\x1b]')?'OSC':data.startsWith('\x1b[')?'CSI':'控制键';setNotice('该控制序列尚不支持（'+family+'），请使用文本输入框。');return}
      // Combine only adjacent, unsent text. Control keys remain ordered boundaries.
      const last=queue.at(-1);
      // Capability handshakes have short deadlines. Batch only adjacent,
      // unsent, individually allowlisted replies into one audited write.
      if(reply&&last?.path.endsWith('/response')&&last.body.epoch===s.epoch&&(last.replies||0)<32&&last.body.text!.length+data.length<=4096){last.body.text+=data;last.replies=(last.replies||0)+1;return}
      if(!name&&!reply&&last?.path.endsWith('/input')&&last?.body.text!==undefined&&last.body.epoch===s.epoch&&last.body.text.length+data.length<=4096){last.body.text+=data;return}
      if(queue.length>=64){blocked=true;queue.length=0;setNotice('输入队列已满，键盘已冻结；未发送的输入已丢弃。');return}
      queue.push({path:`/${selected}/${name?'key':reply?'response':'input'}`,body:name?{epoch:s.epoch,name}:{epoch:s.epoch,text:data},replies:reply?1:undefined});queueMicrotask(()=>void drain());
    };
    // OpenTUI asks for capabilities xterm.js does not always answer by default.
    // Cursor position and DEC mode status remain handled by xterm itself.
    // Do not answer Kitty's CSI ? u query: xterm does not encode Kitty keys.
    const reply=(data:string)=>{if(!replaying)enqueue(data)};
    const queries=[
      terminal.parser.registerCsiHandler({prefix:'>',final:'q'},()=>{reply('\x1bP>|XTerm.js\x1b\\');return true}),
      // Native Windows Codex can read OSC replies as prompt text. Let its
      // optional color probe time out; other TUIs still receive their replies.
      terminal.parser.registerOscHandler(10,data=>{if(data!=='?')return false;if(inputState.current.session?.agent!=='codex')reply('\x1b]10;rgb:e5e5/e7e7/ebeb\x1b\\');return true}),
      terminal.parser.registerOscHandler(11,data=>{if(data!=='?')return false;if(inputState.current.session?.agent!=='codex')reply('\x1b]11;rgb:1111/1818/2727\x1b\\');return true}),
    ];
    const input=terminal.onData(data=>{if(!replaying||!response.test(data))enqueue(data)});
    // ConPTY can enable Win32 input mode. Map supported physical keys before
    // xterm turns them into provider-dependent CSI sequences.
    const physical:Record<string,string>={Enter:'\r',Tab:'\t',Backspace:'\x7f',Escape:'\x1b',
      ArrowUp:'\x1b[A',ArrowDown:'\x1b[B',ArrowRight:'\x1b[C',ArrowLeft:'\x1b[D',
      Home:'\x1b[H',End:'\x1b[F',Delete:'\x1b[3~',PageUp:'\x1b[5~',PageDown:'\x1b[6~'};
    terminal.attachCustomKeyEventHandler(event=>{
      // Enter commits an IME composition; it must not also submit the prompt.
      if(event.isComposing||event.keyCode===229)return false;
      if(event.altKey||event.metaKey||event.shiftKey)return true;
      const wire=event.ctrlKey?({c:'\x03',p:'\x10'} as Record<string,string>)[event.key.toLowerCase()]:physical[event.key];
      if(wire===undefined)return true;
      if(event.type==='keydown'){event.preventDefault();enqueue(wire)}
      return false;
    });
    let timer:ReturnType<typeof setTimeout>;
    const resize=()=>{clearTimeout(timer);timer=setTimeout(()=>{fit.fit();void api('/'+selected+'/resize',{
      rows:Math.max(2,Math.min(200,terminal.rows)),cols:Math.max(10,Math.min(400,terminal.cols))}).catch(()=>{});},150)};
    const observer=new ResizeObserver(resize);observer.observe(container.current);resize();
    const socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}${prefix}/terminals/${selected}/stream`);
    socket.onopen=()=>socket.send(JSON.stringify({token}));
    socket.onmessage=event=>{const data=JSON.parse(event.data);if(data.gap)terminal.writeln('\r\n[较早输出已超出保留窗口]');
      for(const frame of data.frames){
        renderSize+=frame.text.length;
        if(renderSize>2*1024*1024){blocked=true;renderQueue.length=0;renderSize=0;setNotice('终端输出过快，已断开以限制内存占用；请核对后重新连接。');socket.close();return}
        renderQueue.push({text:frame.text,replay:frame.replay===true});
      }
      renderNext();if(data.closed)setNotice('终端已关闭；屏幕输出不代表任务验收通过。');};
    socket.onerror=()=>setNotice('终端连接失败，请点击重新连接输出。');
    socket.onclose=()=>{if(!disposed){blocked=true;queue.length=0;setNotice('终端输出连接已断开，屏幕键盘已冻结；可重新连接输出。未重发任何输入。')}};
    return()=>{disposed=true;queue.length=0;renderQueue.length=0;if(emulator.current===terminal)emulator.current=null;input.dispose();queries.forEach(q=>q.dispose());clearTimeout(timer);observer.disconnect();socket.onmessage=null;socket.onerror=null;socket.onclose=null;socket.close();osc.dispose();win32.dispose();terminal.dispose()};
  },[selected,prefix,token,streamGeneration]);
  return <section className="panel terminal-panel" aria-label="本机终端"><h2>本机终端</h2>
    <p className="muted">点击屏幕可输入文字，支持方向键、回车、退格、Tab、Esc、Ctrl+C 和 Ctrl+P。回显不会推进任务完成。恢复沿用原工作树，不会重发旧输入。</p>
    {agent==='codex'&&<p role="note">Codex 原生终端仍可能提示会话未就绪（No active thread）。请等待加载完成，遇到此提示先核对输入；开发任务建议使用上方会话卡或任务执行入口。</p>}
    <div className="terminal-actions"><label>终端程序<select aria-label="终端程序" value={agent} onChange={e=>setAgent(e.target.value)}>
      <option value="check">终端自检（不调用模型）</option><option value="codex">Codex</option><option value="opencode">OpenCode</option><option value="claude">Claude</option></select></label>
      <label>启动尝试上限<input aria-label="启动尝试上限" type="number" min={1} max={5} value={startupAttempts} onChange={e=>setStartupAttempts(Number(e.target.value))}/></label>
      <button disabled={busy||paused||startupAttempts<1||startupAttempts>5||!Number.isInteger(startupAttempts)} onClick={()=>void act(()=>start({agent,startup_attempts:startupAttempts}))}>启动终端</button>
      <label>终端会话<select aria-label="终端会话" value={selected} onChange={e=>setSelected(e.target.value)}><option value="">选择会话</option>{sessions.map(s=><option key={s.id} value={s.id}>{s.agent} · {s.id.slice(-6)} · {s.live?'在线':'已关闭'}</option>)}</select></label>
      <button disabled={!session?.live||busy} onClick={()=>void act(async()=>{await api('/'+selected+'/takeover',{owner:'human',expected_epoch:session!.epoch});setNotice('已接管；存在未确认输入时，会话停止并等待对账。')})}>接管终端</button>
      <button disabled={!(session?.can_stop??session?.live)||busy} onClick={()=>void act(async()=>{await api('/'+selected+'/stop',{})})}>关闭终端</button></div>
    <button disabled={!session?.can_resume||busy||paused||startupAttempts<1||startupAttempts>5||!Number.isInteger(startupAttempts)} onClick={()=>void act(()=>start({agent:session!.agent,resume_from:selected,startup_attempts:startupAttempts}))}>{session?.startup_state==='failed'?'再次启动此工作区':'恢复终端'}</button>
    <p className="muted">默认最多启动 3 次，创建失败且清理已确认时也计入同一上限。每次等待启动界面最多 90 秒，重试间隔 2 秒。耗尽后等待你再次启动或恢复；已输入指令或旧进程组未确认停止时不自动重试。</p>
    {session?.startup_state&&<p role="status">启动状态：{({starting:'等待启动界面',retrying:'准备重试',ready:'已显示交互界面',failed:'本次启动已退出，等待手动再次启动',stopped:'已停止',reconciling:'停止状态未确认，需要核查'} as Record<string,string>)[session.startup_state]||session.startup_state} · 第 {session.startup_attempt}/{session.startup_limit} 次{session.startup_error?` · ${session.startup_error}`:''}</p>}
    {session?.startup_diagnostic&&<p role="note">最近创建失败：{({job_create:'建立进程组',input_listener:'建立输入监听',pty_create:'创建伪终端',process_create:'创建进程',output_bridge:'连接输出通道',input_connect:'连接输入通道',input_auth:'验证输入通道',session_attach:'绑定会话'} as Record<string,string>)[session.startup_diagnostic.stage]||'其他创建阶段'} · {session.startup_diagnostic.kind}{session.startup_diagnostic.winerror!==undefined?` · Windows ${session.startup_diagnostic.winerror}`:''} · {session.startup_diagnostic.stopped_confirmed?'已确认资源清理':'资源清理待核对'}</p>}
    <button disabled={!selected||busy} onClick={()=>{setNotice('正在重新连接输出，不会重发输入。');setStreamGeneration(value=>value+1)}}>重新连接输出</button>
    {selected&&<><div ref={container} className="terminal-screen" data-testid="terminal-screen"/>
      <form onSubmit={e=>{e.preventDefault();const sid=selected,epoch=session!.epoch,isCodex=session!.agent==='codex';void act(async()=>{const result=await api('/'+sid+'/paste',{epoch,text,bracketed:emulator.current?.modes.bracketedPasteMode??false});setText('');if(isCodex)await settlePaste(sid,epoch,text,result.draft_confirmation);await api('/'+sid+'/key',{epoch,name:'enter'});setNotice('输入已写入，尚未确认执行。')})}}>
      <label>终端输入<textarea value={text} onChange={e=>setText(e.target.value)} placeholder="输入文本，发送时附加回车"/></label>
      <button disabled={busy||paused||!session?.live||session.owner!=='human'||session.state!=='ready'||!text.trim()}>发送到终端</button></form></>}
    {notice&&<p role="status">{notice}</p>}</section>;
}
