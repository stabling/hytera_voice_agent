const vm=require("vm"),fs=require("fs"),path=require("path"),assert=require("assert");
(async()=>{
  const root=path.join(__dirname,"..");
  const db={
    settings:{enabled:true,level:2,intensity:18,maxTerms:3,aiEnabled:true,
      apiUrl:"https://api.example.com/v1/chat/completions",
      aiModel:"mock-model",aiDailyMax:6,disabledSites:[]},
    words:{},stats:{exposures:0,reviews:0,aiUses:{}},apiSecret:"test-secret"
  };
  const changes=[];
  let onMessage=null,onChanged=null,onInstalled=null,onCommand=null,onMenuClick=null;
  let requestCount=0,sentPayload=null;
  const registeredMenus=[],forwardedMessages=[];
  const chrome={
    storage:{
      local:{
        async get(keys){
          const result={};
          for(const key of typeof keys==="string"?[keys]:keys||Object.keys(db))
            if(db[key]!==undefined)result[key]=db[key];
          return result;
        },
        async set(obj){
          const cs={};
          for(const [key,val] of Object.entries(obj)){cs[key]={oldValue:db[key],newValue:val};db[key]=val;}
          if(onChanged)onChanged(cs,"local");
        },
        async setAccessLevel(){},
        async remove(key){delete db[key];}
      },
      onChanged:{addListener(fn){onChanged=fn;}}
    },
    runtime:{
      onInstalled:{addListener(fn){onInstalled=fn;}},
      onMessage:{addListener(fn){onMessage=fn;}},
      getURL(x){return "chrome-extension://mock/"+x;}
    },
    commands:{onCommand:{addListener(fn){onCommand=fn;}}},
    contextMenus:{
      onClicked:{addListener(fn){onMenuClick=fn;}},
      removeAll(cb){cb();},
      create(entry){registeredMenus.push(entry);}
    },
    tabs:{
      async query(){return [{id:8}];},
      async sendMessage(id,msg){forwardedMessages.push({id,msg});},
      async create(){}
    },
    permissions:{async contains(){return true;}}
  };
  const sandbox={console,URL,Promise,setTimeout,clearTimeout,AbortController,Date,
    chrome,globalThis:null,
    importScripts(...paths){
      for(const p of paths)vm.runInContext(fs.readFileSync(path.join(root,"scripts",p),"utf8"),context,{filename:p});
    },
    async fetch(url,options){
      requestCount++;sentPayload=JSON.parse(options.body);
      const selected=sentPayload?.messages?.[1]?.content||"";
      const responseBody=selected.includes("UNTRUSTED_SELECTED_TEXT")?
        {translation:"This is a new project.",tip:"情境翻译"}:
        {items:[{id:"0",replacements:[{source:"项目",english:"project",meaning:"项目",tip:"natural context",level:2}]}]};
      return {ok:true,async json(){return {choices:[{message:{content:JSON.stringify(responseBody)}}]};}};
    }
  };
  sandbox.globalThis=sandbox;
  const context=vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(root,"scripts/background.js"),"utf8"),context,{filename:"background.js"});
  const dispatch=message=>new Promise(resolve=>onMessage(message,{tab:{id:7}},resolve));
  let response=await dispatch({type:"GET_STATE"});
  assert(response.ok&&response.data.settings.aiEnabled);
  response=await dispatch({type:"AI_TRANSFORM",blocks:[{id:"0",text:"这个项目需要完善",mode:"zh"}]});
  assert(response.ok&&response.data.items[0].replacements[0].english==="project");
  assert(requestCount===1&&sentPayload.model==="mock-model");
  response=await dispatch({type:"PATCH_SETTINGS",changes:{intensity:22,apiUrl:"https://evil.example/"}} );
  assert(response.ok&&db.settings.intensity===22&&db.settings.apiUrl.includes("api.example.com"),
    "untrusted content cannot change API provider");
  response=await dispatch({type:"SAVE_WORD",data:{term:"project",meaning:"项目"}});
  assert(response.ok&&db.words.project.term==="project");
  response=await dispatch({type:"UPDATE_WORD",data:{term:"project",action:"review",quality:4}});
  assert(response.ok&&db.words.project.interval===1);
  response=await dispatch({type:"AI_EXPLAIN",term:"project",context:"hello"});
  // Fake server response isn't an explain object; it is valid JSON but we focus on route and isolation.
  assert(response.ok&&requestCount===2);
  response=await dispatch({type:"AI_MANUAL_TRANSLATE",text:"这是一个新项目。"});
  assert(response.ok && response.data.translation==="This is a new project.");
  assert(sentPayload.messages[1].content.includes("这是一个新项目。"));
  assert(!sentPayload.messages[1].content.includes("hello"),"manual request is selection-only");
  await onInstalled();
  assert(registeredMenus.some(x=>x.id==="flow-translate-zh"));
  onMenuClick({menuItemId:"flow-translate-zh",selectionText:"市场需求",editable:false},{id:8});
  await new Promise(r=>setTimeout(r,40));
  assert(forwardedMessages.some(x=>x.msg.type==="MANUAL_TRANSLATE_SELECTION"&&x.msg.text==="市场需求"));
  await onCommand("manual-translate");
  assert(forwardedMessages.some(x=>x.msg.type==="MANUAL_TRANSLATE_SELECTION"));
  db.apiSecret="";
  response=await dispatch({type:"AI_TRANSFORM",blocks:[{id:"0",text:"学习市场知识",mode:"zh"}]});
  assert(!response.ok&&response.error.includes("模型接口密钥"));
  console.log("通过：后台启动、智能接口路由、密钥隔离、安全设置、生词保存及间隔复习");
})().catch(err=>{console.error(err);process.exitCode=1;});
