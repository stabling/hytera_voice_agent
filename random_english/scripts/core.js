/* Pure matching and spaced-repetition helpers shared by content and tests. */
(() => {
  "use strict";
  const RX_ZH = /[\u3400-\u9fff]/;
  const RX_EN = /[A-Za-z]{3}/;
  const DAY = 86400000;
  const defaults = Object.freeze({
    enabled: true, level: 2, intensity: 18, maxTerms: 3,
    pageMode: "auto", videoEnabled: true, videoMode: "bilingual",
    selectionEnabled: true, floatingEnabled: true, showOriginalInBrackets: true,
    manualSelectionEnabled: true,
    aiEnabled: false, apiUrl: "https://api.openai.com/v1/chat/completions",
    aiModel: "gpt-4o-mini", aiDailyMax: 50,
    disabledSites: [], theme: "auto",
  });
  const clip = (x,n=160) => String(x??"").slice(0,n);
  const clamp = (x,min,max) => Math.min(max,Math.max(min,Number(x)||min));
  const keyOf = str => clip(str,120).toLowerCase().trim().replace(/\s+/g," ");
  const isChinese = s => (String(s).match(/[\u3400-\u9fff]/g)||[]).length >= 2;
  const langOf = s => isChinese(s) ? "zh" : (RX_EN.test(s) ? "en" : null);
  const regexEscape = s => s.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
  const hash = value => {
    let h=2166136261;
    for (let i=0;i<value.length;i++) h=Math.imul(h ^ value.charCodeAt(i),16777619);
    return h>>>0;
  };
  function isBlocked(host,disabledSites=[]){
    host=String(host||"").toLowerCase();
    const sensitive=["mail.google.com","outlook.live.com","outlook.office.com",
       "docs.google.com","drive.google.com","accounts.google.com",
       "login.microsoftonline.com","paypal.com","stripe.com"];
    return [...sensitive,...disabledSites].some(s=>{
      s=String(s).toLowerCase().replace(/^https?:\/\//,"").split("/")[0].trim();
      return s && (host===s || host.endsWith("."+s));
    });
  }
  function validSettings(obj){
    const s={...defaults,...(obj && typeof obj==="object" ? obj : {})};
    s.level=clamp(s.level,1,5);
    s.intensity=clamp(s.intensity,3,55);
    s.maxTerms=clamp(s.maxTerms,1,6);
    s.aiDailyMax=clamp(s.aiDailyMax,1,200);
    s.disabledSites=Array.isArray(s.disabledSites) ? s.disabledSites.filter(x=>typeof x==="string").slice(0,300) : [];
    s.pageMode=["auto","zh","en","manual"].includes(s.pageMode)?s.pageMode:"auto";
    s.videoMode=["bilingual","mix"].includes(s.videoMode)?s.videoMode:"bilingual";
    s.apiUrl=clip(s.apiUrl,240);
    s.aiModel=clip(s.aiModel,90);
    return s;
  }

  function manualLookup(text, entries=globalThis.FlowDictionary||[]){
    const source=String(text??"").trim();
    if(!source || source.length>350 || !isChinese(source))return null;
    // Manual translation is only considered "complete" on an exact phrase match.
    // Never stitch unrelated glossary entries into a possibly incorrect sentence.
    const matches=entries.filter(x=>x.cn===source && typeof x.en==="string" && x.en.trim());
    if(!matches.length)return null;
    const row=matches.sort((a,b)=>(a.level||3)-(b.level||3))[0];
    return {translation:String(row.en).trim(),tip:String(row.tip||""),
      original:source,method:"local",level:row.level||2};
  }
  function validManualTranslation(original,candidate){
    const src=String(original||"").trim();
    const value=String(candidate||"").replace(/^[\s"'`]+|[\s"'`]+$/g,"").trim();
    if(!src||!value || src===value || value.length>1200)return "";
    // Reject responses that contain mostly Chinese or HTML-like tags.
    if(!/[A-Za-z]/.test(value) || /<\/?[a-z][^>]*>/i.test(value))return "";
    if((value.match(/[\u3400-\u9fff]/g)||[]).length>value.length*.35)return "";
    const suffix=new RegExp("\\s*[（(]\\s*"+regexEscape(src)+"\\s*[）)]\\s*$","u");
    return value.replace(suffix,"").trim();
  }
  function planLocal(text, mode, settings={},known={},entries=globalThis.FlowDictionary||[]){
    const s=validSettings(settings);
    if(!text || !["zh","en"].includes(mode)) return [];
    const limit=Math.min(s.maxTerms,Math.max(1,Math.ceil(text.length*s.intensity/100/7)));
    const available=entries.filter(e=>{
      if(e.level>s.level+1) return false;
      if(known[keyOf(e.en)] === "known") return false;
      return mode==="zh" ? text.includes(e.cn) : text.toLowerCase().includes(e.en.toLowerCase());
    });
    const cands=[];
    for(const row of available){
      const needle=mode==="zh" ? row.cn : row.en;
      const rx = new RegExp(mode==="en"
          ? `(^|[^\\p{L}])(${regexEscape(needle)})(?=$|[^\\p{L}])`
          : regexEscape(needle),mode==="en"?"giu":"g");
      let match;
      while ((match=rx.exec(text)) !== null){
        const start=mode==="en" ? match.index + match[1].length : match.index;
        const actual=mode==="en" ? match[2] : match[0];
        const end=start+actual.length;
        const near=5-Math.abs((s.level+1)-row.level);
        const score=near*100 + Math.min(needle.length,16)*11+
              (hash(`${text.slice(0,25)}-${needle}-${start}`)%53);
        cands.push({
          start,end,source:actual,english:mode==="zh"?row.en:actual,
          meaning:row.cn,tip:row.tip,level:row.level,
          mode,score
        });
        if(cands.length>400) break;
        if(match[0].length===0) rx.lastIndex++;
      }
    }
    cands.sort((a,b)=>b.score-a.score || a.start-b.start);
    const used=[];
    for(const item of cands){
      if(used.length>=limit) break;
      if(used.some(x=>Math.max(item.start,x.start)<Math.min(item.end,x.end))) continue;
      // Keep original-language sentence almost entirely intact.
      used.push(item);
    }
    return used.sort((a,b)=>a.start-b.start);
  }

  // Keep the exact source visible beside translated English on Chinese webpages.
  // If AI already appended the same Chinese source in parentheses, normalize it.
  function bilingualDisplay(english,source,mode,showOriginal=true){
    const origin=String(source??"").trim();
    let translation=String(english??"").trim();
    if(mode!=="zh" || !origin)return {translation,original:""};
    const already=new RegExp("\\s*[（(]\\s*"+regexEscape(origin)+"\\s*[）)]\\s*$","u");
    const cleaned=translation.replace(already,"").trim();
    if(cleaned)translation=cleaned;
    const append=Boolean(showOriginal)&&Boolean(translation)&&translation!==origin;
    return {translation,original:append?origin:""};
  }
  function planAI(text,mode,raw=[],settings={}){
    const limit=clamp(validSettings(settings).maxTerms,1,6);
    const values=Array.isArray(raw)?raw:[];
    const out=[];
    for(const item of values){
      if(out.length>=limit || !item || typeof item!=="object") break;
      const source=clip(item.source,90);
      const english=clip(item.english || source,120);
      if(!source || !english || source.trim().length<1) continue;
      if(mode==="zh" && !/[A-Za-z]/.test(english)) continue;
      const start=text.indexOf(source);
      if(start<0) continue;
      const end=start+source.length;
      if(out.some(x=>Math.max(start,x.start)<Math.min(end,x.end))) continue;
      out.push({
        start,end,source,english:mode==="en"?source:english,
        meaning:clip(item.meaning||source,100),
        tip:clip(item.tip||"",200),
        level:clamp(item.level||2,1,5),mode
      });
    }
    return out.sort((a,b)=>a.start-b.start);
  }
  function reviewWord(row,quality,now=Date.now()){
    const q=clamp(quality,0,5);
    let ease=clamp(row.ease || 2.5,1.3,3.2);
    let repetitions=Number(row.repetitions)||0;
    let interval=Number(row.interval)||0;
    if(q<3){ repetitions=0; interval=1; }
    else {
      interval=repetitions===0?1:repetitions===1?3:Math.max(1,Math.round(interval*ease));
      repetitions++;
    }
    ease=Math.max(1.3,ease+(0.1-(5-q)*(0.08+(5-q)*0.02)));
    return {...row,repetitions,interval,ease,
      dueAt:now+interval*DAY,reviewedAt:now,reviewCount:(row.reviewCount||0)+1,
      status:q>=4&&repetitions>=3?"known":"learning"};
  }
  globalThis.FlowCore=Object.freeze({
    defaults,DAY,clip,clamp,keyOf,langOf,isChinese,isBlocked,
    validSettings,manualLookup,validManualTranslation,planLocal,planAI,bilingualDisplay,reviewWord,hash
  });
})();
