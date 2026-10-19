import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

test('redrawing terminal history does not resend capability replies',async({page})=>{
  const session={id:'terminal_fixture',agent:'check',owner:'human',epoch:0,state:'ready',live:true,can_resume:false};
  let started=false,send:((text:string)=>void)|undefined;
  const replies:string[]=[],keys:string[]=[];
  await page.route('**/terminals**',async route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    if(path.endsWith('/terminals')){
      if(request.method()==='POST'){started=true;await route.fulfill({json:session})}
      else await route.fulfill({json:started?[session]:[]});
    }else if(path.endsWith('/response')){
      replies.push(request.postDataJSON().text);await route.fulfill({json:{state:'sent_unconfirmed'}});
    }else if(path.endsWith('/key')){
      keys.push(request.postDataJSON().name);await route.fulfill({json:{state:'sent_unconfirmed'}});
    }else await route.fulfill({json:{}});
  });
  await page.routeWebSocket('**/terminals/*/stream',socket=>{
    send=text=>socket.send(text);
    socket.onMessage(()=>socket.send(JSON.stringify({frames:[{seq:1,replay:true,text:'HISTORY\r\n\x1b]10;?\x07\x1b[c'}]})));
  });
  await page.goto('/');
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByRole('button',{name:'打开本机终端',exact:true}).click();
  await page.getByRole('button',{name:'启动终端',exact:true}).click();
  await page.getByTestId('terminal-screen').scrollIntoViewIfNeeded();
  await expect(page.locator('.xterm-rows')).toContainText('HISTORY');
  send!(JSON.stringify({frames:[{seq:2,replay:false,text:'\x1b]10;?\x07\x1b[c\r\nLIVE-END'}]}));
  await expect(page.locator('.xterm-rows')).toContainText('LIVE-END');
  await expect.poll(()=>replies.join('')).toContain('\x1b]10;');
  // Wait through the serial HTTP queue to detect any extra historical reply.
  await page.waitForTimeout(250);
  expect((replies.join('').match(/\x1b\]10;/g)||[]).length).toBe(1);
  expect((replies.join('').match(/\x1b\[\?[0-9;]+c/g)||[]).length).toBe(1);
  await expect(page.getByRole('button',{name:'接管终端',exact:true})).toBeEnabled();
  await page.getByRole('textbox',{name:'Terminal input',exact:true}).press('Control+p');
  await expect.poll(()=>keys.filter(key=>key==='ctrl_p').length).toBe(1);
  await page.waitForTimeout(250);
  expect(keys.filter(key=>key==='ctrl_p')).toHaveLength(1);
});
