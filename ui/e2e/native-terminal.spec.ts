import {test,expect} from '@playwright/test';
import {mkdirSync,writeFileSync,readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {execFileSync} from 'node:child_process';
import {createHash,randomUUID} from 'node:crypto';

// Owner acceptance: at most three independent attempts; one complete pass
// accepts the case. Playwright preserves failed attempts as flaky evidence.
test.describe.configure({retries:process.env.AUTOCODING_NATIVE_SINGLE_ATTEMPT==='1'?0:2});

for(const agent of ['codex','claude','opencode']) {
  test('native TUI startup and terminal responses: '+agent,async({page},testInfo)=>{
    test.skip(process.env.AUTOCODING_NATIVE_TUI!=='1','Explicit native startup matrix; no model prompts');
    const modelResume=agent==='codex'&&process.env.AUTOCODING_NATIVE_CODEX_MODEL==='1';
    const approvals=agent==='codex'&&process.env.AUTOCODING_NATIVE_CODEX_APPROVALS==='1';
    const longPaste=agent==='codex'&&process.env.AUTOCODING_NATIVE_LONG_PASTE==='1';
    const interactions=process.env.AUTOCODING_NATIVE_INTERACTIONS==='1';
    test.setTimeout(longPaste?600000:modelResume||approvals?300000:150000);
    const started=Date.now();
    const root=resolve('../.local/native-browser-'+agent+'-'+Date.now());
    mkdirSync(root,{recursive:true});writeFileSync(root+'/README.md','Native terminal fixture\n');
    if(approvals){
      mkdirSync(root+'/.codex/rules',{recursive:true});
      writeFileSync(root+'/.codex/config.toml','# Isolated approval acceptance fixture\napprovals_reviewer = "user"\n');
      writeFileSync(root+'/.codex/rules/native-approval.rules','prefix_rule(pattern=["Set-Content"], decision="prompt", justification="Confirm the isolated acceptance file write")\n');
    }
    const git=(...args:string[])=>execFileSync('git',['-c','core.hooksPath=NUL','-C',root,...args],{encoding:'utf8'});
    git('init');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','commit','-m','fixture');
    const token=readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf8');
    const registered=await page.request.post('/api/v1/projects',{headers:{Authorization:'Bearer '+token,'Idempotency-Key':randomUUID()},data:{name:'Native '+agent,root,initialize_git:false,permission_mode:approvals?'review':'bypass'}});
    expect(registered.status()).toBe(200);
    const project=await registered.json();
    let output='',responses=0,modelPrompts=0,contextRetained=false;
    const interactionChecks:string[]=[];
    // Newer Codex releases use a model/path footer instead of "context left".
    // Actual approval/file checks below remain the execution proof.
    const codexReady=/permissions:|context left|GPT-[\w.-]+\s+(?:minimal|low|medium|high|xhigh|default)/i;
    const turnSeconds:number[]=[];
    const replies:string[]=[];
    const errors:string[]=[];
    const lifecycle:unknown[]=[];
    // Record only path, method and status; never credentials or request bodies.
    page.on('request',r=>{const path=new URL(r.url()).pathname;if(path.includes('/terminals'))lifecycle.push({at_ms:Date.now()-started,event:'request',path,method:r.method()})});
    page.on('response',r=>{const path=new URL(r.url()).pathname;if(path.includes('/terminals'))lifecycle.push({at_ms:Date.now()-started,event:'response',path,status:r.status()})});
    page.on('pageerror',error=>errors.push(error.message));
    page.on('websocket',socket=>socket.on('framereceived',frame=>{const value=JSON.parse(String(frame.payload));output+=(value.frames||[]).map((f:any)=>f.text).join('')}));
    page.on('response',r=>{if(r.url().endsWith('/response')&&r.status()===200){responses++;replies.push(r.request().postDataJSON().text)}});
    await page.goto('/');await page.getByLabel('访问令牌',{exact:true}).fill(token);await page.getByRole('button',{name:'进入工作台'}).click();
    await page.locator('.project-panel > summary').click();await page.getByLabel('切换项目',{exact:true}).selectOption(project.id);
    await page.getByRole('button',{name:'打开本机终端',exact:true}).click();
    await page.getByLabel('终端程序',{exact:true}).selectOption(agent);
    // Playwright owns the three independent attempts; do not multiply them
    // by the product's internal three-attempt startup recovery.
    await page.getByLabel('启动尝试上限',{exact:true}).fill('1');
    try {
      await page.getByRole('button',{name:'启动终端',exact:true}).click();
      // xterm suspends painting outside the viewport. Inspect a visible screen,
      // not the empty DOM renderer of an off-screen terminal below the controls.
      await page.getByTestId('terminal-screen').scrollIntoViewIfNeeded({timeout:30000});
      // Match the existing 90-second isolated OpenCode cold-start budget.
      if(agent==='opencode')await expect(page.locator('.xterm-rows')).toContainText(/Ask anything|What would you like/,{timeout:90000});
      if(agent==='claude')await expect(page.locator('.xterm-rows')).toContainText(/Choose the text style|Choose the theme|Dark mode|Light mode/,{timeout:90000});
      if(agent==='codex'){
        await expect(page.locator('.xterm-rows')).toContainText(/OpenAI Codex|Trust this folder\?/,{timeout:90000});
      }
      await expect(page.getByRole('button',{name:'关闭终端',exact:true})).toBeEnabled();
      if(modelResume||approvals||longPaste||interactions&&agent==='codex'){
        // A fresh disposable repository may show trust after the initial loading
        // screen. Resolve that real menu before sending any model instruction.
        await expect(page.locator('.xterm-rows')).toContainText(/Trust this folder\?|permissions:|context left|GPT-[\w.-]+\s+(?:minimal|low|medium|high|xhigh|default)/i,{timeout:60000});
        if((await page.locator('.xterm-rows').innerText()).includes('Trust this folder?')){
          await page.getByRole('textbox',{name:'Terminal input',exact:true}).press('Enter');
          await expect(page.locator('.xterm-rows')).not.toContainText('Trust this folder?',{timeout:30000});
        }
        await expect(page.locator('.xterm-rows')).toContainText(codexReady,{timeout:30000});
        await expect(page.locator('.xterm-rows')).not.toContainText('No active thread is available');
      }
      if(longPaste){
        let submits=0;
        const countSubmit=(request:any)=>{if(request.method()==='POST'&&request.url().endsWith('/key')&&request.postDataJSON()?.name==='enter')submits++};
        page.on('request',countSubmit);
        const oversized=process.env.AUTOCODING_NATIVE_OVERSIZED_PASTE==='1';
        const expected=Array.from({length:oversized?24:3},(_,i)=>`Line ${i+1}: `+'中文😀 exact spaces  preserved '.repeat(4)+randomUUID().slice(0,8)).join('\n');
        const prompt='Write ONLY the exact text between BEGIN and END into native-long.txt in UTF-8. Preserve all spaces, Chinese, emoji and line breaks. Do not include the delimiters. Use [IO.File]::WriteAllText with [Text.UTF8Encoding]::new($false), which works in Windows PowerShell. Wait for command completion (poll any running session), then read the file back and verify it before replying DONE.\nBEGIN\n'+expected+'\nEND';
        await page.getByLabel('终端输入',{exact:true}).fill(prompt);
        const pasteResponse=page.waitForResponse(r=>r.url().endsWith('/paste')&&r.request().method()==='POST',{timeout:150000});
        await page.getByRole('button',{name:'发送到终端',exact:true}).click();modelPrompts++;
        const pasted=await pasteResponse;
        expect(pasted.ok()).toBe(true);
        const proof=(await pasted.json()).draft_confirmation;
        expect(proof).toEqual({method:'editor_readback',sha256:createHash('sha256').update(prompt).digest('hex')});
        writeFileSync(root+'/draft-readback-evidence.json',JSON.stringify({characters:prompt.length,lines:expected.split('\n').length,...proof},null,2));
        await expect(page.locator('.terminal-panel')).toContainText(/输入已写入，尚未确认执行。|Error: /,{timeout:330000});
        await expect(page.locator('.terminal-panel')).not.toContainText('尚未确认完整粘贴回显');
        await expect(page.locator('.terminal-panel')).toContainText('输入已写入，尚未确认执行。');
        const sessions=await (await page.request.get(`/api/v1/projects/${project.id}/terminals`,{headers:{Authorization:'Bearer '+token}})).json();
        const cwd=sessions.find((s:any)=>s.agent==='codex').cwd;
        await expect.poll(()=>{try{return readFileSync(resolve(cwd,'native-long.txt'),'utf8').replace(/^\uFEFF/,'').replace(/\r\n/g,'\n').replace(/\n$/,'')}catch{return ''}},{timeout:oversized?180000:90000}).toBe(expected);
        expect(submits).toBe(1);
        writeFileSync(root+'/long-input-evidence.json',JSON.stringify({expected_sha256:createHash('sha256').update(expected).digest('hex'),actual_sha256:createHash('sha256').update(readFileSync(resolve(cwd,'native-long.txt'),'utf8').replace(/^\uFEFF/,'').replace(/\r\n/g,'\n').replace(/\n$/,'')).digest('hex'),submit_count:submits,characters:prompt.length},null,2));
        await expect(page.locator('.xterm-rows')).toContainText(/•\s*DONE/,{timeout:180000});
        page.off('request',countSubmit);
        interactionChecks.push('long_multiline_unicode_exact_file');
      }
      if(approvals){
        const suffix=randomUUID().slice(0,8),expected='APPROVAL_'+suffix;
        // Keep this approval probe independent of the separate long-paste case.
        await page.getByLabel('终端输入',{exact:true}).fill(`Run this exact PowerShell command, then reply DONE: Set-Content native-approval.txt ${expected}`);
        await page.getByRole('button',{name:'发送到终端',exact:true}).click();modelPrompts++;
        await expect(page.locator('.xterm-rows')).toContainText(/Would you like to run|Do you want to run|Approve this command/,{timeout:90000});
        await page.getByRole('textbox',{name:'Terminal input',exact:true}).press('Enter');
        const sessions=await (await page.request.get(`/api/v1/projects/${project.id}/terminals`,{headers:{Authorization:'Bearer '+token}})).json();
        const cwd=sessions.find((s:any)=>s.agent==='codex').cwd;
        await expect.poll(()=>{try{return readFileSync(resolve(cwd,'native-approval.txt'),'utf8').trim()}catch{return ''}},{timeout:90000}).toBe(expected);
        await expect(page.locator('.xterm-rows')).toContainText(/•\s*DONE/,{timeout:30000});
        interactionChecks.push('explicit_command_approval_and_verified_isolated_file_write');
      }
      if(interactions){
        const keyboard=page.getByRole('textbox',{name:'Terminal input',exact:true});
        if(agent==='claude'){
          await keyboard.press('ArrowDown');
          await keyboard.press('Enter');
          await expect(page.locator('.xterm-rows')).not.toContainText(/Choose the text style|Choose the theme/);
          await expect(page.locator('.xterm-rows')).toContainText(/Security notes|login|log in|sign in|account|Console/i);
          interactionChecks.push('theme_selection_to_next_onboarding_screen_no_login');
        }else{
          if(agent==='opencode')await keyboard.press('Control+p');
          else await keyboard.pressSequentially('/');
          // OpenCode's ordinary footer also says "commands"; require the
          // capitalized palette title, never that persistent shortcut hint.
          await expect(page.locator('.xterm-rows')).toContainText(agent==='opencode'?/Commands/:/\/permissions/);
          await keyboard.press('ArrowDown');
          await keyboard.press('Escape');
          await expect(page.locator('.xterm-rows')).not.toContainText(agent==='opencode'?/Commands/:/\/permissions/);
          if(agent==='codex')await keyboard.press('Backspace');
          interactionChecks.push('menu_open_navigation_escape');
        }
      }
      if(modelResume){
        const marker='CONTEXT_'+randomUUID().replaceAll('-',''),ack='ACK_'+randomUUID().slice(0,8);
        await page.getByLabel('终端输入',{exact:true}).fill(`Do not use tools or files. Remember this conversation marker: ${marker}. Reply exactly ${ack}.`);
        let turnStarted=Date.now();
        await page.getByRole('button',{name:'发送到终端',exact:true}).click();modelPrompts++;
        await expect(page.locator('.xterm-rows')).toContainText(new RegExp('•\\s*'+ack),{timeout:90000});
        turnSeconds.push((Date.now()-turnStarted)/1000);
        const previousScreen=await page.locator('.xterm').elementHandle();
        await expect(page.getByRole('button',{name:'关闭终端',exact:true})).toBeEnabled();
        await page.getByRole('button',{name:'关闭终端',exact:true}).click();
        await expect(page.getByRole('button',{name:'恢复终端',exact:true})).toBeEnabled();
        await page.getByRole('button',{name:'恢复终端',exact:true}).click();
        // The old screen can satisfy readiness while React replaces the TUI.
        // First observe the actual terminal instance change, then its readiness.
        await expect.poll(()=>previousScreen!.evaluate(node=>node.isConnected).catch(()=>false)).toBe(false);
        await page.getByTestId('terminal-screen').scrollIntoViewIfNeeded();
        await expect(page.locator('.xterm-rows')).toContainText('OpenAI Codex',{timeout:45000});
        await expect(page.locator('.xterm-rows')).toContainText(codexReady,{timeout:60000});
        await expect(page.locator('.xterm-rows')).not.toContainText('Resuming session',{timeout:30000});
        await expect(page.getByRole('button',{name:'关闭终端',exact:true})).toBeEnabled();
        await page.getByLabel('终端输入',{exact:true}).fill('Do not use tools or files. Return the conversation marker I gave you earlier with every character in reverse order. Reply only with that reversed string.');
        turnStarted=Date.now();
        await page.getByRole('button',{name:'发送到终端',exact:true}).click();modelPrompts++;
        // The reversed marker appears in neither prompt nor replayed history.
        await expect(page.locator('.xterm-rows')).toContainText(marker.split('').reverse().join(''),{timeout:90000});
        turnSeconds.push((Date.now()-turnStarted)/1000);
        contextRetained=true;
      }
      if(interactions){
        const takeoverResponse=page.waitForResponse(response=>response.url().endsWith('/takeover')&&response.request().method()==='POST');
        await page.getByRole('button',{name:'接管终端',exact:true}).click();
        const taken=await takeoverResponse;
        expect(taken.status()).toBe(200);
        const takeover=await taken.json();
        expect(takeover.owner).toBe('human');
        expect(takeover.epoch).toBe(taken.request().postDataJSON().expected_epoch+1);
        // Keyboard writes are unconfirmed at the ledger level. Taking over
        // must freeze them, even when the socket-close notice replaces success.
        expect(takeover.state).toBe('reconciling');
        await expect(page.getByRole('button',{name:'恢复终端',exact:true})).toBeEnabled();
        interactionChecks.push('human_takeover');
      }
      expect(errors).toEqual([]);
      await page.screenshot({path:root+'/startup.png',fullPage:true});
      writeFileSync(root+'/report.json',JSON.stringify({agent,scope:longPaste?'native_long_paste':approvals?'native_command_approval':modelResume?'native_context_resume':interactions?'native_local_interactions':'native_startup_only',interaction_checks:interactionChecks,model_prompts_sent:modelPrompts,context_retained:contextRetained,turn_seconds:turnSeconds,responses,output_sha256:createHash('sha256').update(output).digest('hex'),full_interaction_matrix_passed:false},null,2));
    } finally {
      const notice=await page.locator('.terminal-panel').innerText({timeout:2000}).catch(()=>'[terminal snapshot unavailable]');
      const terminalRecords=await page.request.get(`/api/v1/projects/${project.id}/terminals`,{headers:{Authorization:'Bearer '+token},timeout:3000})
        .then(r=>r.json()).then(rows=>rows.map((r:any)=>({id:r.id,agent:r.agent,startup_state:r.startup_state,startup_timings:r.startup_timings}))).catch(()=>[]);
      writeFileSync(root+'/diagnostics.json',JSON.stringify({attempt:testInfo.retry+1,max_attempts:process.env.AUTOCODING_NATIVE_SINGLE_ATTEMPT==='1'?1:3,lifecycle,terminal_records:terminalRecords,responses,replies,errors,model_prompts_attempted:modelPrompts,context_retained:contextRetained,notice,redacted_startup:output.slice(0,2048),redacted_tail:output.slice(-4096),
        queries:[...new Set(output.match(/\x1b\[[0-?]*[ -/]*[ncqput]/g)||[])].slice(0,40)},null,2));
      const close=page.getByRole('button',{name:'关闭终端',exact:true});
        if(await close.isEnabled({timeout:2000}).catch(()=>false))await close.click({timeout:10000});
    }
    await expect(page.getByRole('button',{name:'恢复终端',exact:true})).toBeEnabled();
  });
}
