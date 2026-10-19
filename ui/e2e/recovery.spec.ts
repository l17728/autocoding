import {test,expect} from '@playwright/test';

for (const scenario of ['normal','missing','orphan']) {
test('recovery requires evidence and explicit Owner attestation: '+scenario,async({page})=>{
  let resolved=false,body:any;
  const project={id:'project_recovery',name:'恢复测试',root:'D:/fixture'};
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname;let json:any={};
    if(path.endsWith('/project'))json=project;
    else if(path.endsWith('/projects'))json=[project];
    else if(path.endsWith('/settings'))json={...project,revision:1};
    else if(path.endsWith('/snapshot'))json={tasks:scenario==='orphan'&&!resolved?[{id:'task_a',state:'blocked',description:'孤立任务',attempt:1}]:[],sessions:[],approvals:[],latest_seq:1,paused:false,live_ids:[],questions:[],batches:[],
      runs:resolved||scenario==='orphan'?[]:[{id:'run_a',task_id:'task_a',state:'reconciling',reason:'RUN_TIMEOUT'}]};
    else if(path.endsWith('/output'))json={latest_seq:1,events:[]};
    else if(path.endsWith('/reviews'))json={settings:{enabled:true,interval_minutes:60},reviews:[],principles:[],knowledge:[],knowledge_total:0,health:{}};
    else if(path.endsWith('/interactions'))json={total:0,items:[]};
    else if(path.endsWith('/closures')||path.endsWith('/terminals'))json=[];
    else if(path.endsWith('/recovery')){
      if(route.request().method()==='POST'){body=route.request().postDataJSON();resolved=true;json={state:'todo'}}
      else json={run:{id:scenario==='orphan'?'orphan:task_a':'run_a',task_id:'task_a'},task:{revision:4,attempt:1},session:{id:'s',epoch:2,generation:1},
        workspace:{path:'D:/old-worktree',head:'abc',status:' M code.py'},source_head:'base',commands:[{id:'c',state:'delivery_unknown'}],
        questions:[],approvals:[],fingerprint:'a'.repeat(64),unavailable_reason:null,missing_workspace:scenario==='missing',orphan:scenario==='orphan'};
    }
    await route.fulfill({json});
  });
  await page.goto('/');await page.getByLabel('访问令牌',{exact:true}).fill('fixture-token');
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByText('异常对账与恢复',{exact:true}).click();
  await page.getByRole('button',{name:'核对恢复证据'}).click();
  const submit=page.getByRole('button',{name:'归档旧尝试并允许重新执行'});
  await expect(submit).toBeDisabled();
  await expect(page.getByText(/命令（原始结果保留）/)).toContainText('delivery_unknown');
  await page.getByLabel('核查结论').fill('已核对停止和副作用，允许重新开发');
  await expect(submit).toBeDisabled();
  await page.getByRole('checkbox',{name:/我已核查旧执行/}).check();
  if(scenario==='missing'){
    await expect(submit).toBeDisabled();
    await page.getByRole('checkbox',{name:/旧工作树不可用/}).check();
  }
  if(scenario==='orphan')await expect(page.getByRole('alert')).toContainText('此任务缺少运行记录');
  await submit.click();
  await expect(page.getByRole('status')).toContainText('旧尝试已归档');
  expect(body).toEqual({fingerprint:'a'.repeat(64),note:'已核对停止和副作用，允许重新开发',stopped:true,...(scenario==='missing'?{accept_missing_workspace:true}:{})});
});
}
