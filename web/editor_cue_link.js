(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;if(root)root.SubstarCueLink=api;})(globalThis,function(){
  'use strict';
  function resolve(url,cues){
    const id=url.searchParams.get('cue_id'),raw=url.searchParams.get('cue');
    if(!id&&raw===null)return null;
    if(!id&&!/^[1-9]\d*$/.test(raw||''))throw Error('Cue 编号无效');
    const found=cues.find(c=>id?c.cue_id===id:Number(c.index)+1===Number(raw));
    if(!found)throw Error('此 Cue 已不存在，请让 agent 读取最新版本后重新提供链接');
    return found;
  }
  function url(project,cue,base){
    const link=new URL('/editor',base);link.searchParams.set('project',project);
    link.searchParams.set('cue_id',cue.cue_id);link.searchParams.set('cue',String(Number(cue.index)+1));
    return link;
  }
  function startSession({getProject,onNavigate,canNavigate=()=>true}) {
    const viewerId=crypto.randomUUID(),indicator=document.querySelector('#projectNavigationStatus');
    let busy=false,dead=false,project='',ack=null,controller=null,lastSuccess=0;
    let initialPoll=true,baselineId=null,handoffId=null;
    let handoffChannel=null;
    function listenForHandoffs(pid) {
      handoffChannel?.close();
      handoffChannel=null;
      if(!pid||typeof BroadcastChannel!=='function')return;
      const channel=new BroadcastChannel('substar:cue-navigation:'+pid);
      handoffChannel=channel;
      channel.onmessage=({data})=>{
        if(data?.type!=='probe'||typeof data.navigationId!=='string'||pid!==getProject())return;
        handoffId=data.navigationId;
        let status='connected';
        if(ack?.id===data.navigationId)status=ack.error?'failed':'located';
        channel.postMessage({type:'editor',navigationId:data.navigationId,status});
        if(status==='connected'&&canNavigate())void poll();
      };
    }
    const show=(state,label)=>{
      if(!indicator)return;
      indicator.dataset.state=state;indicator.querySelector('span').textContent=label;
      indicator.title='当前项目的定位通道：'+label;
    };
    const leave=pid=>{
      if(pid)navigator.sendBeacon('/api/agent/projects/'+encodeURIComponent(pid)+'/navigation/poll',
        new Blob([JSON.stringify({viewer_id:viewerId,closed:true})],{type:'application/json'}));
    };
    async function poll(){
      if(dead||busy||!getProject())return;
      const pid=getProject();
      if(project!==pid){
        leave(project);project=pid;ack=null;lastSuccess=0;
        initialPoll=true;baselineId=null;handoffId=null;
        listenForHandoffs(pid);show('connecting','连接中');
      }
      busy=true;controller=new AbortController();const timer=setTimeout(()=>controller?.abort(),5000);
      try{
        const response=await fetch('/api/agent/projects/'+encodeURIComponent(pid)+'/navigation/poll',{
          method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,
          body:JSON.stringify({viewer_id:viewerId,ack_id:ack?.id||null,ack_error:ack?.error||''})
        });
        if(!response.ok)throw Error('定位通道断开');
        const data=await response.json();
        if(pid!==getProject())return;
        lastSuccess=Date.now();const command=data.navigation;
        // The first poll establishes a baseline. Only a live handoff may
        // ask this newly opened editor to handle that existing command.
        if(initialPoll){baselineId=command?.navigation_id??null;initialPoll=false;}
        if(command&&command.project_id===pid&&command.navigation_id!==ack?.id
          &&(command.navigation_id!==baselineId||command.navigation_id===handoffId)){
          if(!canNavigate()){show('waiting','定位待处理');return;}
          const target=new URL(command.editor_url,location.href);
          const valid=target.origin===location.origin&&target.pathname==='/editor'&&target.searchParams.get('project')===pid;
          const ok=valid&&onNavigate(target)===true;
          ack={id:command.navigation_id,error:ok?'':'无法定位此 Cue'};
          show(ok?'connected':'disconnected',ok?'已定位 Cue '+command.cue:'定位失败');
          handoffChannel?.postMessage({type:'editor',navigationId:ack.id,status:ok?'located':'failed'});
        }else if(ack?.error)show('disconnected','定位失败');
        else show('connected','已连接');
      }catch(_error){if(pid===getProject())show('disconnected','未连接');}
      finally{clearTimeout(timer);controller=null;busy=false;}
    }
    const tick=()=>{
      if(getProject()!==project){show('connecting','连接中');}
      else if(lastSuccess&&Date.now()-lastSuccess>6000)show('disconnected','未连接');
      poll();
    };
    const timer=setInterval(tick,800);
    window.addEventListener('focus',tick);document.addEventListener('visibilitychange',tick);
    window.addEventListener('pagehide',()=>{dead=true;controller?.abort();listenForHandoffs('');leave(project);show('disconnected','未连接');});
    window.addEventListener('pageshow',event=>{if(event.persisted){dead=false;project='';ack=null;tick();}});
    tick();return {poll};
  }
  return {resolve,url,startSession};
});
