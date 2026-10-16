"""DOM smoke test in Chromium with a mocked extension runtime.
This does not test actual installation or Chrome Web Store permissions.
Requires: pip install playwright, plus local chromium executable.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]

def source(path):return (ROOT/path).read_text(encoding="utf-8")

async def inject(page,*paths):
    for path in paths:
        await page.add_script_tag(content=source(path))

async def main():
    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path="/usr/bin/chromium",headless=True,args=["--no-sandbox"])
        page=await browser.new_page(viewport={"width":1200,"height":850})
        await page.set_content(source("tests/fixture.html"))
        await inject(page,"tests/mock_chrome.js","scripts/dictionary.js","scripts/core.js","scripts/content.js")
        await page.add_style_tag(content=source("scripts/content.css"))
        await page.locator("#chinese .fl-inline-mark").first.wait_for(timeout=10000)
        assert await page.locator("#english .fl-inline-mark").count()>0
        assert await page.locator("#chinese .fl-inline-original").count()>0
        assert await page.locator("#chinese .fl-inline-mark").first.inner_text()
        assert "（" in await page.locator("#chinese .fl-inline-mark").first.inner_text()
        assert await page.locator("#english .fl-inline-original").count()==0
        results.append("中文译文带原文括号，英文文章保持原有标注")
        assert await page.locator("#keep-link .fl-inline-mark").count()==0
        assert await page.locator("#keep-contenteditable .fl-inline-mark").count()==0
        assert "项目" in await page.locator("#keep-editor").input_value()
        results.append("中英文网页文字处理，跳过编辑区域")
        await page.locator("#keep-button").click()
        await page.locator(".late .fl-inline-mark").first.wait_for(timeout=8000)
        results.append("动态新增网页段落处理")
        await page.locator("#chinese .fl-inline-mark").first.click()
        assert await page.locator("#flowlingo-root").evaluate(
            "(h)=>h.shadowRoot.querySelector('.bubble').style.display==='block'")
        await page.locator("#flowlingo-root").evaluate(
            "(h)=>h.shadowRoot.querySelector('.bubble .btn.primary').click()")
        await page.wait_for_timeout(120)
        assert await page.evaluate("Object.keys(__mockStore.words).length")>=1
        results.append("悬浮讲解卡片和生词保存")
        await page.evaluate("""async()=>{
           const {settings}=await chrome.storage.local.get("settings");
           await chrome.storage.local.set({settings:{...settings,enabled:false}});
        }""")
        await page.wait_for_timeout(700)
        assert await page.locator("#chinese .fl-inline-mark").count()==0
        await page.evaluate("""async()=>{
           const {settings}=await chrome.storage.local.get("settings");
           await chrome.storage.local.set({settings:{...settings,enabled:true,level:1,intensity:3,maxTerms:1}});
        }""")
        await page.locator("#chinese .fl-inline-mark").first.wait_for(timeout=8000)
        assert await page.locator("#chinese .fl-inline-mark").count()<=1
        results.append("恢复原文、重新启用和接触强度限制")
        await page.close()

        dash=await browser.new_page(viewport={"width":1200,"height":850})
        await dash.set_content(source("ui/dashboard.html"))
        await dash.add_style_tag(content=source("ui/dashboard.css"))
        await inject(dash,"tests/mock_chrome.js","scripts/core.js")
        await dash.evaluate("""()=>{
          __mockStore.words.project={term:"project",meaning:"项目",context:"开始一个新的项目",
            status:"learning",createdAt:Date.now(),dueAt:Date.now()};
        }""")
        await inject(dash,"ui/dashboard.js")
        assert await dash.locator("#statWords").inner_text()=="1"
        await dash.locator('[data-tab="review"]').click()
        await dash.locator("#flipCard").click()
        assert await dash.locator("#reviewAnswer").is_visible()
        await dash.locator('[data-grade="4"]').click()
        assert await dash.locator("#reviewEmpty").is_visible()
        results.append("学习中心与间隔复习")
        await dash.locator('[data-tab="settings"]').click()
        await dash.locator("#setLevel").select_option("4")
        await dash.locator("#setIntensity").fill("26")
        await dash.locator("#setManualSelection").uncheck()
        await dash.locator("#setPageMode").select_option("manual")
        await dash.locator("#saveSettings").click()
        await dash.wait_for_timeout(100)
        assert await dash.evaluate(
          "__mockStore.settings.level===4 && __mockStore.settings.intensity===26 && !__mockStore.settings.manualSelectionEnabled && __mockStore.settings.pageMode==='manual'")
        await dash.locator("#setOriginalInBrackets").uncheck()
        assert not await dash.locator("#previewOriginal").is_visible()
        await dash.locator("#saveSettings").click()
        assert await dash.evaluate("__mockStore.settings.showOriginalInBrackets===false")
        results.append("原文括号开关可关闭，设置保存与实时预览正常")
        results.append("学习偏好设置保存")
        await dash.close()

        caption=await browser.new_page(viewport={"width":1100,"height":800})
        await caption.set_content("""<html><body><video style="width:700px;height:400px;background:teal"></video>
             <div class="ytp-caption-segment" style="font:24px sans-serif">
               项目需要技术和市场分析。
             </div></body></html>""")
        await inject(caption,"tests/mock_chrome.js","scripts/dictionary.js","scripts/core.js")
        await caption.evaluate("""()=>{
           const old=chrome.runtime.sendMessage;
           chrome.runtime.sendMessage=async (msg)=>{
             if(msg.type==="AI_VIDEO")return {ok:true,data:{translation:"The project needs technical analysis.",tip:"technical: 技术的"}};
             if(msg.type==="AI_TRANSFORM")return {ok:true,data:{items:[]}};
             return old(msg);
           };
           __mockStore.settings.aiEnabled=true;
        }""")
        simulated=source("scripts/content.js")
        simulated=simulated.replace("VIDEO_SITES.test(location.hostname)","true")
        simulated=simulated.replace('if(/youtube\\.com$/.test(location.hostname))selector=','if(true)selector=')
        await caption.add_script_tag(content=simulated)
        await caption.wait_for_function("""()=>{
          const h=document.querySelector('#flowlingo-root');
          return h?.shadowRoot.querySelector('.video-target')?.textContent.includes("project");
        }""",timeout=8000)
        results.append("模拟视频字幕读取与双语叠层")
        await browser.close()
    for item in results: print("通过：",item)
    print("网页交互模拟测试全部通过")

if __name__=="__main__":
    asyncio.run(main())
