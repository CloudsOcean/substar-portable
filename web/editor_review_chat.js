(function(root){
  "use strict";
  root.SubstarReviewChat={init({api,project,context,prepare}){
    const $=s=>document.querySelector(s), panel=$('#reviewChatPanel'), output=$('#reviewChatMessages'), status=$('#reviewChatStatus');
    let epoch=0, request=null, timer=null, watcher=null, signature='', busy=false;
    const endpoint=(pid,id='')=>`/api/projects/${encodeURIComponent(pid)}/review-requests${id?'/'+encodeURIComponent(id):''}`;
    const discard=r=>{if(r)api(endpoint(r.pid,r.id),{method:'DELETE'}).catch(()=>{});};
    function clear(){
      epoch++;clearTimeout(timer);clearInterval(watcher);watcher=null;
      discard(request);request=null;busy=false;signature='';
      panel.hidden=true;output.textContent='';status.textContent='';
      $('#copyReviewResult').hidden=true;$('#stopReviewChat').hidden=true;$('#sendAiReview').disabled=false;
    }
    function valid(version){
      if(version!==epoch)return false;
      if(!$('#aiReviewMenu').open||signature!==context()){clear();return false;}
      return true;
    }
    function watch(){
      clearInterval(watcher);watcher=setInterval(()=>valid(epoch),100);
    }
    async function poll(version){
      if(!valid(version)||!request)return;
      const current=request;
      try{
        const value=await api(endpoint(current.pid,current.id));
        if(!valid(version))return;
        if(value.state==='running'){timer=setTimeout(()=>poll(version),1000);return;}
        output.textContent=value.answer||'';
        status.textContent=value.error||'审阅完成';
        $('#copyReviewResult').hidden=!value.answer;
        $('#stopReviewChat').hidden=true;$('#sendAiReview').disabled=false;busy=false;
        discard(current);request=null;
      }catch(error){
        if(!valid(version))return;
        status.textContent=error.message;busy=false;$('#sendAiReview').disabled=false;$('#stopReviewChat').hidden=true;
        discard(current);request=null;
      }
    }
    async function start(){
      clear();signature=context();const version=epoch,pid=project();
      busy=true;panel.hidden=false;status.textContent='正在准备审阅…';
      $('#sendAiReview').disabled=true;$('#stopReviewChat').hidden=false;watch();
      try{
        const payload=await prepare();if(!valid(version))return;
        const value=await api(endpoint(pid),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        const current={pid,id:value.id};
        if(!valid(version)){discard(current);return;}
        request=current;status.textContent='正在生成审阅反馈…';poll(version);
      }catch(error){
        if(!valid(version))return;
        status.textContent=error.message;busy=false;$('#sendAiReview').disabled=false;$('#stopReviewChat').hidden=true;
      }
    }
    $('#stopReviewChat').onclick=clear;
    $('#copyReviewResult').onclick=async()=>{
      const version=epoch;
      try{await navigator.clipboard.writeText(output.textContent);if(valid(version))status.textContent='反馈已复制';}
      catch{if(valid(version))status.textContent='复制失败，请选择反馈文字复制。';}
    };
    // Cancel on close immediately, including close-and-reopen within a single frame.
    new MutationObserver(()=>{if(!$('#aiReviewMenu').open)clear();}).observe($('#aiReviewMenu'),{attributes:true,attributeFilter:['open']});
    window.addEventListener('pagehide',()=>{
      if(request)fetch(endpoint(request.pid,request.id),{method:'DELETE',keepalive:true}).catch(()=>{});
    });
    return {start,clear,open(){if(!busy&&!signature)panel.hidden=true;}};
  }};
})(window);
