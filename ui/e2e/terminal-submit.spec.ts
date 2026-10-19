import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

for(const scenario of ['normal','restored-cursor','suffix-only'])test('Codex form waits for full paste echo before one Enter: '+scenario,async({page})=>{
  const restoreCursor=scenario==='restored-cursor',value=scenario==='suffix-only'?'required-prefix-'+ 'x'.repeat(80):'完整输入';
  const partial=scenario==='suffix-only'?'x'.repeat(80):'完整';
  const session={id:'terminal_echo',agent:'codex',owner:'human',epoch:0,state:'ready',live:true,can_resume:false};
  let started=false,send:((text:string)=>void)|undefined,pasted=false;
  const keys:string[]=[];
  await page.route('**/terminals**',async route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    if(path.endsWith('/terminals')){
      if(request.method()==='POST'){
        expect(request.postDataJSON().startup_attempts).toBe(4);
        started=true;
      }
      await route.fulfill({json:request.method()==='POST'?session:started?[session]:[]});
    }else{
      if(path.endsWith('/paste'))pasted=true;
      if(path.endsWith('/key'))keys.push(request.postDataJSON().name);
      await route.fulfill({json:{state:'sent_unconfirmed'}});
    }
  });
  await page.routeWebSocket('**/terminals/*/stream',socket=>{
    send=text=>socket.send(text);
    socket.onMessage(()=>socket.send(JSON.stringify({frames:[{seq:1,replay:true,text:'READY\r\n› '+value+'\r\nold answer\r\n› '}]})));
  });
  await page.goto('/');
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByRole('button',{name:'打开本机终端',exact:true}).click();
  await page.getByLabel('启动尝试上限',{exact:true}).fill('6');
  await expect(page.getByRole('button',{name:'启动终端',exact:true})).toBeDisabled();
  await page.getByLabel('启动尝试上限',{exact:true}).fill('4');
  await page.getByRole('button',{name:'启动终端',exact:true}).click();
  await page.getByTestId('terminal-screen').scrollIntoViewIfNeeded();
  await expect(page.locator('.xterm-rows')).toContainText('READY');
  await page.getByLabel('终端输入',{exact:true}).fill(value);
  await page.getByRole('button',{name:'发送到终端',exact:true}).click();
  await expect.poll(()=>pasted).toBe(true);
  send!(JSON.stringify({frames:[{seq:2,replay:false,text:partial}]}));
  await expect(page.locator('.xterm-rows')).toContainText(partial);
  await page.waitForTimeout(700);
  expect(keys).toEqual([]);
  send!(JSON.stringify({frames:[{seq:3,replay:false,text:'\r\x1b[2K› '+value+(restoreCursor?'\x1b[1;1H':'')}]}));
  await expect.poll(()=>keys).toEqual(['enter']);
  await expect(page.getByRole('button',{name:'发送到终端',exact:true})).toBeDisabled();
});
