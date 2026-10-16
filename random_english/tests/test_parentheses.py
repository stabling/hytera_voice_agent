# -*- coding: utf-8 -*-
# 使用真实 Chromium DOM + 模拟扩展运行时测试双语括号功能
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
def source(path):
    return (ROOT/path).read_text(encoding="utf-8")
async def inject(page,*files):
    for item in files:
        await page.add_script_tag(content=source(item))

async def main():
    passed=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(
            executable_path="/usr/bin/chromium",headless=True,args=["--no-sandbox"])
        ctx=await browser.new_context(viewport={"width":1220,"height":760})
        page=await ctx.new_page()
        await page.set_content('''<!doctype html><html lang="zh-CN"><body style="font:22px/1.8 sans-serif;margin:40px">
          <p id="zh">这是我正在努力完成的项目，请您看看这个项目。</p>
          <p id="other">这是项目（项目）的演示，请注意这里的项目（项目）。</p>
          <p id="en" lang="en">Our project requires accurate data and a reliable workflow for every team.</p>
          <textarea id="editor">我的项目需要更多数据。</textarea>
          </body></html>''')
        await inject(page,"tests/mock_chrome.js","scripts/dictionary.js","scripts/core.js")
        await page.evaluate('''()=>Object.assign(__mockStore.settings,{
          enabled:true,level:3,intensity:55,maxTerms:6,showOriginalInBrackets:true})''')
        await page.add_style_tag(content=source("scripts/content.css"))
        await inject(page,"scripts/content.js")
        await page.locator("#zh .fl-inline-mark").first.wait_for(timeout=11000)
        await page.wait_for_timeout(350)
        annotations=await page.locator("#zh .fl-inline-original").all_text_contents()
        assert len(annotations)>1 and all(t.startswith("（") and t.endswith("）") for t in annotations)
        original_text=await page.locator("#zh").inner_text()
        assert "project（项目）" in original_text,(original_text,annotations)
        passed.append("中文网页自动增加原文括号")

        duplicate=await page.locator("#other").inner_text()
        assert "project" in duplicate and "project（项目）（项目）" not in duplicate,duplicate
        passed.append("原网页已有括号时不重复追加")

        assert await page.locator("#en .fl-inline-mark").count()>=1
        assert await page.locator("#en .fl-inline-original").count()==0
        assert "我的项目需要更多数据。" in await page.locator("#editor").input_value()
        passed.append("英文网页及编辑区域不会错误改变")

        await page.locator("#zh .fl-inline-mark").first.click()
        await page.wait_for_function('''()=>{
          const h=document.querySelector("#flowlingo-root");
          return h?.shadowRoot.querySelector(".bubble").style.display==="block";
        }''')
        await page.locator("#flowlingo-root").evaluate('''host=>{
          const toggle=[...host.shadowRoot.querySelectorAll(".bubble button")]
            .find(b=>b.textContent.includes("仅显示中文原文"));
          if(!toggle)throw new Error("找不到切换按钮");
          toggle.click();
        }''')
        first_mark=page.locator("#zh .fl-inline-mark").first
        assert "fl-original" in (await first_mark.get_attribute("class"))
        assert "（" not in await first_mark.inner_text()
        await page.locator("#flowlingo-root").evaluate('''host=>{
          const toggle=[...host.shadowRoot.querySelectorAll(".bubble button")]
            .find(b=>b.textContent.includes("恢复双语显示"));
          toggle.click();
        }''')
        assert "（" in await first_mark.inner_text()
        assert await first_mark.locator(".fl-inline-original").count()<=1
        passed.append("原文切换与恢复不会叠加括号")

        await page.evaluate('''async()=>{
          const {settings}=await chrome.storage.local.get("settings");
          await chrome.storage.local.set({settings:{...settings,showOriginalInBrackets:false}});
        }''')
        await page.wait_for_timeout(700)
        assert await page.locator("#zh .fl-inline-original").count()==0
        assert "（项目）" not in await page.locator("#zh").inner_text()
        await page.evaluate('''async()=>{
          const {settings}=await chrome.storage.local.get("settings");
          await chrome.storage.local.set({settings:{...settings,showOriginalInBrackets:true}});
        }''')
        await page.locator("#zh .fl-inline-original").first.wait_for(timeout=10000)
        assert "project（项目）" in await page.locator("#zh").inner_text()
        passed.append("实时设置开关与重新渲染工作正常")

        original="这是我正在努力完成的项目，请您看看这个项目。"
        await page.evaluate('''async()=>{
          const {settings}=await chrome.storage.local.get("settings");
          await chrome.storage.local.set({settings:{...settings,enabled:false}});
        }''')
        await page.wait_for_timeout(700)
        assert await page.locator("#zh").inner_text()==original
        assert await page.locator("#zh .fl-inline-mark").count()==0
        passed.append("暂停学习可原样恢复网页文字")

        ai=await ctx.new_page()
        await ai.set_content('''<html lang="zh-CN"><body style="font:21px sans-serif;padding:40px">
          <p id="ai">我要开发项目，需要学习人工智能技术。</p>
        </body></html>''')
        await inject(ai,"tests/mock_chrome.js","scripts/dictionary.js","scripts/core.js")
        await ai.evaluate('''()=>{
          Object.assign(__mockStore.settings,{
            enabled:true,aiEnabled:true,level:3,intensity:55,maxTerms:6,showOriginalInBrackets:true});
          const old=chrome.runtime.sendMessage;
          chrome.runtime.sendMessage=async message=>{
            if(message.type==="AI_TRANSFORM"){
              return {ok:true,data:{items:(message.blocks||[]).map(b=>({
                id:b.id,replacements:[
                  {source:"项目",english:"project（项目）",meaning:"项目",level:2},
                  {source:"技术",english:"technology (技术)",meaning:"技术",level:2}
                ]
              }))}};
            }
            return old(message);
          };
        }''')
        await ai.add_style_tag(content=source("scripts/content.css"))
        await inject(ai,"scripts/content.js")
        await ai.wait_for_function('''()=>{
          const text=document.querySelector("#ai").textContent;
          return text.includes("project（项目）")&&text.includes("technology（技术）");
        }''',timeout=12000)
        text=await ai.locator("#ai").inner_text()
        assert "project（项目）（项目）" not in text
        assert "technology（技术）（技术）" not in text
        passed.append("智能模型返回括号后自动去重")
        await ctx.close()
        await browser.close()
    for row in passed:print("通过："+row)
    print("双语括号专项测试全部通过")

if __name__=="__main__":
    asyncio.run(main())
