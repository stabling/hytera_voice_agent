const assert=require("assert");
const fs=require("fs"),vm=require("vm"),path=require("path");
const sandbox={console,globalThis:null};sandbox.globalThis=sandbox;
for(const f of ["dictionary.js","core.js"]){
  const text=fs.readFileSync(path.join(__dirname,"..","scripts",f),"utf8");
  vm.runInNewContext(text,sandbox,{filename:f});
}
const C=sandbox.FlowCore,D=sandbox.FlowDictionary;
assert(D.length>250,"offline word bank");
const zh="我今天正在学习新技术，分析市场需求、了解成本，并为项目制定计划。";
const en="Technology and automation improve productivity, while user experience and feedback remain important.";
const opt={level:3,intensity:50,maxTerms:5};
const ch=C.planLocal(zh,"zh",opt,{},D);
const eng=C.planLocal(en,"en",opt,{},D);
assert(ch.length>0,"Chinese replacement available");
assert(eng.length>0,"English annotations available");
assert(ch.every(x=>zh.slice(x.start,x.end)===x.source));
assert(eng.every(x=>en.slice(x.start,x.end)===x.source));
assert(ch.every((x,i)=>!i||x.start>=ch[i-1].end));
const known=Object.fromEntries(D.map(x=>[C.keyOf(x.en),"known"]));
assert.strictEqual(C.planLocal(zh,"zh",opt,known,D).length,0,"known terms skipped");
assert.strictEqual(C.planLocal("你好","zh",opt,{},D).length,0,"no unmatched content");
const invalid=C.planAI("我要开发项目","zh",[
  {source:"完全不存在",english:"not here"},
  {source:"项目",english:"project",level:2},
  {source:"项目",english:"wrong duplicate"},
  {source:"开发",english:"<script>alert(1)</script>"}
],{maxTerms:5});
assert.strictEqual(invalid.length,2,"AI sources validated/deduped"); // HTML is textContent-rendered, not executable
assert(C.isBlocked("mail.google.com",[]));
assert(C.isBlocked("sub.example.com",["example.com"]));
assert(!C.isBlocked("example.com.evil.net",["example.com"]));
const start=1_700_000_000_000;
const row={term:"project",status:"learning",ease:2.5};
const a=C.reviewWord(row,4,start);
const b=C.reviewWord(a,5,start+C.DAY);
const c=C.reviewWord(b,4,start+4*C.DAY);
assert(a.interval===1&&b.interval===3&&c.interval>=3);
assert(c.dueAt>start+4*C.DAY);
const wrong=C.reviewWord(c,1,start+5*C.DAY);
assert(wrong.repetitions===0&&wrong.interval===1);
console.log("PASS: dictionary size="+D.length+"; local transformation, AI plan sanitation, blocklist, SRS and edge cases");
