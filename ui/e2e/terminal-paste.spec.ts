import {test,expect} from '@playwright/test';
import type {Terminal} from '@xterm/xterm';
import {pasteProgress,PasteDeadline} from '../src/terminalPaste';
import {resolve} from 'node:path';

function screen(lines:string[]){
  return {rows:lines.length,buffer:{active:{baseY:0,getLine:(row:number)=>({isWrapped:false,translateToString:()=>lines[row]||''})}}} as unknown as Terminal;
}
test('paste confirmation follows manually drawn wraps and retains internal spaces',()=>{
  const text='Chinese 中文😀 with spaces '.repeat(25)+'END';
  const pieces=text.match(/.{1,47}/gu)!;
  expect(pasteProgress(screen(pieces.map((s,i)=>(i?'  ':'› ')+s)),text)).toBe(text.length);
  expect(pasteProgress(screen(['› a b']),'ab')).toBe(0);
  expect(pasteProgress(screen(['› ab']),'a b')).toBe(0);
  expect(pasteProgress(screen(['› hello extra']),'hello')).toBe(0);
  expect(pasteProgress(screen(['› hello','response','› different']),'hello')).toBe(0);
  expect(pasteProgress(screen(['› first','  中文😀','  last']),'first\n中文😀\nlast')).toBe('first\n中文😀\nlast'.length);
  expect(pasteProgress(screen(['› partial']),'partial remaining')).toBe(7);
});
test('paste waiting requires progress and always has an absolute bound',()=>{
  const progressing=new PasteDeadline(0);
  for(let i=1;i<=5;i++)expect(progressing.observe(i,50000*i)).toBe(true);
  expect(progressing.observe(6,300000)).toBe(false);
  const stuck=new PasteDeadline(0);
  expect(stuck.observe(4,1000)).toBe(true);
  expect(stuck.observe(4,61000)).toBe(false);
  expect(new PasteDeadline(0).observe(0,60000)).toBe(false);
});

test('actual xterm emoji width preserves spaces in absolute-position TUI redraws',async({page})=>{
  await page.setContent('<div id="terminal"></div>');
  await page.addScriptTag({path:resolve('node_modules/@xterm/xterm/lib/xterm.js')});
  await page.addScriptTag({path:resolve('node_modules/@xterm/addon-unicode11/lib/addon-unicode11.js')});
  const line=await page.evaluate(async()=>{
    const scope=window as any;
    const terminal=new scope.Terminal({allowProposedApi:true,cols:80,rows:24});
    terminal.loadAddon(new scope.Unicode11Addon.Unicode11Addon());
    terminal.unicode.activeVersion='11';terminal.open(document.getElementById('terminal'));
    await new Promise<void>(done=>terminal.write('› 中文😀\x1b[1;10Hexact  spaces',done));
    const text=terminal.buffer.active.getLine(0).translateToString(true);
    terminal.dispose();return text;
  });
  expect(line).toBe('› 中文😀 exact  spaces');
});
