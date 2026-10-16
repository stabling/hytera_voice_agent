/* Browser DOM test bridge only. Not part of the production extension. */
(() => {
  const DB={
    settings:{
      enabled:true,level:3,intensity:38,maxTerms:5,pageMode:"auto",
      videoEnabled:true,videoMode:"bilingual",selectionEnabled:true,
      floatingEnabled:true,aiEnabled:false,apiUrl:"https://api.openai.com/v1/chat/completions",
      aiModel:"gpt-4o-mini",aiDailyMax:50,disabledSites:[]
    },
    words:{},stats:{exposures:0,reviews:0,daily:{}},apiSecret:""
  };
  const listeners=[];
  const local={
    async get(keys) {
      if(keys===null)return {...DB};
      const result={};
      for(const k of typeof keys==="string"?[keys]:keys||Object.keys(DB))
        if(DB[k]!==undefined)result[k]=DB[k];
      return result;
    },
    async set(items){
      const changed={};
      for(const [k,v] of Object.entries(items)){
        changed[k]={oldValue:DB[k],newValue:v};
        DB[k]=v;
      }
      for(const f of listeners)f(changed,"local");
    },
    async remove(key){delete DB[key];},
    async setAccessLevel(){}
  };
  globalThis.chrome={
    storage:{local,onChanged:{addListener(fn){listeners.push(fn);}}},
    runtime:{
      getURL(path){return "chrome-extension://mock/"+path;},
      onMessage:{addListener(){}},
      async sendMessage(msg){
        const d=msg?.data||{};
        if(msg.type==="GET_STATE")return {ok:true,data:{
          settings:DB.settings,known:{},stats:DB.stats,
          wordCount:Object.keys(DB.words).length,dueCount:Object.keys(DB.words).length}};
        if(msg.type==="GET_WORDS")return {ok:true,data:DB.words};
        if(msg.type==="SAVE_WORD"){
          DB.words[d.term.toLowerCase()]={term:d.term,meaning:d.meaning,tip:d.tip,
            status:"learning",context:d.context,createdAt:Date.now(),dueAt:Date.now()};
          return {ok:true,data:DB.words[d.term.toLowerCase()]};
        }
        if(msg.type==="UPDATE_WORD"){
          if(d.action==="remove")delete DB.words[d.term.toLowerCase()];
          else if(DB.words[d.term.toLowerCase()])DB.words[d.term.toLowerCase()].status=d.action;
          return {ok:true,data:true};
        }
        if(msg.type==="EXPOSURE"){DB.stats.exposures+=msg.count;return {ok:true,data:true};}
        if(msg.type==="AI_EXPLAIN")return {ok:false,error:"AI 默认关闭"};
        if(msg.type==="OPEN_DASHBOARD")return {ok:true,data:true};
        return {ok:false,error:"mock unexpected "+msg.type};
      }
    },
    tabs:{async query(){return [{url:"https://example.test/article"}]},async create(){return true;}},
    permissions:{async request(){return true;},async contains(){return true;}}
  };
  globalThis.__mockStore=DB;
})();
