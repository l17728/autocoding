import type {Terminal} from '@xterm/xterm';

export async function verifiedDraft(value:string,receipt:unknown):Promise<boolean>{
  if(!receipt||typeof receipt!=='object')return false;
  const proof=receipt as {method?:unknown;sha256?:unknown};
  if(proof.method!=='editor_readback'||typeof proof.sha256!=='string')return false;
  const bytes=new TextEncoder().encode(value.replace(/\r\n?/g,'\n'));
  const digest=await crypto.subtle.digest('SHA-256',bytes);
  return proof.sha256===Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
}

// Codex redraws wrapped editor rows with absolute cursor positions, so xterm's
// isWrapped flag need not be set. Only inspect the latest visible prompt.
export function pasteProgress(terminal:Terminal,value:string):number{
  const buffer=terminal.buffer.active;
  const expected=value.replace(/\r\n?/g,'\n');
  let start=-1;
  for(let row=buffer.baseY;row<buffer.baseY+terminal.rows;row++){
    if(/^\s*›(?: |$)/.test(buffer.getLine(row)?.translateToString(true)||''))start=row;
  }
  if(start<0||!expected.length)return 0;
  let consumed=0;
  for(let row=start;row<buffer.baseY+terminal.rows;row++){
    const raw=buffer.getLine(row)?.translateToString(true)||'';
    // Prompt and continuation have the same two-column gutter. Do not erase
    // internal spaces: "a b" must never confirm "ab" or a different command.
    const fragment=row===start?raw.replace(/^\s*›(?: |$)/,''):raw.startsWith('  ')?raw.slice(2):raw;
    if(row>start){
      if(!fragment&&!expected.slice(consumed).startsWith('\n'))break;
      // Physical row boundaries can represent a soft wrap or a source newline.
      // Preserve whitespace everywhere else, including inside each row.
      if(!expected.startsWith(fragment,consumed)){
        while(consumed<expected.length&&/\s/.test(expected[consumed]))consumed++;
      }else if(expected[consumed]==='\n')consumed++;
    }
    if(!expected.startsWith(fragment,consumed))return 0;
    consumed+=fragment.length;
    if(consumed===expected.length)return consumed;
  }
  return consumed;
}

export class PasteDeadline{
  readonly started:number;
  private lastProgress:number;
  private progress=0;
  constructor(now:number){this.started=this.lastProgress=now}
  observe(progress:number,now:number){
    if(progress>this.progress){this.progress=progress;this.lastProgress=now}
    // A slow native consumer may take over a minute, but neither output noise
    // nor a stuck partial prompt may extend this operation indefinitely.
    return now-this.started<300000&&now-this.lastProgress<60000;
  }
}
