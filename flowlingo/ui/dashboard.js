/* FlowLingo learning centre. Every dynamic string uses textContent (never untrusted HTML). */
(async()=>{
  "use strict";
  const C=globalThis.FlowCore;
  const $=id=>document.getElementById(id);
  const node=(tag,cls,txt)=>{
    const e=document.createElement(tag);if(cls)e.className=cls;
    if(txt!==undefined)e.textContent=String(txt);return e;
  };
  let settings=C.validSettings({}),words={},stats={exposures:0,reviews:0,daily:{}};
  let currentReview=null,revealed=false,visitedReview=new Set();
  const date=new Date();
  $("today").textContent=new Intl.DateTimeFormat("zh-CN",{year:"numeric",month:"long",day:"numeric",weekday:"long"}).format(date);
  function showToast(message){
    const t=$("toast");t.textContent=String(message).slice(0,190);t.style.display="block";
    clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>t.style.display="none",3500);
  }
  async function send(type,data={}){
    const answer=await chrome.runtime.sendMessage({type,...data});
    if(!answer?.ok)throw new Error(answer?.error||"后台服务未就绪");
    return answer.data;
  }
  async function reload(){
    const store=await chrome.storage.local.get(["settings","words","stats","apiSecret"]);
    settings=C.validSettings(store.settings);
    words=store.words||{};
    stats=store.stats||{exposures:0,reviews:0,daily:{}};
    displaySettings(store.apiSecret||"");
    renderAll();
  }
  function dueWords(){
    return Object.values(words).filter(w=>w&&w.status!=="known"&&(w.dueAt||0)<=Date.now()).sort((a,b)=>(a.dueAt||0)-(b.dueAt||0));
  }
  function showTab(tab){
    document.querySelectorAll(".panel").forEach(e=>e.hidden=e.id!==tab);
    document.querySelectorAll("[data-tab]").forEach(e=>e.classList.toggle("selected",e.dataset.tab===tab));
    const titles={overview:"学习概览",vocabulary:"我的生词",review:"间隔复习",settings:"偏好设置",about:"隐私与帮助"};
    $("pageTitle").textContent=titles[tab]||"学习概览";
    if(tab==="review")nextReview(true);
  }
  function renderAll(){
    const list=Object.values(words);
    const due=dueWords().length;
    $("statExposure").textContent=(stats.exposures||0).toLocaleString("zh-CN");
    $("statWords").textContent=list.length.toLocaleString("zh-CN");
    $("statDue").textContent=due.toLocaleString("zh-CN");
    $("statReviews").textContent=(stats.reviews||0).toLocaleString("zh-CN");
    $("sideDue").textContent=String(due);
    renderChart();renderWords();
  }
  function renderChart(){
    const view=$("chart");view.replaceChildren();
    const values=[];
    for(let i=6;i>=0;i--){
      const d=new Date();d.setHours(12,0,0,0);d.setDate(d.getDate()-i);
      const key=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
      values.push({day:new Intl.DateTimeFormat("en",{weekday:"short"}).format(d),value:stats.daily?.[key]||0,today:i===0});
    }
    const max=Math.max(...values.map(v=>v.value),1);
    for(const v of values){
      const col=node("div","bar-group"),bar=node("div","bar"+(v.today?" today":""));
      bar.style.height=Math.max(3,Math.round(v.value/max*130))+"px";
      bar.title=`${v.day} · ${v.value} exposures`;
      col.append(bar,node("small",null,v.day));view.append(col);
    }
  }
  function renderWords(){
    const q=$("wordSearch").value.trim().toLowerCase();
    const filter=$("wordFilter").value,now=Date.now();
    const list=Object.values(words).filter(row=>{
      if(!row || typeof row.term!=="string")return false;
      if(filter==="learning"&&row.status==="known")return false;
      if(filter==="known"&&row.status!=="known")return false;
      if(filter==="due"&&(row.status==="known"||(row.dueAt||0)>now))return false;
      return !q || [row.term,row.meaning,row.context].some(value=>String(value||"").toLowerCase().includes(q));
    }).sort((a,b)=>(b.createdAt||0)-(a.createdAt||0));
    const view=$("wordsList");view.replaceChildren();
    $("wordShowing").textContent=`${list.length} 条`;
    if(!list.length){
      const empty=node("div","empty");empty.style.padding="45px 20px";
      empty.append(node("div","empty-mark","✧"),node("h2",null,"这里还没有对应的词语"),node("p",null,"去看看一篇文章，点击遇到的新表达吧。"));
      view.append(empty);return;
    }
    for(const row of list.slice(0,300)){
      const wrap=node("div","word-row"),left=node("div","word-body");
      const first=node("div");first.append(node("span","word-en",row.term),node("span","word-zh",row.meaning||"尚无释义"));
      left.append(first,node("div","word-context",row.context||row.tip||""));
      const actions=node("div","word-actions");
      const speak=node("button","mini","♫ 发音");
      speak.onclick=()=>{
        if(!window.speechSynthesis)return;
        window.speechSynthesis.cancel();
        const u=new SpeechSynthesisUtterance(row.term);u.lang="en-US";u.rate=.86;
        window.speechSynthesis.speak(u);
      };
      const status=node("button","mini",row.status==="known"?"恢复学习":"✓ 已掌握");
      status.onclick=()=>changeWord(row.term,row.status==="known"?"learning":"known");
      const remove=node("button","mini remove","移除");
      remove.onclick=()=>changeWord(row.term,"remove");
      actions.append(speak,status,remove);wrap.append(left,actions);view.append(wrap);
    }
    if(list.length>300)view.append(node("p","muted","仅显示前 300 条，可用搜索快速定位其它词语。"));
  }
  async function changeWord(term,action,quality){
    try{
      await send("UPDATE_WORD",{data:{term,action,quality}});
      const updated=await chrome.storage.local.get(["words","stats"]);
      words=updated.words||{};stats=updated.stats||stats;
      renderAll();
      showToast(action==="remove"?"已从生词本移除":"已更新学习状态");
    }catch(e){showToast(e.message);}
  }
  function nextReview(reset=false){
    if(reset)visitedReview.clear();
    const list=dueWords().filter(w=>!visitedReview.has(C.keyOf(w.term)));
    const has=list.length>0;
    $("reviewPlace").hidden=!has;$("reviewEmpty").hidden=has;
    if(!has){currentReview=null;return;}
    currentReview=list[0];revealed=false;
    $("reviewTerm").textContent=currentReview.term;
    $("reviewMeaning").textContent=currentReview.meaning||"未记录中文释义";
    $("reviewContext").textContent=currentReview.context||"";
    $("reviewTip").textContent=currentReview.tip||"";
    $("reviewAnswer").hidden=true;$("reviewActions").hidden=true;
    $("flipHint").textContent="点击卡片揭示中文含义";
    $("reviewProgress").textContent=`剩余 ${list.length} 张`;
  }
  function flip(){
    if(!currentReview||revealed)return;
    revealed=true;$("reviewAnswer").hidden=false;$("reviewActions").hidden=false;
    $("flipHint").textContent="依据实际记忆情况选择下面的评分";
  }
  function displaySettings(key){
    const mapping={
      setEnabled:"enabled",setVideo:"videoEnabled",setSelection:"selectionEnabled",
      setFloating:"floatingEnabled",setAiEnabled:"aiEnabled"
    };
    for(const [id,name] of Object.entries(mapping))$(id).checked=!!settings[name];
    const values={
      setLevel:settings.level,setIntensity:settings.intensity,setMaxTerms:settings.maxTerms,
      setPageMode:settings.pageMode,setVideoMode:settings.videoMode,
      setApiUrl:settings.apiUrl,setApiModel:settings.aiModel,
      setDailyLimit:settings.aiDailyMax,
      setSites:settings.disabledSites.join("\n")
    };
    for(const [id,value] of Object.entries(values))$(id).value=value;
    $("setIntensityVal").textContent=settings.intensity+"%";
    $("savedKeyState").textContent=key?"本机已有 API Key · 留空保持原密钥":"当前没有保存 API Key";
    $("setApiKey").value="";
  }
  function apiPattern(raw){
    const url=new URL(raw);
    const local=["localhost","127.0.0.1"].includes(url.hostname);
    if(url.username||url.password||(url.protocol!=="https:"&&!(local&&url.protocol==="http:")) || url.pathname==="/")
      throw Error("地址无效：仅允许 HTTPS 或本机 HTTP，并需要具体 API 路径");
    return `${url.protocol}//${url.hostname}/*`;
  }
  async function saveSettings(){
    const current=await chrome.storage.local.get("settings");
    const prev=C.validSettings(current.settings);
    const cfg={...prev,
      enabled:$("setEnabled").checked,level:Number($("setLevel").value),
      intensity:Number($("setIntensity").value),maxTerms:Number($("setMaxTerms").value),
      pageMode:$("setPageMode").value,videoEnabled:$("setVideo").checked,
      videoMode:$("setVideoMode").value,selectionEnabled:$("setSelection").checked,
      floatingEnabled:$("setFloating").checked,aiEnabled:$("setAiEnabled").checked,
      apiUrl:$("setApiUrl").value.trim(),aiModel:$("setApiModel").value.trim(),
      aiDailyMax:Number($("setDailyLimit").value),
      disabledSites:$("setSites").value.split(/[\r\n,]+/).map(s=>s.trim().replace(/^https?:\/\//,"").replace(/\/.*$/,"")).filter(Boolean)
    };
    if(cfg.aiEnabled)apiPattern(cfg.apiUrl);
    settings=C.validSettings(cfg);
    const batch={settings};
    if($("setApiKey").value.trim())batch.apiSecret=$("setApiKey").value.trim();
    await chrome.storage.local.set(batch);
    const isKey=!!((await chrome.storage.local.get("apiSecret")).apiSecret);
    $("savedKeyState").textContent=isKey?"本机已有 API Key · 留空保持原密钥":"当前没有保存 API Key";
    $("setApiKey").value="";
    $("apiMessage").textContent="设置已保存。正在打开的页面会自动更新。";
    showToast("学习设置保存成功");
  }
  async function grant(){
    const pattern=apiPattern($("setApiUrl").value.trim());
    // Must remain inside user gesture. Permission is granted only for the selected API origin.
    const yes=await chrome.permissions.request({origins:[pattern]});
    $("apiMessage").textContent=yes?`已授权访问：${pattern}`:"已取消 API 域名授权";
  }
  async function testAI(){
    $("apiMessage").textContent="正在测试 AI 接口…";
    try{
      await saveSettings();
      const response=await send("AI_EXPLAIN",{term:"project",context:"I am starting a new project."});
      $("apiMessage").textContent=`连接成功：${response.meaning||"AI 已返回结构化结果"}`;
      showToast("AI 连接正常");
    }catch(e){$("apiMessage").textContent="测试失败："+e.message;}
  }
  function exportData(){
    const data={schema:"flowlingo-backup-v1",savedAt:new Date().toISOString(),settings,words,stats};
    const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json;charset=utf-8"});
    const url=URL.createObjectURL(blob),a=document.createElement("a");
    a.href=url;a.download=`flowlingo-backup-${new Date().toISOString().slice(0,10)}.json`;
    document.body.append(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),3000);
    $("dataMessage").textContent="已导出本地备份（不包含 API Key）。";
  }
  async function importData(file){
    if(!file || file.size>3_000_000)throw Error("文件过大（上限 3 MB）");
    const data=JSON.parse(await file.text());
    if(data.schema!=="flowlingo-backup-v1"||typeof data.words!=="object"||!data.words)
      throw Error("不是有效的 FlowLingo 备份文件");
    const clean={};
    for(const row of Object.values(data.words).slice(0,2400)){
      if(!row||typeof row.term!=="string")continue;
      const term=C.clip(row.term,120).trim();if(!term)continue;
      clean[C.keyOf(term)]={
        term,meaning:C.clip(row.meaning,150),tip:C.clip(row.tip,220),
        context:C.clip(row.context,350),url:C.clip(row.url,300),
        level:C.clamp(row.level||2,1,5),status:row.status==="known"?"known":"learning",
        createdAt:Number(row.createdAt)||Date.now(),dueAt:Number(row.dueAt)||Date.now(),
        ease:C.clamp(row.ease||2.5,1.3,3.2),interval:Math.max(0,Number(row.interval)||0),
        repetitions:Math.max(0,Number(row.repetitions)||0),
        reviewCount:Math.max(0,Number(row.reviewCount)||0),seen:Math.max(0,Number(row.seen)||0)
      };
    }
    if(!window.confirm(`导入 ${Object.keys(clean).length} 条生词记录？将覆盖当前本机生词本。`))return;
    const importedSettings=C.validSettings({...settings,...data.settings,apiUrl:settings.apiUrl,apiModel:settings.aiModel});
    importedSettings.aiEnabled=false;
    const safeStats={
      exposures:Math.max(0,Number(data.stats?.exposures)||0),
      reviews:Math.max(0,Number(data.stats?.reviews)||0),
      daily:data.stats?.daily&&typeof data.stats.daily==="object"
        ?Object.fromEntries(Object.entries(data.stats.daily).filter(([k,v])=>/^\d{4}-\d\d-\d\d$/.test(k)&&Number.isFinite(Number(v))).slice(-45)):{}
    };
    await chrome.storage.local.set({words:clean,stats:safeStats,settings:importedSettings});
    $("dataMessage").textContent=`已导入 ${Object.keys(clean).length} 条记录。AI 已安全关闭，请按需重新开启。`;
    await reload();showToast("学习数据已导入");
  }

  document.querySelectorAll("[data-tab]").forEach(b=>b.addEventListener("click",()=>showTab(b.dataset.tab)));
  $("goReview").onclick=()=>showTab("review");
  $("showWords").onclick=()=>showTab("vocabulary");
  $("wordSearch").oninput=renderWords;
  $("wordFilter").onchange=renderWords;
  $("flipCard").onclick=flip;
  $("flipCard").onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();flip();}};
  document.querySelectorAll("[data-grade]").forEach(button=>button.addEventListener("click",async()=>{
    if(!currentReview)return;
    const term=currentReview.term;
    try{
      await send("UPDATE_WORD",{data:{term,action:"review",quality:Number(button.dataset.grade)}});
      visitedReview.add(C.keyOf(term));
      const result=await chrome.storage.local.get(["words","stats"]);
      words=result.words||{};stats=result.stats||stats;
      renderAll();nextReview();
    }catch(e){showToast(e.message);}
  }));
  $("setIntensity").oninput=()=>{$("setIntensityVal").textContent=$("setIntensity").value+"%";};
  $("saveSettings").onclick=async()=>{try{await saveSettings();}catch(e){$("apiMessage").textContent=e.message;}};
  $("grantPermission").onclick=async()=>{try{await grant();}catch(e){$("apiMessage").textContent=e.message;}};
  $("testAI").onclick=testAI;
  $("removeKey").onclick=async()=>{
    await chrome.storage.local.remove("apiSecret");
    $("setApiKey").value="";$("savedKeyState").textContent="已清除当前设备的 API Key";
    showToast("密钥已删除");
  };
  $("exportData").onclick=exportData;
  $("importData").onchange=async e=>{
    try{await importData(e.target.files?.[0]);}
    catch(error){$("dataMessage").textContent="导入失败："+error.message;}
    finally{e.target.value="";}
  };
  try{await reload();}catch(e){showToast("初始化失败："+e.message);}
  showTab("overview");
})();
