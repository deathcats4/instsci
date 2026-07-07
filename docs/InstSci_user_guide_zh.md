# InstSci 用户使用经验与本地环境说明

这份文档是给第一次接触 InstSci 的使用者看的使用经验，不是 skill 文档，也不是开发者审计报告。它的目标是让用户知道：这个工具适合做什么、本地要准备什么、怎么跑第一批 DOI、失败时怎么判断原因。

## 一句话理解

InstSci 不是“绕过权限下载论文”的工具。它更像一个科研文献获取工作流：

1. 先走开放获取路线，能直接拿 OA PDF 就不打开浏览器。
2. OA 拿不到时，再用可见浏览器进入出版社页面。
3. 如果需要机构登录、SSO、2FA、CAPTCHA 或 Cloudflare，人必须在浏览器里手动完成。
4. 下载成功后，把结果写进 manifest；需要的话再同步到 Zotero。

它解决的是“批量文献获取太乱、AI 乱点网页、结果不可诊断”的问题。

## 适合谁用

适合：

- 有合法机构订阅权限的学生、老师、科研人员。
- 经常批量整理 DOI、下载 PDF、导入 Zotero 的人。
- 想让 AI 帮忙管理文献，但又不希望 AI 盲目浏览网页的人。

不适合：

- 没有机构权限，却希望工具强行拿闭源 PDF 的场景。
- 希望自动绕过验证码、SSO、2FA、Cloudflare 的场景。
- 希望所有出版社都 100% 自动、无需人工确认的场景。

## 本地前置条件

推荐环境：

- Windows 10/11。
- Python 3.10 或以上。
- 能正常运行 PowerShell。
- 能打开可见浏览器窗口。
- 网络能访问 DOI、Unpaywall、出版社网站。
- 有自己的学校、研究所或图书馆订阅权限。
- 可选：Zotero Desktop，用来长期管理条目和 PDF。

我本机测试环境：

- Python: `3.13.12`
- InstSci: `0.1.1`
- CloakBrowser: `0.4.6`
- pyzotero: `1.13.2`
- requests: `2.34.2`
- typer: `0.26.8`
- rich: `15.0.0`
- pymupdf: `1.28.0`
- pipx 虚拟环境路径示例：`%USERPROFILE%\pipx\venvs\instsci`

InstSci 项目声明的最低 Python 版本是 `>=3.10`。

## 安装依赖概念

InstSci 依赖这些能力：

- `typer` / `rich`：命令行界面。
- `requests` / `beautifulsoup4` / `lxml`：OA 和网页预检。
- `cloakbrowser`：可见浏览器工作流。
- `pymupdf`：PDF 文本提取和 DOI/标题校验。
- `pyzotero`：Zotero 同步。
- `mcp[cli]`：MCP 工具链支持。

如果是普通用户，不需要理解这些库，只要安装包时依赖能装齐即可。

## 第一次运行前检查

建议先跑：

```powershell
instsci doctor --full
```

这个命令用来检查：

- 当前 InstSci 环境是否能导入。
- Python 依赖是否完整。
- public package 是否有明显缓存/路径/隐私风险。
- 关键数据文件是否能读取。

如果 `doctor` 就失败，不建议直接跑大批量 DOI。

## 最小使用流程

准备一个 `dois.txt`，每行一个 DOI：

```text
10.1016/j.example.2024.100001
10.1007/s00126-023-01199-3
10.3390/min14030310
```

然后运行：

```powershell
instsci papers .\dois.txt --publisher auto --output .\runs\papers_demo
```

推荐先用 5-10 篇试跑，不要一上来几百篇。

## 推荐的批量策略

比较稳的方式是两阶段：

第一阶段：混合 DOI 自动初筛。

```powershell
instsci papers .\dois.txt --publisher auto --output .\runs\batch_auto
```

它会先尝试 OA-first。如果剩下的是混合出版社，InstSci 会生成类似这样的分组：

```text
runs/batch_auto/browser_groups/elsevier_dois.txt
runs/batch_auto/browser_groups/springer_dois.txt
runs/batch_auto/browser_groups/wiley_dois.txt
```

第二阶段：按出版社分别跑。

```powershell
instsci papers .\runs\batch_auto\browser_groups\springer_dois.txt --publisher springer --no-oa-first --output .\runs\springer_group
```

为什么第二阶段建议加 `--no-oa-first`：

- 第一阶段已经做过 OA-first。
- 第二阶段主要是验证闭源/出版社页面。
- 可以减少 Unpaywall、doi.org 和网络代理导致的重复等待。

## 可见浏览器很重要

如果出现这些页面，需要人自己处理：

- 学校 SSO 登录。
- 2FA / 短信 / OTP。
- CAPTCHA。
- Cloudflare “Are you a robot?”。
- 出版社机构选择页面。

InstSci 不会帮你填密码，也不应该绕过验证。浏览器必须可见，因为最终结论要来自真实页面证据，而不是 HTTP 猜测。

## 常见状态怎么理解

`success`：

PDF 已下载，并且 DOI/标题/正文校验基本匹配。

`browser_group_pending`：

不是失败。意思是混合出版社批次已经拆组，需要继续按 publisher 跑。

`auth_required`：

需要你完成机构登录、OpenAthens、Shibboleth、CARSI、SSO 或类似流程。

`human_verification_required` / `waf_blocked`：

遇到验证码、Cloudflare、WAF、异常流量检查。这个通常不是 DOI 错，也不是 InstSci 一定坏了。

`access_unavailable`：

页面显示当前机构没有权限，或者需要购买。这通常是订阅权限问题。

`capture_failed`：

尝试过但没有抓到 PDF。这个才需要重点看日志、截图和 diagnostic。

`pdf_candidate_conflict`：

抓到了 PDF，但校验发现可能不是目标论文主 PDF，例如补充材料、帮助文档、错误页面。

## 哪些失败不是 bug

下面这些不一定是 InstSci 的问题：

- 学校没有订阅这篇文章。
- DOI 本身不存在或解析到错误页面。
- 出版社临时 Cloudflare / WAF。
- 用户网络中断、代理不可用。
- 需要 SSO/2FA 但用户没完成。
- 一个 DOI 前缀不代表一定在同一个宿主站点，例如部分 `10.1007` 会落到非 Springer 页面。

真正值得反馈给开发者的是：

- 明明页面有 PDF 按钮，但 InstSci 没点到。
- 下载到的是帮助文档、补充材料、别的论文。
- manifest 状态和浏览器截图明显不一致。
- 中断后 rerun 跑到了旧 DOI。
- 同一 DOI 手工能下载，InstSci 稳定失败。

## 网络和代理注意事项

如果环境变量里有坏代理，OA-first 会非常慢，甚至每篇 DOI 都重试。

可以检查：

```powershell
Get-ChildItem Env: | Where-Object { $_.Name -match 'proxy|PROXY' }
```

如果看到类似 `127.0.0.1:9` 这种拒绝连接的代理，可能会影响：

- Unpaywall 查询。
- doi.org 解析。
- Crossref / OpenAlex 等预检。

这时可以先关闭坏代理，或者在第二阶段 publisher 分组时使用 `--no-oa-first`。

## Zotero 推荐用法

我推荐 Zotero 只保存干净的东西：

- 文献条目。
- 对应 PDF 附件。

不要把 InstSci 的过程日志、截图、证据 note 都塞进 Zotero。那些东西留在 InstSci run 目录里就好。

同步示例：

```powershell
instsci zotero handoff .\runs\papers_demo --tags project/demo --collections "Demo Collection"
instsci zotero sync .\runs\papers_demo --attachment-mode linked_file
```

推荐 `linked_file`：

- 不占 Zotero 云存储。
- 适合本地大批量 PDF。
- 也方便配合 Zotero Attanger 这类附件整理插件。

## 输出目录怎么看

一次运行通常会产生：

```text
runs/papers_demo/
  complete/
    manifest.csv
    manifest.json
    pdfs/
  browser_groups/
  diagnostics/
  summary.json
```

重点看：

- `summary.json`：总数、成功数、失败状态统计。
- `complete/manifest.csv`：每篇 DOI 的状态、PDF 路径、下一步建议。
- `complete/pdfs/`：最终 PDF。
- `diagnostics/`：浏览器截图、页面状态、失败证据。
- `browser_groups/`：混合批次拆出来的出版社 DOI 文件。

## 使用预期

第一次用不要追求“大而全”。正确姿势是：

1. 先跑 5 篇。
2. 再跑 20 篇。
3. 看失败原因。
4. 把失败分成权限问题、网络问题、验证码问题、工具 bug。
5. 只把真正可复现的工具 bug 提出来。

这样 InstSci 会越用越稳，也不会被某个学校或某个出版社当天的风控误导。

