import {test,expect} from '@playwright/test';
import {readFileSync,writeFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {randomUUID} from 'node:crypto';

test('Windows IME composes Chinese and composition Enter does not submit',async({page})=>{
  test.skip(process.env.AUTOCODING_OS_IME!=='1','Requires a headed foreground Windows test window');
  test.setTimeout(90000);
  const title='autocoding-ime-'+randomUUID(),keys:string[]=[],input:string[]=[];
  let output='';
  page.on('request',request=>{
    const path=new URL(request.url()).pathname;
    if(!path.includes('/terminals/'))return;
    if(path.endsWith('/key'))keys.push(request.postDataJSON().name);
    if(path.endsWith('/input'))input.push(request.postDataJSON().text);
  });
  page.on('websocket',socket=>socket.on('framereceived',event=>{
    const message=JSON.parse(String(event.payload));
    output+=(message.frames||[]).map((frame:{text:string})=>frame.text).join('');
  }));
  await page.goto('/');
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByRole('button',{name:'打开本机终端',exact:true}).click();
  await page.getByRole('button',{name:'启动终端',exact:true}).click();
  await expect.poll(()=>output,{timeout:30000}).toContain('TERMINAL_READY');
  // Output can arrive before the start action has finished refreshing state.
  await expect(page.getByRole('button',{name:'接管终端',exact:true})).toBeEnabled();
  await page.evaluate(value=>{document.title=value},title);
  await page.bringToFront();
  await page.getByRole('textbox',{name:'Terminal input',exact:true}).click();
  const send=(action:string)=>execFileSync('../.venv/Scripts/python.exe', ['../tools/windows_ime_input.py','--title',title,'--action',action], {timeout:15000,encoding:'utf8'});
  try{
  send('chinese-space');
  await expect.poll(()=>input.join('')).toMatch(/[\u4e00-\u9fff]/);
  expect(keys.filter(key=>key==='enter')).toHaveLength(0);
  const before=input.join('').length;
  send('composition-enter');
  await expect.poll(()=>input.join('').length).toBeGreaterThan(before);
  expect(keys.filter(key=>key==='enter')).toHaveLength(0);
  send('enter');
  await expect.poll(()=>keys.filter(key=>key==='enter').length).toBe(1);
  await expect.poll(()=>output,{timeout:15000}).toContain('ECHO:'+input.join(''));
  }finally{
    const close=page.getByRole('button',{name:'关闭终端',exact:true});
    if(await close.isEnabled())await close.click();
  }
  writeFileSync('../.local/os-ime-'+Date.now()+'.json',JSON.stringify({passed:true,real_windows_ime:true,
    ascii_scan_codes_only:true,chinese_composed:true,composition_enter_not_submitted:true,subsequent_enter_count:1,
    backend:'real authenticated terminal HTTP, ConPTY, browser and Windows IME',echo_verified:true},null,2));
});
