(async()=>{
  "use strict";
  const C=globalThis.FlowCore;
  const $=id=>document.getElementById(id);
  let settings=C.validSettings({}),site="";
  async function save(){
    try{await chrome.storage.local.set({settings});setStatus();}
    catch(e){$("note").textContent="设置保存失败："+e.message;}
  }
  function setStatus(){
    const active=settings.enabled&&(!site||!C.isBlocked(site,settings.disabledSites));
    $("statusPill").textContent=active?"学习中":"已暂停";
    $("statusPill").classList.toggle("off",!active);
  }
  function refresh(){
    $("enabled").checked=settings.enabled;
    $("intensity").value=settings.intensity;
    $("intensityVal").textContent=settings.intensity+"%";
    $("level").value=settings.level;
    $("pageMode").value=settings.pageMode;
    $("videoEnabled").checked=settings.videoEnabled;
    $("siteEnabled").checked=site?!C.isBlocked(site,settings.disabledSites):false;
    $("siteEnabled").disabled=!site;
    setStatus();
  }
  try{
    const [storage,tabs]=await Promise.all([
      chrome.storage.local.get(["settings","words","stats"]),
      chrome.tabs.query({active:true,currentWindow:true})
    ]);
    settings=C.validSettings(storage.settings);
    try{const url=new URL(tabs[0]?.url||"");site=url.protocol.startsWith("http")?url.hostname:"";}catch{}
    $("hostname").textContent=site||"当前不是可改造的网页";
    $("wordCount").textContent=Object.keys(storage.words||{}).length.toLocaleString("zh-CN");
    $("exposures").textContent=(storage.stats?.exposures||0).toLocaleString("zh-CN");
    $("dueCount").textContent=Object.values(storage.words||{}).filter(w=>w&&w.status!=="known"&&(w.dueAt||0)<=Date.now()).length.toLocaleString("zh-CN");
    $("note").textContent=settings.aiEnabled?"AI 语境模式已开启":"离线词典已就绪 · AI 可选";
    refresh();
  }catch(e){$("note").textContent="读取配置失败："+e.message;}
  $("enabled").addEventListener("change",()=>{settings.enabled=$("enabled").checked;save();});
  $("intensity").addEventListener("input",()=>{$("intensityVal").textContent=$("intensity").value+"%";});
  $("intensity").addEventListener("change",()=>{settings.intensity=Number($("intensity").value);save();});
  $("level").addEventListener("change",()=>{settings.level=Number($("level").value);save();});
  $("pageMode").addEventListener("change",()=>{settings.pageMode=$("pageMode").value;save();});
  $("videoEnabled").addEventListener("change",()=>{settings.videoEnabled=$("videoEnabled").checked;save();});
  $("siteEnabled").addEventListener("change",()=>{
    if(!site)return;
    const blocked=new Set(settings.disabledSites);
    if($("siteEnabled").checked)blocked.delete(site);
    else blocked.add(site);
    settings.disabledSites=[...blocked];
    save();refresh();
  });
  $("openDashboard").addEventListener("click",async()=>{
    await chrome.tabs.create({url:chrome.runtime.getURL("ui/dashboard.html")});
    window.close();
  });
})();
