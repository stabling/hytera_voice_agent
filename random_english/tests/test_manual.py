# -*- coding: utf-8 -*-
"""用真实 Chromium DOM 和模拟 Chrome 扩展后台测试手动划词翻译功能。"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
def read(path):return (ROOT/path).read_text(encoding="utf-8")
async def inject(page,*paths):
    for path in paths:await page.add_script_tag(content=read(path))
async def set_selection(page,selector,phrase=None,across=False):
    return await page.evaluate("""({selector,phrase,across})=>{
      const el=document.querySelector(selector),r=document.createRange();
      if(across){
        r.setStart(el.firstChild,0);
        r.setEnd(el.lastChild,el.lastChild.nodeValue.length);
      }else {
        let n=el.firstChild;
        if(n.nodeType!==Node.TEXT_NODE)n=el.querySelector("*").firstChild;
        const start=phrase?n.nodeValue.indexOf(phrase):0;
        if(start<0)throw Error("选择源文本中没有对应短语");
        r.setStart(n,start);r.setEnd(n,start+(phrase||n.textContent).length);
      }
      const sel=window.getSelection();sel.removeAllRanges();sel.addRange(r);
      document.dispatchEvent(new MouseEvent("mouseup",{bubbles:true}));
      return sel.toString();
    }""",{"selector":selector,"phrase":phrase,"across":across})
async def shadow_text(page,sel):
    return await page.locator("#flowlingo-root").evaluate(
       "(h,s)=>h.shadowRoot.querySelector(s)?.textContent",sel)
async def click_shadow(page,sel):
    return await page.locator("#flowlingo-root").evaluate(
       "(h,s)=>h.shadowRoot.querySelector(s).click()",sel)

async def main():
    passed=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path="/usr/bin/chromium",headless=True,args=["--no-sandbox"])
        page=await browser.new_page(viewport={"width":1180,"height":860},device_scale_factor=1)
        await page.set_content("""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>
          <body style="font:20px/1.8 sans-serif;padding:50px">
          <p id="one">我想了解市场需求，选择真正有价值的项目。</p>
          <p id="cross">这项<strong>技术</strong>非常重要</p>
          <p id="sentence">学习是每天生活的一部分。</p>
          <p id="long">"""+"这是一个很长的中文句子，用于测试保护网页排版的边界情况。"*7+"""</p>
          <p id="unknown">这是一个不在词典里的新概念。</p>
          <div contenteditable="true" id="editable">我们需要学习新的技术。</div>
          <a id="link" href="#">学习新的项目</a>
          <button id="original-button">原网页按钮</button>
          <p id="counter">0</p>
          <script>document.getElementById("original-button").onclick=()=>{
            document.getElementById("counter").textContent=String(Number(document.getElementById("counter").textContent)+1)
          };</script></body></html>""")
        await inject(page,"tests/mock_chrome.js","scripts/dictionary.js","scripts/core.js")
        await page.evaluate("""async()=>{
          const {settings}=await chrome.storage.local.get("settings");
          await chrome.storage.local.set({settings:{...settings,
            enabled:true,pageMode:"manual",manualSelectionEnabled:true,
            showOriginalInBrackets:true,aiEnabled:false}});
        }""")
        await page.add_style_tag(content=read("scripts/content.css"))
        await inject(page,"scripts/content.js")
        await page.wait_for_timeout(450)
        assert await page.locator(".fl-inline-mark").count()==0
        passed.append("只手动模式：不会自动修改普通网页正文")

        await set_selection(page,"#one","市场需求")
        await page.wait_for_function("""()=>{
          return document.querySelector("#flowlingo-root")?.shadowRoot.querySelector(".select-action").style.display==="block";
        }""",timeout=6000)
        assert "译成英文" in await shadow_text(page,".select-action")
        await click_shadow(page,".select-action")
        await page.wait_for_function("""()=>{
          return document.querySelector("#flowlingo-root")?.shadowRoot.querySelector(".manual-result")?.textContent==="market demand";
        }""",timeout=8000)
        assert "本地词典" in await shadow_text(page,".manual-status")
        passed.append("选中文本后出现快捷按钮，离线词库能翻译指定中文短语")

        await click_shadow(page,".manual-card .btn.primary")
        assert "market demand（市场需求）" in await page.locator("#one").inner_text()
        assert await page.locator("#one .fl-manual-mark").count()==1
        await page.locator("#original-button").click()
        assert await page.locator("#counter").inner_text()=="1"
        passed.append("译文插入网页并保留中文括号，原网页事件保持可用")
        # Undo from the active manual card:
        await page.locator("#flowlingo-root").evaluate("""h=>{
          const btn=[...h.shadowRoot.querySelectorAll(".manual-card button")].find(b=>b.textContent.includes("撤销"));
          btn.click();
        }""")
        assert "市场需求" in await page.locator("#one").inner_text()
        assert await page.locator("#one .fl-manual-mark").count()==0
        passed.append("单击撤销后恢复原文，清除插入的标记")

        # Native browser translation mock works for sentences not in glossary.
        await page.evaluate("""()=>window.Translator={
           async create(){return {async translate(t){return "Learning is part of everyday life.";}}}
        }""")
        await set_selection(page,"#sentence")
        await page.wait_for_timeout(160)
        await click_shadow(page,".select-action")
        await page.wait_for_timeout(900)
        await page.wait_for_function("""()=>{
          const n=document.querySelector("#flowlingo-root")?.shadowRoot.querySelector(".manual-result");
          return n?.textContent==="Learning is part of everyday life.";
        }""",timeout=8500)
        assert "浏览器内置翻译" in await shadow_text(page,".manual-status")
        await click_shadow(page,".manual-card .btn.primary")
        assert "Learning is part of everyday life.（学习是每天生活的一部分。）" in await page.locator("#sentence").inner_text()
        passed.append("模拟 Chrome 内置翻译能翻译完整中文句子并插入网页")
        # Site-level disable clears all manual insertions.
        await page.evaluate("""async()=>{
          const {settings}=await chrome.storage.local.get("settings");
          await chrome.storage.local.set({settings:{...settings,enabled:false}});
        }""")
        await page.wait_for_timeout(600)
        assert await page.locator("#sentence").inner_text()=="学习是每天生活的一部分。"
        assert await page.locator(".fl-manual-mark").count()==0
        passed.append("关闭网页学习时，手动插入内容也能恢复原文")

        # Reenable then select across nested HTML; translation card works,
        # but inserting into page must be disabled to preserve semantics.
        await page.evaluate("""async()=>{
          const {settings}=await chrome.storage.local.get("settings");
          await chrome.storage.local.set({settings:{...settings,enabled:true}});
        }""")
        await page.wait_for_timeout(300)
        await set_selection(page,"#cross",across=True)
        await page.wait_for_timeout(160)
        await click_shadow(page,".select-action")
        await page.wait_for_function("""()=>{
          return document.querySelector("#flowlingo-root")?.shadowRoot.querySelector(".manual-result")?.textContent==="Learning is part of everyday life.";
        }""",timeout=6500)
        disabled=await page.locator("#flowlingo-root").evaluate("""h=>{
          return [...h.shadowRoot.querySelectorAll(".manual-card button")]
            .find(b=>b.textContent.includes("插入网页")).disabled;
        }""")
        assert disabled
        assert await page.locator("#cross strong").inner_text()=="技术"
        passed.append("跨 HTML 标签的选区仅显示译文，不破坏元素结构")

        # Editing / links are intentionally ignored.
        await set_selection(page,"#editable","学习新的技术")
        await page.wait_for_timeout(200)
        visible=await page.locator("#flowlingo-root").evaluate(
            'h=>h.shadowRoot.querySelector(".select-action").style.display==="block"')
        assert not visible
        await set_selection(page,"#link","学习新的项目")
        await page.wait_for_timeout(200)
        visible=await page.locator("#flowlingo-root").evaluate(
            'h=>h.shadowRoot.querySelector(".select-action").style.display==="block"')
        assert not visible
        passed.append("编辑区与网页链接选区不展示手动翻译入口")

        # No local glossary, built-in Translator or cloud key -> an honest error.
        await page.evaluate("()=>{delete window.Translator;}")
        await set_selection(page,"#unknown")
        await page.wait_for_timeout(150)
        await click_shadow(page,".select-action")
        await page.wait_for_function("""()=>{
          return document.querySelector("#flowlingo-root")?.shadowRoot.querySelector(".manual-result")?.textContent.includes("暂时无法翻译");
        }""",timeout=7000)
        assert await page.locator("#unknown").inner_text()=="这是一个不在词典里的新概念。"
        passed.append("没有可用翻译引擎时不伪造结果，提示配置智能模型")

        # Cloud fallback with only selected text, using a mock service.
        await page.evaluate("""async()=>{
          const old=chrome.runtime.sendMessage;
          chrome.runtime.sendMessage=async msg=>{
            if(msg.type==="AI_MANUAL_TRANSLATE"){
              window.__cloudManualSent=msg.text;
              return {ok:true,data:{translation:"A newly discovered concept.",tip:"contextual expression",method:"ai"}};
            }
            return old(msg);
          };
          const {settings}=await chrome.storage.local.get("settings");
          await chrome.storage.local.set({settings:{...settings,aiEnabled:true}});
        }""")
        await page.wait_for_timeout(320)
        await set_selection(page,"#unknown")
        await page.wait_for_timeout(150)
        await click_shadow(page,".select-action")
        await page.wait_for_function("""()=>{
          return document.querySelector("#flowlingo-root")?.shadowRoot.querySelector(".manual-result")?.textContent==="A newly discovered concept.";
        }""",timeout=7000)
        assert await page.evaluate("__cloudManualSent")=="这是一个不在词典里的新概念。"
        assert "自己配置的智能模型" in await shadow_text(page,".manual-status")
        passed.append("智能模型后备翻译只发送所选中文字，不附带页面正文")

        await page.close()
        await browser.close()
    for item in passed:print("通过："+item)
    print("手动划词翻译功能测试全部通过")

if __name__=="__main__":
    asyncio.run(main())
