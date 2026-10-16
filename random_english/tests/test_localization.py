"""随读英语：检查中文界面、默认语言配置和翻译完整性。"""
from pathlib import Path
import json,re
from html.parser import HTMLParser
ROOT=Path(__file__).resolve().parents[1]

class Reader(HTMLParser):
    def __init__(self):super().__init__();self.level=0;self.items=[]
    def handle_starttag(self,tag,attrs):
        if tag in ("script","style"):self.level+=1
    def handle_endtag(self,tag):
        if tag in ("script","style"):self.level=max(0,self.level-1)
    def handle_data(self,data):
        if not self.level and data.strip():self.items.append(data.strip())

manifest=json.loads((ROOT/"manifest.json").read_text("utf-8"))
messages=json.loads((ROOT/"_locales/zh_CN/messages.json").read_text("utf-8"))
assert manifest.get("default_locale")=="zh_CN"
for key in ("name","short_name","description"):
    match=re.fullmatch(r"__MSG_(\w+)__",manifest[key])
    assert match and match.group(1) in messages
for val in messages.values():
    assert re.search("[\u3400-\u9fff]",val["message"])
for filename in ("ui/popup.html","ui/dashboard.html"):
    parser=Reader();parser.feed((ROOT/filename).read_text("utf-8"))
    for t in parser.items:
        t=re.sub(r"Alt\+Shift\+[A-Z]","",t)
        t=t.replace("example.com","").replace("project","").replace("market demand","")  # 英文学习示例保留英文
        assert not re.search("[A-Za-z]",t),f"未翻译文案 {filename}: {t}"
for filename in ("ui/popup.js","ui/dashboard.js","scripts/content.js"):
    text=(ROOT/filename).read_text("utf-8")
    for old in ("AMBIENT ENGLISH","ENGLISH EXPOSURES","DAILY EXPOSURE","FLOWLINGO · IN CONTEXT",
                "FlowLingo · 无感英语","FlowLingo 英语讲解","AI 连接正常","API Key"):
        assert old not in text, f"运行时文案可能未翻译 {filename}: {old}"
dashboard=(ROOT/"ui/dashboard.js").read_text("utf-8")
assert 'new Intl.DateTimeFormat("zh-CN",{weekday:"short"})' in dashboard
print("通过：简体中文扩展配置、全部静态界面文字及动态文案检查")
