import os
import subprocess
import tempfile
from pathlib import Path

import markdown

INPUT = Path("USER_GUIDE.md")
OUTPUT = Path("static/USER_GUIDE.pdf")

md = INPUT.read_text(encoding="utf-8")

html_body = markdown.markdown(md, extensions=["tables", "fenced_code"])

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>B2B Lead Finder 使用手册</title>
<style>
  @page {{ margin: 20mm 18mm; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 11pt;
    line-height: 1.7;
    color: #1f2937;
    max-width: 210mm;
    margin: 0 auto;
  }}
  h1 {{ font-size: 22pt; color: #0f172a; border-bottom: 2px solid #0284c7; padding-bottom: 8px; margin-top: 0; }}
  h2 {{ font-size: 15pt; color: #0369a1; margin-top: 24px; border-left: 4px solid #0284c7; padding-left: 10px; }}
  h3 {{ font-size: 12pt; color: #0f172a; margin-top: 18px; }}
  p {{ margin: 8px 0; }}
  a {{ color: #0284c7; text-decoration: none; }}
  ul, ol {{ margin: 8px 0; padding-left: 24px; }}
  li {{ margin: 4px 0; }}
  code {{
    background: #f1f5f9;
    padding: 2px 6px;
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 10pt;
  }}
  pre {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 12px;
    overflow-x: auto;
    font-size: 9.5pt;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0;
    font-size: 10pt;
  }}
  th, td {{
    border: 1px solid #e2e8f0;
    padding: 8px 10px;
    text-align: left;
  }}
  th {{ background: #f1f5f9; font-weight: 600; color: #334155; }}
  blockquote {{
    border-left: 4px solid #0284c7;
    margin: 12px 0;
    padding: 8px 16px;
    background: #f0f9ff;
    color: #0c4a6e;
  }}
  hr {{ border: 0; border-top: 1px solid #e2e8f0; margin: 20px 0; }}
</style>
</head>
<body>
{html_body}
</body>
</html>
"""

with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
    f.write(html)
    tmp_html = f.name

try:
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--run-all-compositor-stages-before-draw",
        "--print-to-pdf=" + str(OUTPUT.resolve()),
        "--print-to-pdf-no-header",
        "file://" + tmp_html,
    ]
    subprocess.run(cmd, check=True, timeout=120)
    print(f"PDF generated: {OUTPUT.resolve()}")
finally:
    os.unlink(tmp_html)
