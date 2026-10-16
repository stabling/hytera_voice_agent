/* FlowLingo clean-room page enhancer. Runs in an isolated content-script world. */
(() => {
  "use strict";
  if(globalThis.__flowlingoLoaded)return;
  globalThis.__flowlingoLoaded=true;
  const C=globalThis.FlowCore,D=globalThis.FlowDictionary||[];
  const EXCLUDE = 'script,style,noscript,textarea,input,select,option,button,label,nav,header,footer,a,pre,code,kbd,samp,svg,math,canvas,iframe,[contenteditable],[role="textbox"],[aria-hidden="true"],[data-flow-wrap],[data-flow-ui],.katex,.MathJax,.ytp-caption-window-container,.bpx-player-subtitle-wrap,.player-timedtext';
  const VIDEO_SITES=/(^|\.)youtube\.com$|(^|\.)bilibili\.com$|(^|\.)netflix\.com$/i;
  let settings=C.validSettings({}),known={},ready=false,pausedPage=false;
  let rootHost=null,shadow=null,tipBox=null,floatBox=null,videoBox=null,quickBox=null;
  let intersection=null,pending=new WeakMap(),processed=new WeakSet(),wrapped=new Set();
  let aiNodes=[],aiTimer=null,aiRequests=0,aiBroken=false,epoch=0;
  let exposureCounter=0,exposureTimer=null,siteHref=location.href,lastVideoText="";
  let videoCalls=0,videoBusy=false,videoTimer=null,videoCache=new Map();
  let lastSelection="",currentTooltipInfo=null,notificationTimeout=null;
  const send=async(type,extra={})=>{
    try{
      const res=await chrome.runtime.sendMessage({type,...extra});
      if(!res?.ok)throw new Error(res?.error||"扩展后台不可用");
      return res.data;
    }catch(e){throw new Error(e?.message||"扩展后台不可用");}
  };
  const allowed=()=>ready&&settings.enabled&&!pausedPage&&
    !C.isBlocked(location.hostname,settings.disabledSites);
  const videoAllowed=()=>allowed()&&settings.videoEnabled&&VIDEO_SITES.test(location.hostname);
  function el(tag,className,text){
    const e=document.createElement(tag);
    if(className)e.className=className;
    if(text!==undefined)e.textContent=String(text);
    return e;
  }
  function drawUI(){
    if(rootHost)return;
    rootHost=el("div");
    rootHost.id="flowlingo-root";
    rootHost.dataset.flowUi="1";
    (document.body||document.documentElement).append(rootHost);
    shadow=rootHost.attachShadow({mode:"open"});
    const style=el("style");
    style.textContent=`
      :host{all:initial;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#173d46;}
      *,*:before,*:after{box-sizing:border-box}
      button{font:inherit;cursor:pointer}
      .bubble{position:fixed;z-index:2147483605;width:320px;max-width:calc(100vw - 24px);padding:17px;border:1px solid #e5e1d8;border-radius:18px;pointer-events:auto;color:#173d46;background:#fffdf8;box-shadow:0 18px 45px #16394333;display:none;}
      .bar{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px;}
      .eyebrow{font-size:10px;letter-spacing:.13em;font-weight:750;color:#89948f;text-transform:uppercase}
      .close{border:0;background:transparent;color:#667e7e;border-radius:6px;padding:0 5px;font-size:21px;line-height:1.2}
      .head{font-size:21px;font-weight:800;letter-spacing:-.02em;overflow-wrap:anywhere}
      .translation{font-size:15px;color:#28474c;margin:4px 0 9px;}
      .context{border-radius:9px;background:#f3f1ea;padding:10px 11px;color:#63716e;font-size:12px;line-height:1.7;max-height:100px;overflow:auto;overflow-wrap:anywhere}
      .tip{font-size:12px;color:#5e7670;margin:8px 0;}
      .buttons{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
      .btn{border:1px solid #dce4df;background:#fff;padding:6px 10px;border-radius:9px;color:#275861;font-size:12px;font-weight:650}
      .btn.primary{background:#1d525d;color:#fff;border-color:#1d525d}
      .btn:disabled{opacity:.5;cursor:default}
      .note{font-size:11px;color:#71837c;margin-top:10px}
      .floating{position:fixed;bottom:26px;right:24px;pointer-events:auto;display:flex;align-items:center;gap:7px;border:1px solid #edf1e8;border-radius:24px;background:#214d57;color:#fffcf6;padding:10px 13px;box-shadow:0 9px 24px #1d3d4440;font-weight:700;cursor:pointer;transition:transform .2s,opacity .2s}
      .floating:hover{transform:translateY(-2px)}
      .floating .dot{height:8px;width:8px;border-radius:50%;background:#f7bb8a}
      .quick{position:fixed;bottom:75px;right:24px;width:240px;padding:15px;border:1px solid #e5e1d8;border-radius:16px;box-shadow:0 16px 40px #173d4630;background:#fffdf9;pointer-events:auto;display:none}
      .quick h3{font-size:15px;margin:0 0 4px;font-weight:800}
      .quick p{margin:0 0 12px;font-size:12px;color:#778782}
      .row{display:flex;gap:7px}
      .quick .btn{flex:1}
      .video{position:fixed;z-index:2147483602;pointer-events:none;transform:translateX(-50%);padding:8px 16px 10px;max-width:75vw;min-width:260px;border-radius:10px;text-align:center;color:#fff;background:rgba(10,23,29,.84);box-shadow:0 3px 18px #0005;display:none;}
      .video-source{font-size:13px;line-height:1.45;opacity:.85}
      .video-target{font-size:18px;font-weight:700;line-height:1.5;overflow-wrap:anywhere;text-shadow:0 1px 2px #0008}
      .video-tip{color:#eacdaf;font-size:11px;margin-top:3px}
      .toast{position:fixed;right:24px;bottom:82px;border-radius:9px;background:#1b434c;color:#fff;font-size:12px;padding:9px 12px;box-shadow:0 8px 24px #20343c30;pointer-events:none;display:none;max-width:310px}
      @media(prefers-reduced-motion:reduce){.floating{transition:none}}
    `;
    shadow.append(style);
    tipBox=el("div","bubble");tipBox.setAttribute("role","dialog");tipBox.setAttribute("aria-label","FlowLingo 英语讲解");
    floatBox=el("button","floating");floatBox.setAttribute("aria-label","FlowLingo 学习快捷菜单");
    quickBox=el("div","quick");
    videoBox=el("div","video");
    shadow.append(tipBox,floatBox,quickBox,videoBox);
    const toast=el("div","toast");toast.id="flow-toast";shadow.append(toast);
    floatBox.addEventListener("click",()=>{quickBox.style.display=quickBox.style.display==="block"?"none":"block";hideTip();});
    tipBox.addEventListener("click",e=>e.stopPropagation());
    renderFloat();
  }
  function renderFloat(){
    if(!floatBox)return;
    floatBox.style.display=ready&&settings.floatingEnabled&&!C.isBlocked(location.hostname,settings.disabledSites)?"flex":"none";
    floatBox.replaceChildren(el("span","dot"),document.createTextNode(pausedPage||!settings.enabled?" 已暂停":" FlowLingo · "+settings.intensity+"%"));
    quickBox.replaceChildren();
    quickBox.append(el("h3",null,"FlowLingo · 无感英语"),el("p",null,pausedPage||!settings.enabled?"此页面已暂停":"阅读中自动出现适量英语。"));
    const row=el("div","row");
    const pause=el("button","btn primary",pausedPage||!settings.enabled?"继续学习":"暂停本页");
    pause.onclick=()=>toggle();
    const less=el("button","btn","−");less.title="降低英语比例";
    less.onclick=()=>updateDensity(-5);
    const more=el("button","btn","+");more.title="提高英语比例";
    more.onclick=()=>updateDensity(5);
    row.append(pause,less,more);
    const row2=el("div","row");row2.style.marginTop="8px";
    const home=el("button","btn","学习中心 ↗");
    home.onclick=()=>{send("OPEN_DASHBOARD").catch(()=>{});quickBox.style.display="none";};
    const close=el("button","btn","隐藏悬浮球");
    close.onclick=async()=>{
      const s={...settings,floatingEnabled:false};
      try { await send("PATCH_SETTINGS",{changes:{floatingEnabled:false}}); }
      catch(e) { toast("无法保存设置："+e.message); }
      quickBox.style.display="none";
    };
    row2.append(home,close);quickBox.append(row,row2);
  }
  function toast(message){
    if(!shadow)return;
    const node=shadow.querySelector("#flow-toast");
    node.textContent=String(message).slice(0,110);
    node.style.display="block";
    clearTimeout(notificationTimeout);
    notificationTimeout=setTimeout(()=>node.style.display="none",2600);
  }
  function placeBox(box,rect){
    const width=320,gap=8;
    const left=Math.max(12,Math.min(window.innerWidth-width-12,rect.left));
    box.style.left=left+"px";
    box.style.top=Math.max(10,Math.min(window.innerHeight-290,rect.bottom+gap))+"px";
    box.style.display="block";
    requestAnimationFrame(()=>{
      if(box.style.display!=="block")return;
      const height=box.getBoundingClientRect().height;
      if(rect.bottom+height+gap>window.innerHeight && rect.top-height-gap>0)
        box.style.top=Math.max(9,rect.top-height-gap)+"px";
      else box.style.top=Math.max(9,Math.min(window.innerHeight-height-9,rect.bottom+gap))+"px";
    });
  }
  function hideTip(){if(tipBox)tipBox.style.display="none";currentTooltipInfo=null;}
  function speak(text){
    if(!("speechSynthesis" in window)){toast("当前浏览器不支持语音朗读");return;}
    speechSynthesis.cancel();
    const utterance=new SpeechSynthesisUtterance(text);
    utterance.lang="en-US";utterance.rate=.86;
    speechSynthesis.speak(utterance);
  }
  function tooltip(data,rect){
    drawUI();quickBox.style.display="none";currentTooltipInfo=data;
    tipBox.replaceChildren();
    const headRow=el("div","bar");
    headRow.append(el("span","eyebrow","FLOWLINGO · IN CONTEXT"));
    const x=el("button","close","×");x.setAttribute("aria-label","关闭");x.onclick=hideTip;
    headRow.append(x);
    const term=el("div","head",data.english||data.source);
    const meaning=el("div","translation",data.meaning||"点击 AI 解析以查看语境释义");
    const context=el("div","context",data.context||"来自当前网页的真实语境");
    const note=el("div","tip",data.tip||"");
    if(!data.tip)note.style.display="none";
    tipBox.append(headRow,term,meaning,context,note);
    const buttons=el("div","buttons");
    const listen=el("button","btn","♫ 发音");listen.onclick=()=>speak(data.english||data.source);
    const save=el("button","btn primary","＋ 加入生词本");
    save.onclick=async()=>{
      save.disabled=true;save.textContent="保存中…";
      try{
        await send("SAVE_WORD",{data:{
          term:data.english||data.source,meaning:data.meaning,tip:data.tip,
          context:data.context,url:location.href,level:data.level||2}});
        save.textContent="✓ 已保存";toast("已加入生词本");
      }catch(e){save.textContent="保存失败";toast(e.message);}
    };
    const explain=el("button","btn","✦ AI 语境讲解");
    explain.onclick=async()=>{
      explain.disabled=true;explain.textContent="分析中…";
      try{
        const ai=await send("AI_EXPLAIN",{term:data.english||data.source,context:data.context||""});
        meaning.textContent=ai.meaning||meaning.textContent;
        note.textContent=[ai.tip,ai.example?`例句：${ai.example}`:"",ai.translation].filter(Boolean).join("\n");
        note.style.display="block";note.style.whiteSpace="pre-line";
        explain.textContent="✓ 解析完成";
      }catch(e){explain.disabled=false;explain.textContent="✦ AI 语境讲解";toast(e.message);}
    };
    const mastered=el("button","btn","✓ 我已掌握");
    mastered.onclick=async()=>{
      mastered.disabled=true;
      try{
        await send("SAVE_WORD",{data:{
          term:data.english||data.source,meaning:data.meaning,tip:data.tip,
          context:data.context,url:location.href,level:data.level||2}});
        await send("UPDATE_WORD",{data:{term:data.english||data.source,action:"known"}});
        mastered.textContent="✓ 已标记掌握";
        toast("已更新语言掌握记录");
      }catch(e){mastered.disabled=false;toast(e.message);}
    };
    buttons.append(listen,save,explain,mastered);
    if(data.element){
      const toggle=el("button","btn",data.element.classList.contains("fl-original")?"显示英文":"查看原文");
      toggle.onclick=()=>{
        const node=data.element;
        node.classList.toggle("fl-original");
        node.textContent=node.classList.contains("fl-original")?node.dataset.source:node.dataset.english;
        toggle.textContent=node.classList.contains("fl-original")?"显示英文":"查看原文";
      };
      buttons.append(toggle);
    }
    tipBox.append(buttons,el("div","note","内容仅供辅助学习；复杂语境请结合原文判断。"));
    placeBox(tipBox,rect);
  }
  function dataFromMark(mark){
    const wrap=mark.closest("[data-flow-wrap]");
    return {
      source:mark.dataset.source||"",english:mark.dataset.english||"",
      meaning:mark.dataset.meaning||mark.dataset.source,
      tip:mark.dataset.tip||"",level:Number(mark.dataset.level)||2,
      context:(wrap?.dataset.original||mark.parentElement?.textContent||"").slice(0,240),
      element:mark
    };
  }
  function isEligible(node){
    if(node?.nodeType!==Node.TEXT_NODE||!node.isConnected||!node.parentElement)return false;
    const text=node.nodeValue||"";
    if(text.trim().length<8 || text.length>1000)return false;
    if(!C.langOf(text))return false;
    if(node.parentElement.closest(EXCLUDE))return false;
    const p=node.parentElement;
    if(p.isContentEditable || p.closest("[contenteditable]"))return false;
    const style=getComputedStyle(p);
    if(style.display==="none"||style.visibility==="hidden"||style.opacity==="0")return false;
    return true;
  }
  function createIntersection(){
    if(intersection)intersection.disconnect();
    pending=new WeakMap();
    intersection=new IntersectionObserver(records=>{
      for(const item of records){
        if(!item.isIntersecting)continue;
        const nodes=pending.get(item.target);
        if(!nodes)continue;
        pending.delete(item.target);
        intersection.unobserve(item.target);
        for(const node of nodes)processNode(node);
      }
    },{rootMargin:"250px 0px 250px 0px",threshold:0});
  }
  function watchNode(node){
    if(processed.has(node)||!isEligible(node))return;
    processed.add(node);
    const parent=node.parentElement;
    let nodes=pending.get(parent);
    if(!nodes){nodes=new Set();pending.set(parent,nodes);intersection.observe(parent);}
    nodes.add(node);
  }
  function scan(root=document.body){
    if(!allowed()||!root)return;
    if(root.nodeType===Node.TEXT_NODE){watchNode(root);return;}
    if(root.nodeType!==Node.ELEMENT_NODE||root.closest("[data-flow-wrap],[data-flow-ui]"))return;
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    let n=0;
    while(walker.nextNode()&&n<1800){
      watchNode(walker.currentNode);n++;
    }
  }
  function applyPlan(node,plans,original){
    if(!allowed()||!node.isConnected||node.nodeValue!==original||!plans.length)return;
    if(node.parentElement?.closest("[data-flow-wrap],[data-flow-ui]"))return;
    const wrapper=el("span","fl-inline-wrap");
    wrapper.dataset.flowWrap="1";wrapper.dataset.original=original;
    let pos=0;
    for(const p of plans){
      if(p.start<pos||p.end>original.length)continue;
      if(p.start>pos)wrapper.append(document.createTextNode(original.slice(pos,p.start)));
      const span=el("span","fl-inline-mark",p.english);
      span.dataset.source=p.source;span.dataset.english=p.english;
      span.dataset.meaning=p.meaning||p.source;span.dataset.tip=p.tip||"";
      span.dataset.level=String(p.level||2);span.dataset.mode=p.mode;
      span.setAttribute("tabindex","0");
      span.setAttribute("role","button");
      span.setAttribute("aria-label",`${p.english}：查看英语解释`);
      wrapper.append(span);pos=p.end;
    }
    if(pos<original.length)wrapper.append(document.createTextNode(original.slice(pos)));
    if(!wrapper.children.length)return;
    node.replaceWith(wrapper);
    wrapped.add(wrapper);
    exposureCounter+=plans.length;
    scheduleExposure();
  }
  function localFor(node,mode){
    if(!isEligible(node)||!allowed())return;
    const text=node.nodeValue;
    const plan=C.planLocal(text,mode,settings,known,D);
    applyPlan(node,plan,text);
  }
  function processNode(node){
    if(!isEligible(node)||!allowed())return;
    const mode=C.langOf(node.nodeValue);
    if(!mode)return;
    if(settings.pageMode!=="auto"&&settings.pageMode!==mode)return;
    if(mode==="en"&&node.nodeValue.trim().length<30)return;
    if(settings.aiEnabled&&!aiBroken&&aiRequests<5){
      aiNodes.push({node,mode,text:node.nodeValue,epoch});
      if(aiNodes.length>=5)flushAI();
      else if(!aiTimer)aiTimer=setTimeout(flushAI,320);
    }else localFor(node,mode);
  }
  async function flushAI(){
    clearTimeout(aiTimer);aiTimer=null;
    if(!aiNodes.length||!allowed())return;
    if(aiRequests>=5||aiBroken){
      for(const x of aiNodes.splice(0))localFor(x.node,x.mode);
      return;
    }
    const batch=aiNodes.splice(0,5);
    aiRequests++;
    const stamp=epoch;
    try{
      const blocks=batch.map((x,i)=>({id:String(i),text:x.text.slice(0,470),mode:x.mode}));
      const result=await send("AI_TRANSFORM",{blocks});
      if(stamp!==epoch)return;
      for(let i=0;i<batch.length;i++){
        const item=batch[i],source=result?.items?.find(x=>String(x.id)===String(i));
        const plans=C.planAI(item.text,item.mode,source?.replacements||[],settings);
        if(plans.length)applyPlan(item.node,plans,item.text);
        else localFor(item.node,item.mode);
      }
    }catch(e){
      aiBroken=true;
      if(stamp===epoch){
        toast("AI 暂不可用，已切换离线词典");
        for(const item of batch)localFor(item.node,item.mode);
      }
    }
    if(aiNodes.length)aiTimer=setTimeout(flushAI,200);
  }
  function restoreAll(){
    epoch++;
    clearTimeout(aiTimer);aiNodes=[];aiTimer=null;
    if(intersection)intersection.disconnect();
    for(const wrap of wrapped){
      if(wrap.isConnected)wrap.replaceWith(document.createTextNode(wrap.dataset.original||""));
    }
    wrapped.clear();processed=new WeakSet();
    createIntersection();
    exposureCounter=0;
  }
  function rescan(){
    restoreAll();
    if(allowed())scan(document.body);
    renderFloat();
    if(!videoAllowed())videoBox&&(videoBox.style.display="none");
  }
  async function updateDensity(delta){
    const next={...settings,intensity:C.clamp(settings.intensity+delta,3,55)};
    settings=next;
    try{await send("PATCH_SETTINGS",{changes:{intensity:next.intensity}});}
    catch(e){toast("设置保存失败："+e.message);}
    quickBox.style.display="none";
    rescan();
  }
  function toggle(){
    pausedPage=!pausedPage;
    rescan();
    quickBox.style.display="none";
    toast(pausedPage?"已暂停本页改造":"已恢复本页学习");
  }
  function scheduleExposure(){
    if(exposureTimer)return;
    exposureTimer=setTimeout(()=>{
      exposureTimer=null;
      const count=exposureCounter;exposureCounter=0;
      if(count)send("EXPOSURE",{count:Math.min(count,30)}).catch(()=>{});
    },2800);
  }
  function videoLocation(){
    const video=[...document.querySelectorAll("video")].find(v=>{
      const b=v.getBoundingClientRect();
      return b.width>260&&b.height>130&&b.bottom>0&&b.top<innerHeight;
    });
    if(!video)return null;
    const r=video.getBoundingClientRect();
    return {left:Math.max(145,Math.min(innerWidth-145,r.left+r.width/2)),
      top:Math.max(50,Math.min(innerHeight-85,r.bottom-120)),width:Math.max(260,Math.min(710,r.width*.9))};
  }
  function collectCaptions(){
    let selector="";
    if(/youtube\.com$/.test(location.hostname))selector=".ytp-caption-segment";
    else if(/bilibili\.com$/.test(location.hostname))selector=".bpx-player-subtitle-item-text,.bpx-player-subtitle-item,.bilibili-player-video-subtitle .subtitle-item-text";
    else if(/netflix\.com$/.test(location.hostname))selector=".player-timedtext-text-container span";
    if(!selector)return "";
    const nodes=[...document.querySelectorAll(selector)].filter(n=>{
      if(n.closest('[data-flow-ui]'))return false;
      const b=n.getBoundingClientRect();
      return b.width>0 && b.height>0 && b.bottom>=0 && b.top<=innerHeight;
    });
    return nodes.map(n=>n.textContent.trim()).filter(Boolean).join(" ").replace(/\s+/g," ").slice(0,250);
  }
  function setVideoLine(original,output,note=""){
    if(!videoAllowed()||!videoBox)return;
    const loc=videoLocation();
    if(!loc||!original){videoBox.style.display="none";return;}
    videoBox.style.left=loc.left+"px";videoBox.style.top=loc.top+"px";
    videoBox.style.width=loc.width+"px";
    videoBox.replaceChildren();
    if(settings.videoMode!=="mix"||!output)
      videoBox.append(el("div","video-source",original));
    if(output)videoBox.append(el("div","video-target",output));
    if(note)videoBox.append(el("div","video-tip",note));
    videoBox.style.display="block";
  }
  function localCaption(text,lang){
    const plans=C.planLocal(text,lang,settings,known,D);
    if(lang==="zh"){
      let pos=0,result="";
      for(const p of plans){result+=text.slice(pos,p.start)+p.english;pos=p.end;}
      result+=text.slice(pos);
      return {translation:result===text?"":result,tip:""};
    }
    return {translation:plans.length?"关键词："+plans.map(p=>`${p.source}（${p.meaning}）`).join(" · "):"",
      tip:plans.length?"离线模式为词语注解；开启 AI 可翻译完整英文字幕。":""};
  }
  async function updateVideo(){
    if(!videoAllowed()||!videoBox)return;
    const original=collectCaptions();
    if(!original){videoBox.style.display="none";lastVideoText="";return;}
    const lang=C.langOf(original);
    if(!lang)return;
    if(original===lastVideoText){
      const loc=videoLocation();
      if(loc){videoBox.style.left=loc.left+"px";videoBox.style.top=loc.top+"px";}
      return;
    }
    lastVideoText=original;
    const fallback=localCaption(original,lang);
    const cached=videoCache.get(original);
    if(cached){setVideoLine(original,cached.translation,cached.tip||"");return;}
    setVideoLine(original,fallback.translation,fallback.tip);
    if(!settings.aiEnabled||aiBroken||videoBusy||videoCalls>=12)return;
    videoBusy=true;videoCalls++;
    try{
      const answer=await send("AI_VIDEO",{text:original,lang});
      const value={translation:C.clip(answer?.translation,350),tip:C.clip(answer?.tip,110)};
      if(value.translation){
        videoCache.set(original,value);
        if(videoCache.size>80)videoCache.delete(videoCache.keys().next().value);
        if(lastVideoText===original)setVideoLine(original,value.translation,value.tip);
      }
    }catch{
      // Video always remains useful offline, even with missing AI credentials.
    }finally{videoBusy=false;}
  }
  function scheduleVideo(){
    if(videoTimer)return;
    videoTimer=setTimeout(()=>{videoTimer=null;updateVideo().catch(()=>{});},240);
  }
  function maybeMoveFullscreen(){
    if(!rootHost)return;
    const desired=document.fullscreenElement;
    if(desired && desired!==rootHost && desired instanceof HTMLElement &&
       !["VIDEO","IFRAME"].includes(desired.tagName) && rootHost.parentNode!==desired)
      desired.append(rootHost);
    else if(!desired&&rootHost.parentNode!==document.body&&document.body)
      document.body.append(rootHost);
    scheduleVideo();
  }
  function explainSelection(){
    if(!settings.selectionEnabled)return;
    const sel=window.getSelection();
    if(!sel||sel.isCollapsed||!sel.rangeCount)return;
    const text=sel.toString().trim();
    if(!text||text.length>80||text===lastSelection&&tipBox.style.display==="block")return;
    const anchor=sel.anchorNode?.parentElement;
    if(anchor?.closest(EXCLUDE))return;
    const rect=sel.getRangeAt(0).getBoundingClientRect();
    if(!rect.width&&!rect.height)return;
    lastSelection=text;
    const row=D.find(x=>x.en.toLowerCase()===text.toLowerCase()||x.cn===text);
    tooltip({
      source:text,english:row?.en||text,meaning:row?.cn||"",
      tip:row?.tip||"",level:row?.level||2,
      context:sel.anchorNode?.parentElement?.textContent?.slice(0,230)||text
    },rect);
  }
  function captureClick(e){
    const mark=e.target.closest?.(".fl-inline-mark");
    if(!mark)return;
    e.preventDefault();e.stopPropagation();
    tooltip(dataFromMark(mark),mark.getBoundingClientRect());
  }
  let hoverTimer=null;
  function onMarkHover(e){
    const mark=e.target?.closest?.(".fl-inline-mark");
    if(!mark)return;
    clearTimeout(hoverTimer);
    hoverTimer=setTimeout(()=>{
      if(mark.isConnected && allowed())tooltip(dataFromMark(mark),mark.getBoundingClientRect());
    },550);
  }
  function onMarkLeave(){
    clearTimeout(hoverTimer);
  }
  function captureKey(e){
    if(e.key==="Enter"&&e.target?.matches?.(".fl-inline-mark")){
      e.preventDefault();
      tooltip(dataFromMark(e.target),e.target.getBoundingClientRect());
    }
    if(e.key==="Escape")hideTip();
  }
  const mutationRoots=new Set();
  let mutationTimer=null;
  function onMutation(records){
    let needVideo=false;
    for(const r of records){
      if(r.type==="characterData"){
        if(r.target.parentElement?.closest("[data-flow-wrap],[data-flow-ui]"))continue;
        mutationRoots.add(r.target);
      }else {
        for(const node of r.addedNodes){
          if(node.nodeType===Node.ELEMENT_NODE){
            if(node.closest("[data-flow-wrap],[data-flow-ui]"))continue;
            mutationRoots.add(node);
          }else if(node.nodeType===Node.TEXT_NODE &&
                  !node.parentElement?.closest("[data-flow-wrap],[data-flow-ui]"))
            mutationRoots.add(node);
        }
      }
      needVideo=true;
      if(mutationRoots.size>75)break;
    }
    if(needVideo&&videoAllowed())scheduleVideo();
    if(mutationRoots.size&&!mutationTimer){
      mutationTimer=setTimeout(()=>{
        mutationTimer=null;
        let amount=0;
        for(const n of mutationRoots){
          if(++amount>75)break;
          if(n.isConnected)scan(n);
        }
        mutationRoots.clear();
        if(location.href!==siteHref){siteHref=location.href;lastVideoText="";scheduleVideo();}
      },190);
    }
  }
  async function init(){
    try{
      const data=await send("GET_STATE");
      settings=C.validSettings(data.settings);
      known=data.known||{};
    }catch(e){console.warn("[FlowLingo]",e);return;}
    ready=true;
    drawUI();createIntersection();
    const observer=new MutationObserver(onMutation);
    observer.observe(document.documentElement,{childList:true,subtree:true,characterData:true});
    document.addEventListener("click",captureClick,true);
    document.addEventListener("pointerover",onMarkHover,true);
    document.addEventListener("pointerout",onMarkLeave,true);
    document.addEventListener("keydown",captureKey,true);
    document.addEventListener("mouseup",()=>setTimeout(explainSelection,100));
    document.addEventListener("fullscreenchange",maybeMoveFullscreen);
    window.addEventListener("scroll",()=>{
      if(tipBox?.style.display==="block")hideTip();
      if(videoAllowed())scheduleVideo();
    },{passive:true});
    window.addEventListener("resize",scheduleVideo,{passive:true});
    chrome.runtime.onMessage.addListener(message=>{
      if(message.type==="TOGGLE")toggle();
      if(message.type==="EXPLAIN_SELECTION")explainSelection();
      if(message.type==="FLOW_REFRESH"){
        send("GET_STATE").then(data=>{
          settings=C.validSettings(data.settings);
          known=data.known||{};
          aiBroken=false;aiRequests=0;videoCalls=0;
          rescan();if(videoAllowed())scheduleVideo();
        }).catch(()=>{});
      }
    });
    chrome.storage.onChanged.addListener((changes,area)=>{
      if(area!=="local"||!changes.settings)return;
      const next=C.validSettings(changes.settings.newValue);
      settings=next;aiBroken=false;aiRequests=0;videoCalls=0;
      rescan();
      if(videoAllowed())scheduleVideo();
    });
    if(allowed())scan(document.body);
    if(videoAllowed())scheduleVideo();
  }
  if(document.body)init();else document.addEventListener("DOMContentLoaded",init,{once:true});
})();
