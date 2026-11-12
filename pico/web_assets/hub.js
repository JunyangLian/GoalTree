/* Workspace-pinned extension browser. Opening it never installs or probes. */
(function(root){
  'use strict';
  const keyOf=s=>[s.workspaceId||'',s.goalId||'',s.sessionId||''].join('|');
  const matches=(row,q)=>!q||[row.title,row.id,row.description,row.source?.author,row.source?.url,row.origin].join(' ').toLowerCase().includes(q.toLowerCase());
  const sourceName=row=>row.origin==='learned'?'自动学习':row.origin==='workspace'?'工作区':row.source?.author==='obra/superpowers'?'Superpowers · 精简适配':'GoalTree';
  function install(options){
    const $=id=>document.getElementById(id),dialog=$('extensions-dialog');if(!dialog)return;
    const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
    let data=null,tab='plugins',epoch=0,scopeKey='',pending=false,viewEpoch=0,enabledOnly=false;
    const scope=()=>({...options.scope()});
    const status=text=>{$('hub-status').textContent=text;};
    const request=(path,body,pin)=>options.api(path,body,{workspaceId:pin.workspaceId,csrf:pin.csrf});
    const query=pin=>new URLSearchParams({goal_id:pin.goalId||'',session_id:pin.sessionId||''}).toString();
    const isCurrent=(mine,pin)=>mine===epoch&&keyOf(scope())===keyOf(pin)&&dialog.open;
    function link(url,label){
      try {const u=new URL(url);if(u.protocol!=='https:')return el('span','hub-meta',label);}
      catch{return el('span','hub-meta',label);}
      const a=el('a','hub-source',label);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;
    }
    async function load(){
      const pin=scope(),mine=++epoch;viewEpoch++;scopeKey=keyOf(pin);pending=true;status('正在读取当前工作区…');
      $('hub-detail').classList.add('hidden');$('hub-list').classList.remove('hidden');$('hub-list').replaceChildren();data=null;
      try {const value=await request('/api/extensions?'+query(pin),undefined,pin);if(!isCurrent(mine,pin))return;
        data=value;$('hub-workspace').textContent=value.workspace_name;status('');pending=false;render();}
      catch(error){if(isCurrent(mine,pin)){pending=false;status(error.message);}}
    }
    async function toggle(group,id,enabled){
      if(pending||scope().busy)return status('请等待当前操作结束后调整配置。');
      const pin=scope(),mine=epoch,revision=data.revision;pending=true;render();status('正在保存…');
      try {const value=await request('/api/extensions',{group,id,enabled,revision,goal_id:pin.goalId||'',session_id:pin.sessionId||''},pin);
        if(!isCurrent(mine,pin))return;data=value;status('已应用到此工作区。');}
      catch(error){if(isCurrent(mine,pin))status(error.message);}
      finally{if(isCurrent(mine,pin)){pending=false;render();}}
    }
    function button(text,action,disabled=false){const b=el('button','hub-action',text);b.type='button';b.disabled=disabled;b.onclick=action;return b;}
    function toggleButton(group,id,enabled,blocked){const b=button(enabled?'已启用':'启用',()=>toggle(group,id,!enabled),pending||scope().busy||blocked);
      b.classList.toggle('enabled',enabled);b.setAttribute('role','switch');b.setAttribute('aria-checked',String(enabled));return b;}
    async function detail(kind,id){
      const pin=scope(),mine=epoch,view=++viewEpoch;status('正在读取说明…');
      try {const value=await request('/api/extensions/detail?'+query(pin)+'&kind='+encodeURIComponent(kind)+'&id='+encodeURIComponent(id),undefined,pin);
        if(!isCurrent(mine,pin)||view!==viewEpoch)return;
        const box=$('hub-detail');box.replaceChildren();
        box.append(button('返回列表',()=>{viewEpoch++;box.classList.add('hidden');$('hub-list').classList.remove('hidden');}));
        box.append(el('h2','',value.title));
        const content=el(kind==='mcp'?'pre':'div',kind==='mcp'?'hub-config':'markdown-body');
        if(kind==='skill'&&root.GoalTreeMarkdown)content.innerHTML=root.GoalTreeMarkdown.render(value.content);
        else content.textContent=value.content;
        box.append(content);if(value.source?.url)box.append(link(value.source.url,'查看来源 · '+(value.source.license||'项目文档')));
        box.classList.remove('hidden');$('hub-list').classList.add('hidden');status('');}
      catch(error){if(isCurrent(mine,pin))status(error.message);}
    }
    async function probe(id){
      if(pending)return;const pin=scope(),mine=epoch;pending=true;render();status('正在检测协议与工具列表，最多约12秒…');
      try {const result=await request('/api/extensions/probe',{id},pin);if(!isCurrent(mine,pin))return;
        const row=data.mcp.find(s=>s.id===id);if(row)row.last_check=result;status(result.message);}
      catch(error){if(isCurrent(mine,pin))status(error.message);}
      finally{if(isCurrent(mine,pin)){pending=false;render();}}
    }
    function render(){
      if(!data)return;const container=$('hub-list'),filter=$('hub-search').value.trim();container.replaceChildren();
      const rows=(data[tab]||[]).filter(row=>matches(row,filter)&&(!enabledOnly||row.enabled));
      $('hub-count').textContent=rows.length+' 项';
      for(const row of rows){
        const card=el('article','hub-card'),head=el('div','hub-card-head');
        const icon=el('span','hub-icon');
        if(typeof row.icon_url==='string'&&/^\/icons\/[a-z0-9-]+\.(svg|png|ico)$/.test(row.icon_url)){
          const img=el('img');img.src=row.icon_url;img.alt='';img.width=32;img.height=32;img.loading='lazy';
          img.onerror=()=>{icon.replaceChildren(el('span','',row.title.slice(0,1)));};icon.append(img);
        }else icon.textContent=row.title.slice(0,1);
        head.append(icon);
        const title=el('div','hub-card-title');title.append(el('strong','',row.title));
        const meta=tab==='plugins'?(row.mcp?.length?'MCP 连接':row.skills.length+' 项技能'):tab==='skills'?sourceName(row):row.transport+' · '+row.license;
        title.append(el('span','hub-meta',meta));head.append(title);card.append(head,el('p','hub-description',row.description));
        const actions=el('div','hub-card-actions');
        if(tab==='plugins'){
          const names=row.skills.map(id=>data.skills.find(s=>s.id===id)?.title||id);card.append(el('p','hub-components',names.join(' · ')));
          if(row.status_label)card.append(el('span','hub-tag',row.status_label));
          if(row.supported===false)actions.append(button('查看配置',()=>detail('mcp',row.mcp[0])));
          else actions.append(toggleButton('plugins',row.id,row.enabled,false));
        }else if(tab==='skills'){
          if(row.origin==='bundled')actions.append(toggleButton('skills',row.setting_id,row.enabled,!row.parent_enabled));
          else actions.append(el('span','hub-tag',row.origin==='learned'?(row.enabled?'自动生效':row.status==='candidate'?'自动观察':'历史记录'):'本地可用'));
          if(row.origin==='bundled'&&!row.parent_enabled)card.append(el('p','hub-meta','所属插件包已关闭。'));
          actions.append(button('查看技能',()=>detail('skill',row.id)));
        }else{
          const checked=row.last_check;let label=row.status_label;
          if(row.enabled&&row.status==='unverified'&&checked)label=checked.status==='ok'?'上次检测通过':'上次检测未通过';
          const badge=el('span','hub-tag',label);badge.classList.toggle('good',!!checked&&checked.status==='ok'&&row.enabled);card.append(badge);
          if(row.credential_env)card.append(el('p','hub-meta',(row.has_credentials?'已发现本地凭据 · ':'')+row.credential_env+(row.credential_required?'（必需）':'（可选）')));
          if(row.supported){actions.append(toggleButton('mcp',row.id,row.enabled,false));
            actions.append(button('检测连接',()=>probe(row.id),pending||!row.enabled||['credentials','network_disabled','unconfigured'].includes(row.status)));}
          actions.append(button('配置说明',()=>detail('mcp',row.id)));
          if(checked)card.append(el('p','hub-meta','最近检测 '+checked.checked_at.slice(0,16).replace('T',' ')+' UTC'));
        }
        if(row.source?.url)card.append(link(row.source.url,'来源 · '+(row.source.license||'项目文档')));
        else if(row.source_url)card.append(link(row.source_url,'项目与文档'));
        card.append(actions);container.append(card);
      }
      if(!rows.length)container.append(el('p','hub-empty','没有匹配的项目。'));
      document.querySelectorAll('[data-hub-tab]').forEach(b=>{const selected=b.dataset.hubTab===tab;b.setAttribute('aria-selected',String(selected));b.classList.toggle('selected',selected);});
    }
    $('open-extensions').onclick=()=>{if(!dialog.open)dialog.showModal();$('open-extensions').setAttribute('aria-expanded','true');load();};
    $('close-extensions').onclick=()=>dialog.close();
    dialog.addEventListener('close',()=>{epoch++;viewEpoch++;pending=false;$('open-extensions').setAttribute('aria-expanded','false');$('open-extensions').focus();});
    document.querySelectorAll('[data-hub-tab]').forEach(b=>{b.onclick=()=>{viewEpoch++;tab=b.dataset.hubTab;$('hub-list').classList.remove('hidden');$('hub-detail').classList.add('hidden');render();};});
    $('hub-search').oninput=()=>{viewEpoch++;$('hub-list').classList.remove('hidden');$('hub-detail').classList.add('hidden');render();};
    $('hub-enabled-only').onclick=()=>{viewEpoch++;enabledOnly=!enabledOnly;$('hub-enabled-only').setAttribute('aria-pressed',String(enabledOnly));$('hub-list').classList.remove('hidden');$('hub-detail').classList.add('hidden');render();};
    $('hub-refresh').onclick=load;
    return {changed(){if(dialog.open&&keyOf(scope())!==scopeKey)load();}};
  }
  root.GoalTreeExtensions=Object.freeze({install,matches,keyOf});
})(globalThis);
