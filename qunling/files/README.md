# 通用文件处理库

`qunling.files` 不依赖 QQ 或 LangBot SDK。它负责格式探测、结构化读取、
检索和从明确数据结构导出产物；文件授权、下载、资产登记、持久任务、
隔离执行及发送确认由调用方负责。

```python
from qunling.files import FileError, FileLimits
from qunling.files import inspect_file, read_file, search_file, export_file

manifest = inspect_file(trusted_input_path)
page = read_file(trusted_input_path, {"sheet": "数据", "limit": 20})
while page["next"]:
    page = read_file(trusted_input_path, page["next"])
hits = search_file(trusted_input_path, "关键词")
artifact = export_file("docx", {"blocks": [{"text": "分析结果"}]}, trusted_output_path)
```

## 读取约定

`inspect_file` 返回真实内容类型、MIME、字节数、SHA-256、能力和结构。
未知二进制文件只声明 `transfer`，不把下载成功描述为读取成功。
Markdown、CSV 和源码属于纯文本子类型，部分子类型依据文件名，返回相应提示。

`read_file` 返回 `format`、`sections`、`next`、`truncated`、`needs_ocr`、
`warnings`。一次提供一个正文块、页面、幻灯片、表格或目录；将 `next`
原样传入可继续读取。同一结果不会默默把超限文档当作完整内容。

可用选择器：

| 字段 | 含义 |
| --- | --- |
| `section` | `inspect_file` 返回的结构 ID |
| `page` | PDF/PPTX 的页号，从 1 开始 |
| `sheet` | XLSX 工作表名称 |
| `offset` | 未选择结构时为结构下标；选择表格或目录后为行/成员下标，从 0 开始 |
| `limit` | 单次行数或目录成员数，默认 100 |
| `text_offset` | 长正文或单元格的字符续读位置 |
| `column_offset`、`column_limit` | 表格列范围，从 0 开始；默认前 100 列，省略列会明确提示 |
| `cell` | 精确读取表格单元格，如 `R2C3`；可结合 `text_offset` 续读超长单元格 |
| `member` | 压缩包中精确的成员路径 |

`section`、`page`、`sheet` 不能同时使用。表格行号从 1 开始。
`read_archive_member(path, member)` 返回一个经过校验的成员的字节串；
调用方可将它登记为子资产。不展开到宿主文件系统、不隐式递归解包。

支持 TXT/MD、UTF-8/BOM UTF-16/GB18030 文本、常见源码、CSV/TSV、JSON；
PDF 文本、DOCX 正文与表格、XLSX 工作表、PPTX 幻灯片文字；
ZIP/RAR/7z 混合内容目录和成员读取。图片只识别容器头并声明需要视觉处理。

- PDF 无可提取文本的页面标记 `needs_ocr`；这不是已完成 OCR。
- DOCX 不提取页眉、脚注；PPTX 不解析图片、图表、备注。结果带明确提示。
- XLSX 公式与缓存值分开返回，缓存可能缺失或过时；不计算公式。
- 不执行宏、嵌入程序、源代码，不跟随 Office 外部关系。
- 加密/分卷/自解压压缩包、ZIP64、非 Store/Deflate ZIP 暂不支持。
- 解析依赖为 `pypdf`、`python-docx`、`openpyxl`、`python-pptx`；
  RAR/7z 需要完整 `7zz`/`7z`。依赖缺失返回 `dependency_missing`。

## 导出约定

输出路径必须由可信主机代码选择，父目录须已存在。永不覆盖已有文件。
结果返回格式、MIME、字节数、SHA-256 和警告。

| 格式 | 数据结构 |
| --- | --- |
| TXT/MD | `{"text": "正文"}` |
| JSON | `{"data": 任意合法 JSON 值}` |
| CSV | `{"rows": [["列名", "数量"], ["条目", 1]]}` |
| DOCX | `{"blocks": [{"heading": "标题", "level": 1}, {"text": "正文"}, {"table": [["列名"], ["条目"]]}]}` |
| XLSX | `{"sheets": [{"name": "数据", "rows": [["列名"], ["条目"]]}]}` |
| PPTX | `{"slides": [{"title": "标题", "text": "正文"}]}` |
| ZIP | `{"files": [{"name": "notes/a.txt", "text": "正文"}, {"name": "image.png", "base64": "..."}]}` |

单元格只接受字符串、有限数值、布尔值和空值。XLSX 中形如 `=...`
的字符串保持字符串类型；CSV 的公式样文本前加单引号，并在产物警告中说明。
ZIP 成员只允许安全相对路径，不能提供宿主文件路径。

## 限制和隔离

默认限制包括：输入 40 MiB、输出/单成员 20 MiB、压缩包展开 200 MiB、
1000 个成员或结构块、10 万行、20 万单元格、提取文本 100 万字符、
单次结果 2.4 万字符。限制可由可信主机通过 `FileLimits` 调整，模型不能设置。
XML 拒绝 DTD/实体、过深结构；压缩包拒绝路径穿越、链接、重复名称和异常压缩比。

**必须把不可信文档解析放在独立受限工作进程或容器中。** Python PDF/Office
解析库仍可能因畸形文件耗费大量 CPU 或内存；本库的输入预检不构成安全沙箱。
7-Zip 子进程已有内存、CPU、时间、输出限制，但同样没有隔离宿主文件访问。
生产调用方应关闭网络、移除凭据和宿主敏感挂载、限制 CPU/内存/PID/磁盘/时间。
不要在 HTTP 或插件事件线程同步执行这些操作。
