import {test,expect} from '@playwright/test';

test('acceptance editor preserves draft on conflict and shows effective version',async({page})=>{
  const project={id:'project_policy',name:'验收配置测试',root:'D:/fixture'};
  const config={checks:{unit:['C:/Python312/python.exe','test.py']},protected_paths:['test.py']};
  let conflict=true,reads=0;
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname;let json:any={};
    if(path.endsWith('/project'))json=project;
    else if(path.endsWith('/projects'))json=[project];
    else if(path.endsWith('/settings'))json={...project,revision:1};
    else if(path.endsWith('/snapshot'))json={tasks:[],sessions:[],approvals:[],latest_seq:0,paused:false,live_ids:[],questions:[],batches:[],runs:[]};
    else if(path.endsWith('/output'))json={latest_seq:0,events:[]};
    else if(path.endsWith('/reviews'))json={settings:{enabled:true,interval_minutes:60},reviews:[],principles:[],knowledge:[],knowledge_total:0,health:{}};
    else if(path.endsWith('/interactions'))json={total:0,items:[]};
    else if(path.endsWith('/closures')||path.endsWith('/terminals'))json=[];
    else if(path.endsWith('/acceptance')){
      if(route.request().method()==='GET'){reads++;json={revision:reads-1,source:'file',fingerprint:'a'.repeat(64),config,active_tasks:['task_running'],uncovered_tasks:['task_missing']}}
      else {
        if(conflict){await route.fulfill({status:409,json:{error:'ACCEPTANCE_VERSION_CONFLICT'}});return}
        json={revision:2,source:'database',fingerprint:'b'.repeat(64),config:route.request().postDataJSON().config};
      }
    }
    await route.fulfill({json});
  });
  await page.goto('/');await page.getByLabel('访问令牌',{exact:true}).fill('fixture-token');
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByText('验收配置管理',{exact:true}).click();
  await page.getByRole('button',{name:'载入验收配置',exact:true}).click();
  await expect(page.getByText(/进行中任务：task_running/)).toBeVisible();
  const draft=JSON.stringify({...config,protected_paths:['tests/']},null,2);
  await page.getByLabel('验收配置 JSON').fill(draft);
  await page.getByRole('button',{name:'校验并保存验收配置'}).click();
  await expect(page.getByRole('status')).toContainText('编辑内容已保留');
  await expect(page.getByLabel('验收配置 JSON')).toHaveValue(draft);
  conflict=false;
  await page.getByRole('button',{name:'重新载入（覆盖编辑内容）'}).click();
  await page.getByRole('button',{name:'校验并保存验收配置'}).click();
  await expect(page.getByText('版本 2 · 来源 项目数据库')).toBeVisible();
  await expect(page.getByRole('status')).toContainText('验收配置已保存');
});
